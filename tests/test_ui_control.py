"""管理 MCP 的队列、去重、未知结果及权限边界；原生窗口另有真实集成验收。"""

from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from backend.security import unprotect
from backend.ui_control import UIBroker, UIFields


@pytest.mark.anyio
async def test_ui_command_is_not_success_until_native_result():
    """超时只代表没有观察到结果，不取消、不自动重发正在执行的命令。"""
    broker = UIBroker()  # 单独的纯队列测试，不冒充真实原生窗口。
    with pytest.raises(ValueError, match="原生窗口"):
        broker.enqueue("set_fields", {"fields": {"body": "test"}}, "request-1")
    assert broker.next_command()["command"] is None
    first = broker.enqueue(
        "set_fields", {"fields": {"body": "test"}}, "request-1"
    )  # 首次业务请求。
    assert first["status"] == "queued"
    assert broker.enqueue("set_fields", {"fields": {"body": "test"}}, "request-1") == first
    assert len(broker.pending) == 1
    command = broker.next_command()["command"]  # 模拟传输领取，仅用于队列契约。
    assert command["id"] == "request-1"
    assert (await broker.wait("request-1", 0.01))["status"] == "executing"
    assert broker.next_command()["command"] is None
    broker.complete("request-1", {"actual": True})
    assert (await broker.wait("request-1", 0))["result"] == {"actual": True}
    assert (
        broker.enqueue("set_fields", {"fields": {"body": "test"}}, "request-1")["status"]
        == "completed"
    )
    with pytest.raises(ValueError, match="不同操作"):
        broker.enqueue("set_fields", {"fields": {"body": "changed"}}, "request-1")
    with pytest.raises(ValueError):
        broker.complete("request-1", {"actual": False})


@pytest.mark.anyio
async def test_ui_queue_is_bounded_and_errors_are_not_success():
    """有界队列不覆盖未知结果，失败也不能被标为成功。"""
    broker = UIBroker()  # 独立队列。
    broker.next_command()
    for index in range(32):  # 达到明确队列上限。
        broker.enqueue("invoke", {"action": "send_message"}, f"request-{index}")
    with pytest.raises(ValueError, match="队列已满"):
        broker.enqueue("invoke", {}, "overflow")
    broker.next_command()
    broker.complete("request-0", {}, "backend rejected")
    assert (await broker.wait("request-0", 0))["status"] == "failed"
    with pytest.raises(ValueError, match="未知"):
        await broker.wait("unknown", 0)


def test_ui_fields_reject_arbitrary_windows_and_invalid_flags():
    """不允许借字段模型注入 HWND、系统消息或命令行。"""
    for value in (
        {"hwnd": 123},
        {"command": "cmd.exe"},
        {"allow_write": "yes"},
        {"body": "x" * 8001},
        {"ai_count": 51},
        {"ai_count": True},
        {"ai_count": "50"},
        {"rounds": 4},
        {"concurrency": 5},
    ):
        with pytest.raises(ValidationError):
            UIFields(**value)


@pytest.mark.anyio
async def test_ui_mcp_is_separate_from_project_identity(live, tmp_path):
    """实际 HTTP MCP：普通项目凭据不能读管理界面，管理端能发现工具但不会假装窗口在线。"""
    project = live.action("project", root=str(tmp_path))  # 原有项目隔离保持不变。
    profile = live.action(
        "profile", project_id=project["id"], name="Engineer"
    )  # 仅测试内存使用的项目身份。
    credential = unprotect(Path(profile["directory"]) / (profile["prefix"] + ".dpapi"))["token"]
    async with httpx2.AsyncClient(trust_env=False) as http:
        denied = await http.post(
            live.url + "/ui/mcp",
            headers={"Authorization": "Bearer " + credential},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert denied.status_code == 401
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + live.key}, trust_env=False
        ) as http,
        Client(streamable_http_client(live.url + "/ui/mcp", http_client=http)) as client,
    ):
        tools = await client.list_tools()  # 真实管理 MCP 工具发现。
        assert {tool.name for tool in tools.tools} == {
            "ui_get_state",
            "ui_set_fields",
            "ui_select_tab",
            "ui_select_agent",
            "ui_invoke",
            "ui_wait_operation",
        }
        state = await client.call_tool(
            "ui_get_state", {}
        )  # 此 fixture 没有原生窗口，不能伪造 UI 状态。
        assert state.is_error
        write = await client.call_tool(
            "ui_set_fields",
            {"fields": {"allow_write": True}, "request_id": "write-without-confirm"},
        )
        assert write.is_error
        start = await client.call_tool(
            "ui_invoke", {"action": "start_collaboration", "request_id": "start-without-confirm"}
        )
        assert start.is_error


@pytest.mark.anyio
async def test_ui_mcp_real_transport_waits_for_frontend_receipt(live):
    """协议级替身验证队列传输；本测试不声称已经操作真实 MFC 窗口。"""
    live.request("/api/ui/next")  # 明确模拟一次前端心跳。
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + live.key}, trust_env=False
        ) as http,
        Client(streamable_http_client(live.url + "/ui/mcp", http_client=http)) as client,
    ):
        queued = await client.call_tool(
            "ui_select_tab", {"main": "agents", "request_id": "select-agents"}
        )
        assert queued.structured_content["status"] == "queued"
        command = live.request("/api/ui/next")["command"]  # 实际 HTTP 管理通道领取。
        assert command["id"] == "select-agents" and command["operation"] == "select_tab"
        pending = await client.call_tool(
            "ui_wait_operation", {"operation_id": "select-agents", "wait_seconds": 0}
        )
        assert pending.structured_content["status"] == "executing"
        live.request(
            "/api/ui/result",
            {"operation_id": "select-agents", "result": {"state": {"main_tab": "agents"}}},
        )
        finished = await client.call_tool("ui_wait_operation", {"operation_id": "select-agents"})
        assert finished.structured_content["status"] == "completed"
        assert finished.structured_content["result"]["state"]["main_tab"] == "agents"
        repeated = await client.call_tool(
            "ui_select_tab", {"main": "agents", "request_id": "select-agents"}
        )
        assert repeated.structured_content["status"] == "completed"
        assert live.request("/api/ui/next")["command"] is None
