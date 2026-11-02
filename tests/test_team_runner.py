"""多人数计划、并发屏障及取消边界；阶段替身只证明调度，不冒充真实模型。"""

import subprocess
import sys
import threading
import time
from collections import Counter

import pytest

from backend.runner import Runner, team_members
from backend.store import Store


def prepared_runner(tmp_path):
    """创建 tmp_path 中的真实存储、项目及默认身份，不调用模型。"""
    store = Store(tmp_path / "db.sqlite3", "team-test-only-master")  # 独立测试数据库。
    project = store.project(str(tmp_path))  # 此测试自己的工作目录。
    for name in ("Operator", "Engineer", "Reviewer"):  # 默认三身份。
        store.register(project["id"], name, name)
    runner = Runner(store, lambda *_: {}, tmp_path / "runs")  # 阶段执行由各测试显式替换。
    return runner, project


@pytest.mark.parametrize("count", [2, 4, 5, 20, 50])
def test_team_prepare_does_not_call_models_or_overwrite_roles(tmp_path, monkeypatch, count):
    """准备 2/4/5/20/50 人都不启动 CLI，已有角色和其他身份保留。"""
    runner, project = prepared_runner(tmp_path)  # 本测试的真实存储。
    runner.store.register(project["id"], "Engineer", "用户已有的角色")
    runner.store.register(project["id"], "External", "外部客户端")
    monkeypatch.setattr("backend.runner.find_codex", lambda: pytest.fail("准备团队不应调用 CLI"))
    result = runner.prepare(project["id"], count)  # 实际身份登记，不是模拟成功。
    assert not result["model_started"] and len(result["team"]) == count
    assert result["team"][0]["name"] == "Engineer" and result["team"][-1]["name"] == "Reviewer"
    assert result["team"][0]["role"] == "用户已有的角色"
    assert len({item["name"] for item in result["team"]}) == count
    runner.prepare(project["id"], 2)
    assert len(runner.store.snapshot(project["id"])["agents"]) == count + 2
    assert runner.worker is None and runner.processes == {}


@pytest.mark.parametrize(
    "fields",
    [
        {"ai_count": 1},
        {"ai_count": 51},
        {"ai_count": True},
        {"ai_count": 4.5},
        {"rounds": 0},
        {"rounds": 4},
        {"rounds": False},
        {"concurrency": 0},
        {"concurrency": 5},
        {"concurrency": "4"},
        {"allow_write": "yes"},
    ],
)
def test_team_limits_reject_before_start(tmp_path, fields):
    """越界、布尔伪整数与字符串不能隐式变成大规模调用。"""
    runner, project = prepared_runner(tmp_path)  # 尚未启动的真实执行器。
    with pytest.raises(ValueError):
        runner.start(project["id"], "受限测试目标", **fields)
    assert (
        runner.worker is None and runner.processes == {} and runner.snapshot()["status"] == "idle"
    )


@pytest.mark.parametrize("count", [2, 4, 5, 20, 50])
@pytest.mark.parametrize("writable", [False, True])
def test_team_scheduler_is_bounded_and_reviews_after_workers(
    tmp_path, monkeypatch, count, writable
):
    """阶段替身验证真实线程调度：每人每轮一次、只读至多四路、写入串行、审查有屏障。"""
    runner, project = prepared_runner(tmp_path)  # 存储与调度是真实对象，模型阶段是明确替身。
    lock = threading.Lock()  # 保护测试观察到的并发计数。
    active = 0  # 当前替身工作阶段数。
    peak = 0  # 本轮实际线程并发峰值。
    completed = Counter()  # 每轮已结束的工作成员数。
    calls = []  # 实际进入阶段替身的身份、轮次和序号。

    def stage(
        executable,
        directory,
        goal,
        allow_write,
        run_id,
        members,
        member,
        index,
        round_number,
        after,
    ):
        """参数沿用生产阶段契约；只检查调度关系，不生成任何假 AI 回复。"""
        nonlocal active, peak
        with lock:
            if member["name"] == "Reviewer":
                assert active == 0 and completed[round_number] == count - 1
            active += 1
            peak = max(peak, active)
            calls.append((round_number, member["name"], index))
        time.sleep(0.01)  # 只用于让真实测试线程重叠，不模拟模型耗时或重试。
        with lock:
            active -= 1
            if member["name"] != "Reviewer":
                completed[round_number] += 1

    monkeypatch.setattr("backend.runner.find_codex", lambda: "explicit-stage-test-double")
    monkeypatch.setattr(runner, "_execute_stage", stage)
    result = runner.start(
        project["id"], "只验证有界调度", writable, count, 2, 4
    )  # 无真实模型调用。
    runner.worker.join(timeout=10)
    assert not runner.worker.is_alive() and runner.snapshot()["status"] == "finished"
    assert (
        len(calls) == count * 2
        and len({(round_number, name) for round_number, name, _ in calls}) == count * 2
    )
    assert result["planned_calls"] == count * 2
    assert peak <= (1 if writable else 4)
    if not writable and count > 2:
        assert peak >= 2
    assert [item["name"] for item in team_members(count)][-1] == "Reviewer"


def test_member_failure_cancels_queue_without_retry(tmp_path, monkeypatch):
    """一个成员失败即中止同组和未启动队列，不进入审查、不自动重试。"""
    runner, project = prepared_runner(tmp_path)  # 独立真实调度器。
    started = []  # 只记录真正进入替身的成员。

    def stage(*arguments):
        """arguments 对应一次生产阶段；Engineer 失败，其他阶段等候中止信号。"""
        name = arguments[6]["name"]  # 当前实际被调度的身份。
        started.append(name)
        if name == "Engineer":
            raise RuntimeError("明确的测试失败，不能当成功")
        assert runner.abort.wait(2)

    monkeypatch.setattr("backend.runner.find_codex", lambda: "explicit-stage-test-double")
    monkeypatch.setattr(runner, "_execute_stage", stage)
    runner.start(project["id"], "失败边界", False, 50, 3, 4)
    runner.worker.join(timeout=10)
    assert runner.snapshot()["status"] == "failed" and not runner.worker.is_alive()
    assert "Reviewer" not in started and len(started) <= 4 and len(set(started)) == len(started)
    assert runner.processes == {}


def test_stop_cancels_parallel_members_and_pending_rounds(tmp_path, monkeypatch):
    """停止同一活任务后全部线程退出，不继续后续轮次或启动审查。"""
    runner, project = prepared_runner(tmp_path)  # 本测试自己的调度器。
    entered = threading.Event()  # 至少一个阶段真正开始后才请求停止。
    started = []  # 记录启动边界，不把排队当执行。

    def stage(*arguments):
        """当前阶段只等待真实取消事件；不调用模型。"""
        started.append(arguments[6]["name"])
        entered.set()
        assert runner.cancel.wait(3)

    monkeypatch.setattr("backend.runner.find_codex", lambda: "explicit-stage-test-double")
    monkeypatch.setattr(runner, "_execute_stage", stage)
    runner.start(project["id"], "取消边界", False, 50, 3, 4)
    assert entered.wait(2)
    runner.stop()
    runner.worker.join(timeout=10)
    assert runner.snapshot()["status"] == "stopped" and not runner.worker.is_alive()
    assert "Reviewer" not in started and len(started) <= 4 and runner.processes == {}


def test_parallel_stop_reaps_owned_cli_test_processes(tmp_path, monkeypatch):
    """用四个真实 Python 子进程验证并行回收；这些是明确的进程替身，不冒充模型或 MCP。"""
    runner, project = prepared_runner(tmp_path)  # 调度与管道清理使用生产代码。
    original_popen = subprocess.Popen  # 保留真正的进程创建器，taskkill 等生命周期命令不被替换。
    children = []  # 本测试独占的睡眠子进程。

    def launch(arguments, **options):
        """仅把明确测试 CLI 替换成读取 stdin 后睡眠的真实进程，其他 arguments 原样执行。"""
        if arguments[0] == "explicit-process-test-double":
            child = original_popen(
                [sys.executable, "-u", "-c", "import sys,time; sys.stdin.read(); time.sleep(60)"],
                **options,
            )  # 没有模型输出或协议模拟。
            children.append(child)
            return child
        return original_popen(arguments, **options)

    monkeypatch.setattr("backend.runner.find_codex", lambda: "explicit-process-test-double")
    monkeypatch.setattr("backend.runner.subprocess.Popen", launch)
    try:
        runner.start(project["id"], "真实进程取消测试，不调用模型", False, 50, 3, 4)
        deadline = time.monotonic() + 8  # 仅观察启动，不重新提交任务。
        while time.monotonic() < deadline:
            with runner.lock:
                count = len(runner.processes)  # 实际已登记的活动进程数。
            if count == 4:
                break
            time.sleep(0.02)
        assert count == 4 and len(children) == 4
        runner.stop()
        runner.worker.join(timeout=15)
        assert not runner.worker.is_alive() and runner.snapshot()["status"] == "stopped"
        assert runner.processes == {} and all(child.poll() is not None for child in children)
    finally:
        runner.close()
        for child in children:  # 只回收本测试拥有的对象，不按进程名称清理其他应用。
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
