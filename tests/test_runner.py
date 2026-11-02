"""执行器失败与取消边界；模型通信正向路径由真实四回合验收另外证明。"""

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from backend.runner import Runner
from backend.store import Store


def make_runner(tmp_path: Path) -> Runner:
    """创建不启动模型的临时执行器，返回真实持久化实例。"""
    store = Store(tmp_path / "db.sqlite3", "test-only-master")  # 独立测试存储。
    return Runner(store, lambda *_: {}, tmp_path / "runs")


def test_running_record_after_process_restart_is_interrupted(tmp_path):
    """磁盘上的 running 不能作为进程仍存活的证据，也不能自动重启消耗额度。"""
    directory = tmp_path / "runs"  # 故障前的记录目录。
    directory.mkdir()
    (directory / "latest.json").write_text(
        json.dumps({"id": "crashed", "status": "running", "stage": "旧状态", "events": []}),
        encoding="utf-8",
    )
    runner = make_runner(tmp_path)  # 模拟新进程读取旧记录。
    assert runner.snapshot()["status"] == "interrupted"
    assert runner.worker is None
    assert runner.processes == {}


def test_missing_cli_does_not_start_a_fake_success(tmp_path, monkeypatch):
    """未安装 CLI 时清晰失败，而不是填充模拟 AI 消息。"""
    runner = make_runner(tmp_path)  # 当前测试执行器。
    monkeypatch.setattr("backend.runner.find_codex", lambda: None)
    with pytest.raises(ValueError, match="未检测到"):
        runner.start("project", "测试目标")
    assert runner.snapshot()["status"] == "idle"
    assert runner.worker is None


def test_close_requests_cancel_and_joins_its_worker(tmp_path):
    """关闭时取消并等待真实线程，不遗留隐藏工作。"""
    runner = make_runner(tmp_path)  # 当前测试执行器。
    runner.worker = threading.Thread(target=runner.cancel.wait, daemon=False)
    runner.worker.start()
    runner.close()
    assert runner.cancel.is_set()
    assert not runner.worker.is_alive()


def test_terminate_ends_only_the_owned_live_process(tmp_path):
    """直接用实际子进程证明取消回收；不使用模拟 Popen 证明进程生命周期。"""
    runner = make_runner(tmp_path)  # 当前测试执行器。
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )  # 测试独占的有界睡眠进程。
    try:
        runner._terminate(process)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
