"""真实套接字、官方 SDK 客户端与独立 stdio 进程端到端验收。"""

import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.error import HTTPError

import anyio
import httpx2
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from backend.security import unprotect


@asynccontextmanager
async def client_for(live, project_id: str, name: str):
    """使用项目身份名创建独立 HTTP MCP 客户端，令牌从当前用户密文读取。"""
    profile = live.action("profile", project_id=project_id, name=name)  # 无明文密钥的配置结果。
    credential = unprotect(Path(profile["directory"]) / (profile["prefix"] + ".dpapi"))[
        "token"
    ]  # 仅测试内存使用。
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + credential},
            trust_env=False,
            timeout=20,
        ) as http,
        Client(streamable_http_client(live.url + "/mcp", http_client=http)) as client,
    ):
        yield client


@pytest.mark.anyio
async def test_two_independent_http_mcp_clients_exchange_and_ack(live, tmp_path):
    """双客户端真实协商协议、列工具、互发中文并确认；不使用内存直连替身。"""
    project = live.action("project", root=str(tmp_path))  # 实际临时项目。
    async with (
        client_for(live, project["id"], "Engineer") as engineer,
        client_for(live, project["id"], "Reviewer") as reviewer,
    ):
        assert engineer.protocol_version == "2026-07-28"
        tools = await engineer.list_tools()  # 从真实服务发现的工具表。
        assert {tool.name for tool in tools.tools} == {
            "collab_whoami",
            "collab_members",
            "collab_send",
            "collab_inbox",
            "collab_thread",
            "collab_ack",
            "collab_reserve",
        }
        who = await engineer.call_tool("collab_whoami", {})  # 绑定发送者身份。
        assert who.structured_content["name"] == "Engineer"
        arguments = {
            "recipients": ["Reviewer"],
            "thread": "e2e",
            "kind": "review",
            "subject": "请审查",
            "body": "中文正文与 emoji 🛠",
            "client_id": "e2e-first",
        }  # 真正待投递的业务消息。
        sent = await engineer.call_tool("collab_send", arguments)  # 首次发送。
        replay = await engineer.call_tool("collab_send", arguments)  # 原样重试。
        assert sent.structured_content["id"] == replay.structured_content["id"]
        assert replay.structured_content["deduplicated"] is True
        inbox = await reviewer.call_tool("collab_inbox", {})  # 审查员收件箱。
        assert len(inbox.structured_content["messages"]) == 1
        assert inbox.structured_content["messages"][0]["body"] == arguments["body"]
        ack = await reviewer.call_tool(
            "collab_ack", {"message_id": sent.structured_content["id"]}
        )  # 接收者真实确认。
        assert ack.structured_content["acknowledged"]
        reply = await reviewer.call_tool(
            "collab_send",
            {
                **arguments,
                "recipients": ["Engineer"],
                "body": "请补空输入测试",
                "client_id": "reply-1",
            },
        )  # 反向回复。
        assert not reply.is_error
        received = await engineer.call_tool("collab_inbox", {})  # 工程师收到独立身份发来的消息。
        assert received.structured_content["messages"][0]["sender"] == "Reviewer"


@pytest.mark.anyio
async def test_two_stdio_bridge_processes_use_shared_http_store(live, tmp_path):
    """两个独立 stdio 服务进程互通，而非各自保存隔离的进程内状态。"""
    project = live.action("project", root=str(tmp_path))  # 共享项目。
    first = live.action("profile", project_id=project["id"], name="Engineer")[
        "configuration"
    ]  # 第一身份配置。
    second = live.action("profile", project_id=project["id"], name="Reviewer")[
        "configuration"
    ]  # 第二身份配置。
    async with (
        Client(StdioServerParameters(command=first["command"], args=first["args"])) as engineer,
        Client(StdioServerParameters(command=second["command"], args=second["args"])) as reviewer,
    ):
        sent = await engineer.call_tool(
            "collab_send",
            {
                "recipients": ["Reviewer"],
                "thread": "stdio-real",
                "kind": "plan",
                "subject": "独立进程",
                "body": "请检查共享数据库中的消息",
                "client_id": "stdio-1",
            },
        )  # 经双层真实 MCP 发送。
        assert not sent.is_error
        inbox = await reviewer.call_tool("collab_inbox", {})  # 另一个进程从共享后端读到。
        assert inbox.structured_content["messages"][0]["sender"] == "Engineer"
        assert inbox.structured_content["messages"][0]["thread"] == "stdio-real"


@pytest.mark.anyio
async def test_mcp_cross_project_read_and_invalid_ack_fail(live, tmp_path):
    """即便名称相同，另一项目的身份也不能看见话题或确认消息。"""
    first = live.action("project", root=str(tmp_path))  # 第一个项目。
    other = tmp_path / "other"  # 第二个项目目录。
    other.mkdir()
    second = live.action("project", root=str(other))  # 第二个项目。
    live.action(
        "send",
        project_id=first["id"],
        recipients=["Reviewer"],
        thread="secret-thread",
        kind="task",
        subject="项目内消息",
        body="不能跨项目读取",
        client_id="isolated",
    )
    async with client_for(live, second["id"], "Reviewer") as client:
        result = await client.call_tool(
            "collab_thread", {"thread": "secret-thread"}
        )  # 同名话题的跨项目读取。
        assert result.structured_content["messages"] == []
        denied = await client.call_tool("collab_ack", {"message_id": 1})  # 跨项目确认。
        assert denied.is_error
        invalid = await client.call_tool("collab_inbox", {"after": -1})  # SDK 参数验证。
        assert invalid.is_error


def test_security_guards_protect_admin_and_mcp(live):
    """管理令牌不能当 AI 身份；任意来源、Host 和无凭据请求均拒绝。"""
    for path, key, headers, status in (
        ("/api/state", "wrong", {}, 401),
        ("/mcp", None, {}, 401),
        ("/api/state", "é", {}, 401),
        ("/mcp", "x" * 129, {}, 401),
        ("/health", None, {"Origin": "https://attacker.example"}, 403),
        ("/health", None, {"Host": "rebound.example:80"}, 421),
    ):
        with pytest.raises(HTTPError) as raised:
            live.request(path, key=key, headers=headers)
        assert raised.value.code == status


def test_legacy_2025_mcp_handshake_remains_compatible(live, tmp_path):
    """使用旧版 JSON-RPC 初始化、通知、列工具；兼容当前大量桌面 MCP 宿主。"""
    project = live.action("project", root=str(tmp_path))  # 实际临时项目。
    configuration = live.action(
        "profile", project_id=project["id"], name="Engineer"
    )  # 当前用户密文路径。
    credential = unprotect(Path(configuration["directory"]) / (configuration["prefix"] + ".dpapi"))[
        "token"
    ]  # 不打印的连接令牌。
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }  # 旧版客户端请求头。
    result = live.request(
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "legacy-test", "version": "1"},
            },
        },
        key=credential,
        headers=headers,
    )  # 实际旧版握手。
    assert result["result"]["protocolVersion"] == "2025-11-25"
    tools = live.request(
        "/mcp",
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        key=credential,
        headers=headers,
    )  # 旧协议后续工具列表。
    assert len(tools["result"]["tools"]) == 7


def test_restart_preserves_messages_and_encrypted_identities(live, tmp_path):
    """服务停止后原数据目录重启，真实 HTTP 消息和加密身份仍然有效。"""
    project = live.action("project", root=str(tmp_path))  # 真实项目。
    sent = live.action(
        "send",
        project_id=project["id"],
        recipients=["Engineer"],
        thread="persist",
        kind="task",
        subject="重启前",
        body="持久化的中文消息",
        client_id="restart-1",
    )  # 重启前提交。
    profile = live.action("profile", project_id=project["id"], name="Engineer")  # 旧身份密文。
    profile_path = Path(profile["directory"]) / (profile["prefix"] + ".dpapi")  # 当前用户密文文件。
    credential = unprotect(profile_path)["token"]  # 重启前的身份令牌。
    assert credential.encode("ascii") not in profile_path.read_bytes()
    live.close()
    live.start()
    snapshot = live.request("/api/state?project=" + project["id"])  # 重启后的管理快照。
    assert snapshot["messages"][0]["id"] == sent["id"]
    assert unprotect(profile_path)["token"] == credential


def test_second_service_cannot_share_the_same_data_directory(live):
    """实际启动第二进程，确认独占锁拒绝重复后端而原服务仍健康。"""
    root = Path(__file__).resolve().parents[1]  # 项目源码根目录。
    result = subprocess.run(
        [sys.executable, str(root / "main.py"), "serve", "--data", str(live.directory)],
        capture_output=True,
        timeout=15,
        check=False,
    )  # 竞争同一数据目录的进程。
    assert result.returncode != 0
    assert live.request("/health")["status"] == "ok"


@pytest.mark.anyio
async def test_long_poll_observes_a_later_message_without_blocking_sender(live, tmp_path):
    """等待收件箱时发件客户端仍能提交，验证事件循环没有被同步等待阻塞。"""
    project = live.action("project", root=str(tmp_path))  # 当前测试项目。
    async with (
        client_for(live, project["id"], "Engineer") as engineer,
        client_for(live, project["id"], "Reviewer") as reviewer,
    ):
        result = {}  # 收件任务写入的真实结果。
        started = anyio.Event()  # 同步收件任务已经准备启动。

        async def receive() -> None:
            """等候最多三秒，并把结构化收件结果放入 result。"""
            started.set()
            result.update(
                (await reviewer.call_tool("collab_inbox", {"wait_seconds": 3})).structured_content
            )

        async with anyio.create_task_group() as group:
            group.start_soon(receive)
            await started.wait()
            await engineer.call_tool(
                "collab_send",
                {
                    "recipients": ["Reviewer"],
                    "thread": "poll",
                    "kind": "note",
                    "subject": "唤醒",
                    "body": "真实后续投递",
                    "client_id": "poll-1",
                },
            )
        assert result["messages"][0]["body"] == "真实后续投递"
