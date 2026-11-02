"""业务存储的事务、隔离、游标和建议性租约回归测试。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.store import Actor, Store


@pytest.fixture
def state(tmp_path: Path):
    """为测试创建单项目与三个身份，返回存储、工程师、审查员和第三人。"""
    store = Store(tmp_path / "db.sqlite3", "test-only-master")  # 测试专用密钥，不是生产凭据。
    project = store.project(str(tmp_path))  # 临时项目。
    actors = []  # 独立测试身份。
    for name in ("Engineer", "Reviewer", "ThirdAgent"):  # 角色名与名字一致方便断言。
        store.register(project["id"], name, name)
        actors.append(Actor(project["id"], name, name))
    return store, *actors


def send(store, actor, recipients=None, client_id="message-1", body="中文协作消息"):
    """发送固定测试话题；可覆盖接收者、幂等键和正文。"""
    return store.send(
        actor,
        recipients or ["Reviewer"],
        "quality",
        "review",
        "检查边界条件",
        body,
        client_id,
    )


def test_delivery_is_explicit_and_ack_is_per_recipient(state):
    """读取不等于确认；第三人不能确认消息；两位接收者互不影响。"""
    store, engineer, reviewer, third = state  # 当前测试身份。
    message = send(store, engineer, ["Reviewer", "ThirdAgent"])  # 同一消息的两份投递。
    assert store.read(engineer)["messages"] == []
    assert store.read(reviewer)["messages"][0]["body"] == "中文协作消息"
    assert all(
        delivery["acknowledged"] is None
        for delivery in store.read(reviewer)["messages"][0]["deliveries"]
    )
    assert store.acknowledge(reviewer, message["id"])["acknowledged"]
    assert store.acknowledge(reviewer, message["id"])["acknowledged"]
    with pytest.raises(ValueError):
        store.acknowledge(engineer, message["id"])
    deliveries = store.read(third)["messages"][0]["deliveries"]  # 确认只改变 Reviewer 的记录。
    assert (
        next(item for item in deliveries if item["recipient"] == "Reviewer")["acknowledged"]
        is not None
    )
    assert (
        next(item for item in deliveries if item["recipient"] == "ThirdAgent")["acknowledged"]
        is None
    )


def test_idempotency_and_changed_payload(state):
    """结果不明时可安全原样重试；相同键不同正文不能静默成功。"""
    store, engineer, reviewer, _ = state  # 本次存储与身份。
    first = send(store, engineer)  # 第一次投递。
    repeated = send(store, engineer)  # 原样重试。
    assert first["id"] == repeated["id"]
    assert repeated["deduplicated"] is True
    assert len(store.read(reviewer)["messages"]) == 1
    with pytest.raises(ValueError, match="不同消息"):
        send(store, engineer, body="不同正文")


def test_partial_delivery_rolls_back(state):
    """一位接收者不存在时，不能给其他接收者留下半条成功记录。"""
    store, engineer, reviewer, _ = state  # 本次存储与身份。
    with pytest.raises(ValueError):
        send(store, engineer, ["Reviewer", "UnknownAgent"])
    assert store.read(reviewer)["messages"] == []


def test_project_and_credentials_are_isolated(state, tmp_path):
    """同名身份在两个项目中有不同令牌，无法读取、确认或释放对方的记录。"""
    store, engineer, reviewer, _ = state  # 原项目的真实成员。
    other_root = tmp_path / "other"  # 第二个临时项目。
    other_root.mkdir()
    other = store.project(str(other_root))  # 第二项目。
    credential = store.register(other["id"], "Reviewer", "另一个项目审查员")[
        "token"
    ]  # 只在内存中用的令牌。
    other_actor = store.authenticate(credential)  # 第二项目身份。
    assert other_actor is not None
    assert credential != store.token(reviewer.project_id, "Reviewer")
    assert store.authenticate("wrong-token") is None
    message = send(store, engineer)  # 原项目消息。
    assert store.read(other_actor)["messages"] == []
    assert store.read(other_actor, thread="quality")["messages"] == []
    with pytest.raises(ValueError):
        store.acknowledge(other_actor, message["id"])


def test_cursor_does_not_skip_interleaved_messages(state):
    """批量游标只推进到已返回的消息，不受其他收件箱消息穿插影响。"""
    store, engineer, reviewer, _ = state  # 当前测试身份。
    ids = []  # Reviewer 应当收到的所有编号。
    for index in range(7):  # 在目标消息之间插入不相关投递。
        ids.append(send(store, engineer, client_id=f"target-{index}")["id"])
        send(store, engineer, ["ThirdAgent"], client_id=f"other-{index}")
    received = []  # 分页读取到的所有编号。
    after = 0  # 初始游标。
    while True:
        page = store.read(reviewer, after=after, limit=2)  # 当前页。
        if not page["messages"]:
            assert page["next_cursor"] == after
            break
        received.extend(item["id"] for item in page["messages"])
        after = page["next_cursor"]
    assert received == ids


def test_concurrent_idempotent_send_has_one_commit(state):
    """并发原样重试必须只提交一条消息，不出现唯一约束异常。"""
    store, engineer, reviewer, _ = state  # 共享存储但各线程使用独立 SQLite 连接。
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: send(store, engineer), range(24))
        )  # 同一业务消息的 24 次并发请求。
    assert len({result["id"] for result in results}) == 1
    assert len(store.read(reviewer)["messages"]) == 1


def test_file_leases_are_atomic_hierarchical_and_owner_scoped(state):
    """目录租约覆盖子文件；批量冲突不留半批；别人不能释放持有者的租约。"""
    store, engineer, reviewer, _ = state  # 本次租约竞争双方。
    assert store.reserve(engineer, ["src"])["acquired"]
    denied = store.reserve(reviewer, ["src/app.py", "tests/test_app.py"])  # 有一项冲突的批量申请。
    assert denied["acquired"] is False
    assert not any(item["owner"] == "Reviewer" for item in store.members(reviewer)["reservations"])
    store.reserve(reviewer, ["src"], release=True)
    assert not store.reserve(reviewer, ["src/app.py"])["acquired"]
    store.reserve(engineer, ["src"], release=True)
    assert store.reserve(reviewer, ["src/app.py"])["acquired"]
    assert not store.reserve(engineer, ["src"])["acquired"]


def test_expired_lease_can_be_reclaimed(state, monkeypatch):
    """模拟时间前进，不用真的等 30 秒；持久化租约可在到期后被新身份认领。"""
    store, engineer, reviewer, _ = state  # 当前竞争双方。
    monkeypatch.setattr("backend.store.time.time", lambda: 1000)
    store.reserve(engineer, ["file.py"], ttl=30)
    monkeypatch.setattr("backend.store.time.time", lambda: 1031)
    assert store.reserve(reviewer, ["file.py"])["acquired"]


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "C:\\file.py",
        "src/../../file.py",
        "src/*.py",
        "//server/share",
        "src/file.py.",
        "src/file.py ",
        "NUL",
        "src/COM1.txt",
    ],
)
def test_unsafe_lease_paths_are_rejected(state, path):
    """绝对、越界和通配符路径不能成为项目租约。"""
    store, engineer, _, _ = state  # 当前申请身份。
    with pytest.raises(ValueError):
        store.reserve(engineer, [path])


@pytest.mark.parametrize(
    "field,value",
    [
        ("thread", ""),
        ("body", ""),
        ("body", "a" * 16001),
        ("subject", "a" * 201),
        ("client_id", ""),
        ("kind", "execute"),
    ],
)
def test_invalid_message_fields_do_not_write(state, field, value):
    """无效字段不能写入部分消息或扩大消息类型为执行能力。"""
    store, engineer, reviewer, _ = state  # 当前存储及身份。
    arguments = {
        "recipients": ["Reviewer"],
        "thread": "test",
        "kind": "note",
        "subject": "标题",
        "body": "正文",
        "client_id": "test-1",
    }  # 最小有效请求。
    arguments[field] = value
    with pytest.raises(ValueError):
        store.send(engineer, **arguments)
    assert store.read(reviewer)["messages"] == []


def test_full_export_is_not_limited_by_viewer_window(state):
    """界面的 300 条窗口不能影响完整历史分页导出。"""
    store, engineer, reviewer, _ = state  # 当前测试身份。
    for index in range(321):  # 超过 UI 窗口的一组真实提交。
        send(store, engineer, client_id=f"history-{index}")
    assert len(store.snapshot(engineer.project_id)["messages"]) == 300
    first = store.snapshot(engineer.project_id, after=0)["messages"]  # 首批历史。
    second = store.snapshot(engineer.project_id, after=first[-1]["id"])["messages"]  # 其余历史。
    assert len(first) + len(second) == 321
    assert len(store.read(reviewer, limit=200)["messages"]) == 200


def test_database_reopen_preserves_state_and_unknown_schema_is_rejected(state):
    """重建 Store 后消息与租约仍在，未来表结构必须拒绝写入。"""
    store, engineer, reviewer, _ = state  # 第一个 Store 实例。
    send(store, engineer)
    store.reserve(engineer, ["app.py"])
    reopened = Store(store.path, store.master)  # 重启后的 Store。
    assert reopened.read(reviewer)["messages"][0]["body"] == "中文协作消息"
    assert reopened.members(engineer)["reservations"][0]["owner"] == "Engineer"
    with store.connection(write=True) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="版本较新"):
        Store(store.path, store.master)
