"""测试只使用临时目录与本机子进程，不接触用户项目或已有 MCP 设置。"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from backend.security import unprotect


@pytest.fixture
def anyio_backend() -> str:
    """固定为产品实际使用的 asyncio 后端，避免依赖未安装的 Trio。"""
    return "asyncio"


class LiveService:
    """临时本机服务；directory 是测试数据目录。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory  # 仅用于本轮测试的数据目录。
        self.process: subprocess.Popen | None = None  # 明确持有测试自有进程。
        self.url = ""  # 绑定后才赋值，禁止猜端口。
        self.key = ""  # 只在内存中保存的管理令牌。
        self.output = None  # 使用文件收集日志，避免多个错误堆栈填满无人读取的 PIPE 阻塞服务。
        self.opener = build_opener(ProxyHandler({}))  # 回环请求不得经过环境代理。
        self.start()

    def start(self) -> None:
        """启动同一个数据目录的服务，验证活进程及实际健康响应。"""
        root = Path(__file__).resolve().parents[1]  # 当前项目根目录。
        self.output = (self.directory.parent / "service-test.log").open(
            "w+b"
        )  # 每次真实进程启动的独立日志。
        instance = (
            uuid.uuid4().hex
        )  # Windows 虚拟环境启动器可能另起真实 Python PID，使用启动随机标识而非父 PID 识别实例。
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(root / "main.py"),
                "serve",
                "--data",
                str(self.directory),
                "--instance",
                instance,
            ],
            stdout=self.output,
            stderr=self.output,
        )
        deadline = time.monotonic() + 20  # 有界等待服务启动。
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.output.seek(0)
                detail = self.output.read().decode(
                    "utf-8", errors="replace"
                )  # 仅失败时读取本测试的服务日志。
                self.close()
                raise AssertionError(detail)
            state_path = self.directory / "state.json"  # 原子发布的端口信息。
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))  # 本次启动状态。
                if state["instance"] == instance:
                    self.url = f"http://127.0.0.1:{state['port']}"
                    try:
                        with self.opener.open(self.url + "/health", timeout=0.5) as response:
                            if json.load(response)["status"] == "ok":
                                self.key = unprotect(self.directory / "admin.dpapi")["key"]
                                return
                    except (URLError, TimeoutError):
                        pass
            time.sleep(0.05)
        self.close()
        raise AssertionError("服务未在 20 秒内变为健康状态")

    def request(self, path: str, value=None, key: str | None = None, headers: dict | None = None):
        """向 path 发送真实 HTTP；value 非空时 POST；key 默认使用管理令牌。"""
        metadata = {
            "Authorization": "Bearer " + (self.key if key is None else key),
            "Content-Type": "application/json",
        }  # 不会打印请求头。
        metadata.update(headers or {})
        request = Request(
            self.url + path,
            data=json.dumps(value).encode("utf-8") if value is not None else None,
            headers=metadata,
        )  # 实际套接字请求。
        with self.opener.open(request, timeout=20) as response:
            return json.load(response)

    def action(self, op: str, **arguments):
        """调用管理 op，arguments 为对应操作参数。"""
        return self.request("/api/action", {"op": op, **arguments})

    def close(self) -> None:
        """只关闭本测试持有的服务，异常时才结束该 Popen。"""
        if not self.process:
            return
        if self.process.poll() is None:
            try:
                self.action("shutdown")
                self.process.wait(timeout=12)
            except (URLError, TimeoutError, ValueError, subprocess.TimeoutExpired):
                if os.name == "nt" and self.process.poll() is None:
                    # Windows venv 启动器可能有真实解释器子进程，失败清理必须包含自己的进程树。
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(self.process.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=8,
                        check=False,
                    )
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=5)
        self.process.communicate(timeout=5)
        if self.output:
            self.output.close()


@pytest.fixture
def live(tmp_path: Path):
    """每个测试启动独立后端，确保不存在前一个测试留下的假成功记录。"""
    service = LiveService(tmp_path / "state")  # 当前测试的真实本机服务。
    try:
        yield service
    finally:
        service.close()
