"""协作状态持久化；不读取或修改项目代码，不执行消息里的指令。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

# 对外消息类型；自由正文只是数据，不拥有执行权限。
KINDS = {"task", "plan", "question", "review", "change", "result", "note"}
# 身份名称限制为可安全显示、可放入配置文件的短标识。
NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,47}\Z")
# 拒绝 Windows 设备名，避免相同路径被 Win32 解释成设备或别名。
RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


@dataclass(frozen=True)
class Actor:
    """服务器认证得到的身份；客户端不能通过工具参数指定发送者。"""

    project_id: str  # 被授权的单个项目。
    name: str  # 项目内唯一身份名。
    role: str  # 给人查看的分工描述，不作为权限来源。


def text(value: str, label: str, maximum: int, allow_empty: bool = False) -> str:
    """校验 value 文本；label 是错误字段名，maximum 是字符上限；返回原文本。"""
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{label} 必须是文本且不能包含空字符")
    if len(value) > maximum or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} 必须为 1～{maximum} 个字符")
    return value


class Store:
    """SQLite 协作存储；path 为数据库文件，master 为本机管理密钥。"""

    def __init__(self, path: Path, master: str) -> None:
        self.path = path  # 数据库路径，每次操作使用独立连接。
        self.master = master  # 只用于派生连接令牌，绝不放入日志或工具返回值。
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(write=True) as db:
            # 版本门禁防止旧程序误写未来版本的数据库。
            version = db.execute("PRAGMA user_version").fetchone()[0]  # 现有表结构版本。
            if version not in (0, 1):
                raise ValueError("数据库版本较新，请使用对应版本的程序")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agents (
                    project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
                    role TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, seen REAL NOT NULL,
                    PRIMARY KEY(project_id, name)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES projects(id), sender TEXT NOT NULL,
                    thread TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
                    body TEXT NOT NULL, client_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    created REAL NOT NULL, UNIQUE(project_id, sender, client_id),
                    FOREIGN KEY(project_id, sender) REFERENCES agents(project_id, name)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    message_id INTEGER NOT NULL REFERENCES messages(id),
                    recipient TEXT NOT NULL, acknowledged REAL,
                    PRIMARY KEY(message_id, recipient)
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    project_id TEXT NOT NULL, path TEXT NOT NULL, owner TEXT NOT NULL,
                    expires REAL NOT NULL, PRIMARY KEY(project_id, path),
                    FOREIGN KEY(project_id, owner) REFERENCES agents(project_id, name)
                );
                CREATE INDEX IF NOT EXISTS message_project_id ON messages(project_id, id);
                CREATE INDEX IF NOT EXISTS delivery_recipient ON deliveries(recipient, message_id);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """创建短连接；write=True 时串行提交事务；异常时回滚，始终关闭连接。"""
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # 本次操作连接。
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def project(self, root: str) -> dict[str, Any]:
        """打开现有 root 目录并返回项目；规范化路径避免同一项目被重复注册。"""
        directory = Path(text(root, "项目目录", 32000)).resolve(strict=True)  # 真实项目根目录。
        if not directory.is_dir():
            raise ValueError("项目路径不是目录")
        canonical = os.path.normcase(str(directory))  # Windows 下忽略路径大小写。
        project_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]  # 稳定项目标识。
        with self.connection(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO projects VALUES(?,?,?)",
                (project_id, canonical, time.time()),
            )
        return {"id": project_id, "root": canonical}

    def token(self, project_id: str, name: str) -> str:
        """从 master 为 project_id/name 派生固定令牌；调用者负责保护返回值。"""
        identity = f"agent\0{project_id}\0{name}".encode()  # 带分隔符的身份域。
        return hmac.new(self.master.encode("ascii"), identity, hashlib.sha256).hexdigest()

    def register(self, project_id: str, name: str, role: str) -> dict[str, Any]:
        """管理端登记 name/role；项目必须已存在；返回身份和该身份专用令牌。"""
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("AI 名称需为 2～48 位英文字母、数字、下划线或短横线，并以字母开头")
        text(role, "角色", 80)
        credential = self.token(project_id, name)  # 只在管理端接入配置中使用。
        digest = hashlib.sha256(credential.encode("ascii")).hexdigest()  # 数据库仅保存哈希。
        with self.connection(write=True) as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ValueError("项目不存在")
            db.execute(
                "INSERT INTO agents VALUES(?,?,?,?,?) ON CONFLICT(project_id,name) DO UPDATE SET role=excluded.role",
                (project_id, name, role, digest, 0),
            )
        return {
            "project_id": project_id,
            "name": name,
            "role": role,
            "token": credential,
        }

    def authenticate(self, credential: str) -> Actor | None:
        """验证客户端 credential 并记录活动时间；无效令牌返回 None，不透露具体原因。"""
        digest = hashlib.sha256(
            credential.encode("utf-8")
        ).hexdigest()  # 对不可信输入也只做固定哈希。
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM agents WHERE token_hash=?", (digest,)
            ).fetchone()  # 对应身份。
            if row is None:
                return None
            db.execute("UPDATE agents SET seen=? WHERE token_hash=?", (time.time(), digest))
            return Actor(row["project_id"], row["name"], row["role"])

    def _member(self, db: sqlite3.Connection, actor: Actor) -> None:
        """在 db 内验证 actor 尚为有效成员；内部 Store 方法也不信任伪造身份。"""
        if not db.execute(
            "SELECT 1 FROM agents WHERE project_id=? AND name=?",
            (actor.project_id, actor.name),
        ).fetchone():
            raise ValueError("身份无效")

    def send(
        self,
        actor: Actor,
        recipients: list[str],
        thread: str,
        kind: str,
        subject: str,
        body: str,
        client_id: str,
    ) -> dict[str, Any]:
        """将 actor 的消息事务性投递给 recipients；thread/kind/subject/body 为正文，client_id 用于去重。"""
        text(thread, "话题", 100)
        text(subject, "标题", 200)
        text(body, "正文", 16000)
        text(client_id, "去重标识", 128)
        if kind not in KINDS:
            raise ValueError("未知消息类型")
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 16:
            raise ValueError("必须明确指定 1～16 个接收者，不支持广播")
        if any(
            not isinstance(name, str) or not NAME_PATTERN.fullmatch(name) for name in recipients
        ):
            raise ValueError("接收者名称无效")
        targets = sorted(set(recipients))  # 固定顺序保证等价重试有相同摘要。
        payload = json.dumps(
            [targets, thread, kind, subject, body], ensure_ascii=False
        )  # 去重比较数据。
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()  # 相同键不同正文必须报错。
        with self.connection(write=True) as db:
            self._member(db, actor)
            for name in targets:  # 接收者逐个验证，任何一个无效都不产生投递。
                if not db.execute(
                    "SELECT 1 FROM agents WHERE project_id=? AND name=?",
                    (actor.project_id, name),
                ).fetchone():
                    raise ValueError(f"当前项目中没有接收者：{name}")
            previous = db.execute(
                "SELECT id,payload_hash FROM messages WHERE project_id=? AND sender=? AND client_id=?",
                (actor.project_id, actor.name, client_id),
            ).fetchone()  # 已成功提交的相同业务请求。
            if previous:
                if previous["payload_hash"] != digest:
                    raise ValueError("该去重标识已用于不同消息；请先核对投递结果")
                return {"id": previous["id"], "deduplicated": True}
            cursor = db.execute(
                "INSERT INTO messages(project_id,sender,thread,kind,subject,body,client_id,payload_hash,created) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    actor.project_id,
                    actor.name,
                    thread,
                    kind,
                    subject,
                    body,
                    client_id,
                    digest,
                    time.time(),
                ),
            )  # 新消息写入游标。
            message_id = cursor.lastrowid  # SQLite 分配的单调消息编号。
            db.executemany(
                "INSERT INTO deliveries(message_id,recipient) VALUES(?,?)",
                [(message_id, name) for name in targets],
            )
            return {"id": message_id, "deduplicated": False}

    def _messages(
        self, db: sqlite3.Connection, query: str, args: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        """执行内部固定 query/args 并附加投递信息；永不拼接客户端提供的 SQL。"""
        result = []  # 返回给界面的消息集合。
        for row in db.execute(query, args).fetchall():  # 每条消息的普通字段。
            item = dict(row)  # 不包含令牌哈希或内部幂等摘要。
            item.pop("payload_hash", None)
            item["deliveries"] = [
                dict(delivery)
                for delivery in db.execute(
                    "SELECT recipient,acknowledged FROM deliveries WHERE message_id=? ORDER BY recipient",
                    (row["id"],),
                )
            ]  # 每位接收者的独立确认时间。
            result.append(item)
        return result

    def read(
        self, actor: Actor, after: int = 0, limit: int = 100, thread: str | None = None
    ) -> dict[str, Any]:
        """读取 actor 的收件箱，或指定 thread 的项目公开对话；after 为排他游标，读取不等于确认。"""
        if (
            not isinstance(after, int)
            or after < 0
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ValueError("游标必须非负，批量大小为 1～200")
        if thread is not None:
            text(thread, "话题", 100)
        with self.connection() as db:
            self._member(db, actor)
            # 查询分别固定，避免在字符串内混入任何客户端值。
            if thread is None:
                items = self._messages(
                    db,
                    "SELECT m.* FROM messages m JOIN deliveries d ON d.message_id=m.id "
                    "WHERE m.project_id=? AND d.recipient=? AND m.id>? ORDER BY m.id LIMIT ?",
                    (actor.project_id, actor.name, after, limit),
                )  # 仅自己的收件箱。
            else:
                items = self._messages(
                    db,
                    "SELECT * FROM messages WHERE project_id=? AND thread=? AND id>? ORDER BY id LIMIT ?",
                    (actor.project_id, thread, after, limit),
                )  # 同项目话题对成员可见。
            return {
                "messages": items,
                "next_cursor": items[-1]["id"] if items else after,
            }

    def acknowledge(self, actor: Actor, message_id: int) -> dict[str, Any]:
        """actor 确认自己的 message_id 投递；重复确认幂等，不能确认他人的消息。"""
        with self.connection(write=True) as db:
            self._member(db, actor)
            changed = db.execute(
                "UPDATE deliveries SET acknowledged=COALESCE(acknowledged,?) "
                "WHERE message_id=? AND recipient=? AND EXISTS "
                "(SELECT 1 FROM messages WHERE id=? AND project_id=?)",
                (time.time(), message_id, actor.name, message_id, actor.project_id),
            ).rowcount  # 命中的本人投递数。
            if changed != 1:
                raise ValueError("不存在可由当前身份确认的消息")
            return {"id": message_id, "acknowledged": True}

    def members(self, actor: Actor) -> dict[str, Any]:
        """返回 actor 所在项目的角色、最后活动时间和有效文件租约。"""
        with self.connection() as db:
            self._member(db, actor)
            return {
                "project_id": actor.project_id,
                "self": actor.name,
                "agents": [
                    dict(row)
                    for row in db.execute(
                        "SELECT name,role,seen FROM agents WHERE project_id=? ORDER BY name",
                        (actor.project_id,),
                    )
                ],
                "reservations": [
                    dict(row)
                    for row in db.execute(
                        "SELECT path,owner,expires FROM reservations WHERE project_id=? AND expires>? ORDER BY path",
                        (actor.project_id, time.time()),
                    )
                ],
            }

    def reserve(
        self, actor: Actor, paths: list[str], ttl: int = 900, release: bool = False
    ) -> dict[str, Any]:
        """为 actor 原子申请 paths 建议性租约，ttl 为秒；release=True 仅释放本人指定路径。"""
        if not isinstance(paths, list) or not 1 <= len(paths) <= 32 or not 30 <= ttl <= 3600:
            raise ValueError("路径数量为 1～32，租期为 30～3600 秒")
        with self.connection(write=True) as db:
            self._member(db, actor)
            root = Path(
                db.execute("SELECT root FROM projects WHERE id=?", (actor.project_id,)).fetchone()[
                    0
                ]
            )  # 项目根。
            normalized = []  # 校验后的项目相对路径，不支持模糊 glob。
            for value in paths:  # 输入的单个文件或目录。
                text(value, "文件路径", 1000)
                relative = PurePosixPath(value.replace("\\", "/"))  # 统一分隔符。
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or any(part in ("..", ".") for part in relative.parts)
                    or any(
                        part.endswith((" ", ".")) or part.split(".")[0].upper() in RESERVED_NAMES
                        for part in relative.parts
                    )
                    or ":" in value
                    or "*" in value
                    or "?" in value
                ):
                    raise ValueError("租约只能使用项目内明确的相对路径，不允许上级路径或通配符")
                resolved = (root / str(relative)).resolve()  # 检测符号链接、目录联接带来的越界。
                if not resolved.is_relative_to(root.resolve()):
                    raise ValueError("文件路径超出项目目录")
                normalized.append(
                    os.path.normcase(str(resolved.relative_to(root.resolve()))).replace("\\", "/")
                )
            normalized = sorted(set(normalized))
            if release:
                for path in normalized:  # 只释放本人持有的完全匹配租约。
                    db.execute(
                        "DELETE FROM reservations WHERE project_id=? AND path=? AND owner=?",
                        (actor.project_id, path, actor.name),
                    )
                return {"released": normalized}
            now = time.time()  # 持久化租约采用墙上时间，重启后仍可判断到期。
            conflicts = []  # 任何冲突都阻止整批申请，绝不留下半批租约。
            for row in db.execute(
                "SELECT path,owner,expires FROM reservations WHERE project_id=? AND expires>? AND owner<>?",
                (actor.project_id, now, actor.name),
            ):
                if any(
                    path == row["path"]
                    or path.startswith(row["path"] + "/")
                    or row["path"].startswith(path + "/")
                    for path in normalized
                ):
                    conflicts.append(dict(row))
            if conflicts:
                return {"acquired": False, "conflicts": conflicts}
            for path in normalized:  # 成功时一起写入或续租。
                db.execute(
                    "INSERT INTO reservations VALUES(?,?,?,?) ON CONFLICT(project_id,path) "
                    "DO UPDATE SET owner=excluded.owner,expires=excluded.expires",
                    (actor.project_id, path, actor.name, now + ttl),
                )
            return {"acquired": True, "paths": normalized, "expires": now + ttl}

    def snapshot(self, project_id: str | None = None, after: int | None = None) -> dict[str, Any]:
        """管理端快照；project_id 选择项目，after 非空时提供可分页导出的后续消息。"""
        with self.connection() as db:
            projects = [
                dict(row) for row in db.execute("SELECT * FROM projects ORDER BY created")
            ]  # 已登记项目。
            if not project_id:
                return {
                    "projects": projects,
                    "agents": [],
                    "messages": [],
                    "reservations": [],
                }
            if after is None:
                messages = self._messages(
                    db,
                    "SELECT * FROM (SELECT * FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 300) ORDER BY id",
                    (project_id,),
                )  # 窗口默认最近 300 条，完整历史通过分页导出。
            else:
                messages = self._messages(
                    db,
                    "SELECT * FROM messages WHERE project_id=? AND id>? ORDER BY id LIMIT 200",
                    (project_id, after),
                )  # 导出不能静默截断历史。
            return {
                "projects": projects,
                "messages": messages,
                "agents": [
                    dict(row)
                    for row in db.execute(
                        "SELECT name,role,seen FROM agents WHERE project_id=? ORDER BY name",
                        (project_id,),
                    )
                ],
                "reservations": [
                    dict(row)
                    for row in db.execute(
                        "SELECT path,owner,expires FROM reservations WHERE project_id=? AND expires>? ORDER BY path",
                        (project_id, time.time()),
                    )
                ],
            }
