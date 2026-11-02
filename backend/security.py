"""本机连接密钥保护；Windows 使用当前用户 DPAPI 和独立 NTFS 访问控制。"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

if os.name == "nt":
    import ntsecuritycon
    import win32api
    import win32con
    import win32crypt
    import win32security


def private_directory(path: Path) -> None:
    """创建并限制 path 目录为当前用户与 SYSTEM 可访问；失败时停止，不降级为公开目录。"""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        process_token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )  # 当前安全令牌。
        try:
            user_sid = win32security.GetTokenInformation(process_token, win32security.TokenUser)[
                0
            ]  # 当前用户 SID。
        finally:
            process_token.Close()
        system_sid = win32security.CreateWellKnownSid(
            win32security.WinLocalSystemSid, None
        )  # 系统维护身份。
        dacl = win32security.ACL()  # 不继承上级的宽松权限。
        flags = (
            win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
        )  # 子文件及目录继承保护。
        for sid in (user_sid, system_sid):  # 仅这两个身份获得完全控制。
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, sid
            )
        descriptor = win32security.SECURITY_DESCRIPTOR()  # 受保护的文件安全描述符。
        descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(
            str(path),
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            descriptor,
        )
    else:
        path.chmod(0o700)


def atomic_json(path: Path, value: Any) -> None:
    """将 value 编码后原子替换 path；不会产生半个 JSON 文件。"""
    temporary = path.with_name(
        path.name + "." + secrets.token_hex(6) + ".pending"
    )  # 各线程独立的同目录临时文件。
    with temporary.open("w", encoding="utf-8") as stream:  # 完整写入后再公开新状态。
        stream.write(json.dumps(value, ensure_ascii=False, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def protect(path: Path, value: dict[str, Any]) -> None:
    """把 value 保存到 path；Windows 密文仅当前登录用户能解密。"""
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")  # 加密前只存在内存的数据。
    if os.name == "nt":
        encoded = win32crypt.CryptProtectData(encoded, "XiaoguaiCollab", None, None, None, 0)
    temporary = path.with_name(
        path.name + "." + secrets.token_hex(6) + ".pending"
    )  # 原子写入用独立临时文件。
    with temporary.open("wb") as stream:  # 先确保密钥落盘，避免数据库已有消息而重启后密钥丢失。
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)


def unprotect(path: Path) -> dict[str, Any]:
    """读取 path 的用户密文；损坏或跨用户使用时报错，不静默换密钥。"""
    encoded = path.read_bytes()  # 待读取的持久化密文。
    if os.name == "nt":
        encoded = win32crypt.CryptUnprotectData(encoded, None, None, None, 0)[1]
    return json.loads(encoded.decode("utf-8"))


def master_key(directory: Path) -> str:
    """读取或首次创建 directory 下的管理密钥；返回值禁止写入日志。"""
    private_directory(directory)
    path = directory / "admin.dpapi"  # 当前用户绑定的本机密钥文件。
    if not path.exists():
        protect(path, {"key": secrets.token_urlsafe(48)})
    return unprotect(path)["key"]
