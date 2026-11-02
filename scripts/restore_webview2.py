"""从固定官方来源恢复 WebView2 SDK，先验 SHA512，不把编译依赖放进源码历史。"""

from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]  # 独立源码仓库根目录。


def restore() -> None:
    """恢复锁定版本的三个编译文件和上游许可；不安装运行时或改变系统设置。"""
    destination = ROOT / "native/third_party/webview2"  # 依赖恢复范围固定在本仓库。
    lock = json.loads(
        (destination / "SDK.lock.json").read_text(encoding="utf-8")
    )  # 已审查的官方版本与摘要。
    source = urlsplit(lock["source"])  # 只从固定 NuGet 域名走 HTTPS，不接受任意地址。
    if source.scheme != "https" or source.hostname != "api.nuget.org" or source.username:
        raise ValueError("SDK 来源必须是官方 NuGet HTTPS 地址")
    package = (
        ROOT / ".build/downloads" / f"webview2-{lock['version']}.nupkg"
    )  # 只缓存公开依赖，不保存凭据。
    package.parent.mkdir(parents=True, exist_ok=True)
    if not package.exists():
        pending = package.with_suffix(".pending")  # 下载失败不留可被误认成成功缓存的文件。
        with (
            urllib.request.urlopen(lock["source"], timeout=60) as response,
            pending.open("wb") as stream,
        ):
            shutil.copyfileobj(response, stream)
        if hashlib.sha512(pending.read_bytes()).hexdigest() != lock["sha512"]:
            raise ValueError("SDK SHA512 不匹配，未安装，未绕过校验")
        pending.replace(package)
    if hashlib.sha512(package.read_bytes()).hexdigest() != lock["sha512"]:
        raise ValueError("SDK 缓存摘要不匹配，请人工检查缓存；不会自动重用错误内容")
    files = {
        "build/native/include/WebView2.h": "include/WebView2.h",
        "build/native/include/WebView2EnvironmentOptions.h": "include/WebView2EnvironmentOptions.h",
        "build/native/x64/WebView2LoaderStatic.lib": "lib/WebView2LoaderStatic.lib",
        "LICENSE.txt": "LICENSE.txt",
    }  # 显式映射文件，绝不对不可信压缩包执行 extractall。
    with zipfile.ZipFile(package) as archive:
        for member, relative in files.items():
            target = destination / relative  # 固定目标不包含压缩包提供的任意路径。
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
    print(f"WebView2 SDK {lock['version']} 已校验并恢复")


if __name__ == "__main__":
    restore()
