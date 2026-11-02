"""通过官方 MCP 客户端操作真实原生窗口；不使用鼠标、坐标、UIA 或界面替身。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid
from pathlib import Path

from mcp import Client, StdioServerParameters

# 隐藏宿主创建真实 WebView2/DOM 控件和正常消息循环，所有数据都放在独立测试目录。
ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT


async def exercise(data: Path, project: Path) -> dict:
    """通过 data 下的管理 MCP 配置操作真实 project 测试窗口，返回已检查的事实。"""
    configuration = json.loads(
        (data / "connections/desktop-ui.mcp.json").read_text(encoding="utf-8")
    )["mcpServers"]["xiaoguai_ui"]  # 原样使用程序实际导出的配置，不读取任何凭据。
    checks = []  # 每项都在对应的真实结果断言成功后才记录。
    async with Client(
        StdioServerParameters(command=configuration["command"], args=configuration["args"])
    ) as client:

        async def tool(name: str, arguments: dict) -> dict:
            """调用 name/arguments，拒绝把 MCP 错误或未知执行状态当成功。"""
            result = await client.call_tool(name, arguments)  # 标准 MCP JSON-RPC 工具调用。
            if result.is_error:
                raise AssertionError(
                    "MCP 工具错误："
                    + name
                    + " "
                    + " ".join(getattr(block, "text", "") for block in result.content)
                )
            return result.structured_content

        async def operation(
            name: str, arguments: dict, request_id: str | None = None, expect_failure: bool = False
        ) -> dict:
            """提交 name/arguments 后等待真实回执；request_id 可验证重试，expect_failure 校验拒绝路径。"""
            ticket = await tool(
                name, {**arguments, "request_id": request_id or uuid.uuid4().hex}
            )  # 变更必须有明确去重键。
            result = await tool(
                "ui_wait_operation", {"operation_id": ticket["operation_id"], "wait_seconds": 20}
            )
            assert result["status"] == ("failed" if expect_failure else "completed"), result
            return result.get("result", {})

        async def state() -> dict:
            """读回原生控件状态，而非仅查询后端数据库。"""
            result = await tool("ui_get_state", {"message_limit": 10})
            assert result["status"] == "completed", result
            return result["result"]

        current = await state()  # 初始窗口真正显示的项目与字段。
        assert Path(current["fields"]["project_path"]).resolve() == project.resolve()
        assert not current["window_visible"] and len(current["participants"]) == 3
        assert current["renderer"] == "webview2" and current["security"]["local_scripts_only"]
        checks.append("真实 WebView2 窗口通过 MCP 读回 DOM 控件、字段与参与者")
        await operation("ui_select_tab", {"main": "agents"})
        await operation(
            "ui_set_fields",
            {"fields": {"agent_name": "MCPTester", "agent_role": "界面 MCP 原生集成测试"}},
        )
        await operation("ui_invoke", {"action": "register_agent"})
        current = await state()
        assert current["main_tab"] == "agents" and any(
            agent["name"] == "MCPTester" for agent in current["participants"]
        )
        checks.append("切换 Tab、填写字段、登记参与者走真实界面业务入口")
        await operation("ui_select_agent", {"name": "MCPTester"})
        await operation("ui_select_tab", {"main": "connection"})
        await operation("ui_invoke", {"action": "export_agent_config"})
        await operation("ui_invoke", {"action": "export_ui_config"})
        current = await state()
        assert (
            current["selected_agent"] == "MCPTester"
            and current["controls"]["export_ui_config"]["visible_on_active_tab"]
        )
        assert (data / f"connections/{current['project_id']}_MCPTester.mcp.json").is_file()
        checks.append("选择身份及两类配置导出均通过真实原生控件执行")
        original_subject = current["fields"]["subject"]  # 批量预校验前的真实字段。
        await operation(
            "ui_set_fields",
            {"fields": {"subject": "must-not-change", "recipient": "UnknownAgent"}},
            expect_failure=True,
        )
        assert (await state())["fields"]["subject"] == original_subject
        for name, arguments in (
            ("ui_set_fields", {"fields": {"allow_write": True}}),
            ("ui_invoke", {"action": "start_collaboration"}),
        ):
            rejected = await client.call_tool(
                name, {**arguments, "request_id": uuid.uuid4().hex}
            )  # 不触发真实模型或写权限。
            assert rejected.is_error
        checks.append("整批参数预校验失败不留部分修改；危险操作缺少确认被拒绝")
        await operation("ui_set_fields", {"fields": {"body": ""}})
        await operation(
            "ui_invoke",
            {"action": "start_collaboration", "confirm_start": True},
            expect_failure=True,
        )  # 实际到达后端的启动校验，空目标在启动 CLI/模型前被拒绝。
        assert (await state())["run"].get("status", "idle") != "running"
        checks.append("网页启动动作实际贯通后端；空目标被拒绝且未消耗模型额度")
        other = project / "second-project"  # 测试目录草稿在轮询中不会被覆盖。
        other.mkdir()
        await operation("ui_set_fields", {"fields": {"project_path": str(other)}})
        assert Path((await state())["fields"]["project_path"]).resolve() == other.resolve()
        await operation("ui_invoke", {"action": "open_project"})
        assert Path((await state())["fields"]["project_path"]).resolve() == other.resolve()
        await operation("ui_set_fields", {"fields": {"project_path": str(project)}})
        await operation("ui_invoke", {"action": "open_project"})
        checks.append("项目目录草稿跨快照保留，实际打开与切回项目成功")
        await operation("ui_select_tab", {"main": "collaboration", "section": "execution"})
        await operation(
            "ui_set_fields",
            {
                "fields": {
                    "thread": "ui-mcp-native",
                    "recipient": "Engineer + Reviewer",
                    "subject": "真实 MCP 界面集成验收",
                    "body": "这是脚本通过管理 MCP 操作真实网页发送的协议测试消息，不是 AI 生成的对话。<img src=x onerror=alert(1)>\n```python\nprint('<safe>')\n```",
                    "allow_write": False,
                }
            },
        )
        request_id = uuid.uuid4().hex  # 同一业务编号原样重试只能发送一次。
        await operation("ui_invoke", {"action": "send_message"}, request_id=request_id)
        await operation("ui_invoke", {"action": "send_message"}, request_id=request_id)
        current = await state()
        assert len(current["messages"]) == 1 and current["messages"][0]["sender"] == "Operator"
        assert current["security"]["untrusted_message_markup"] == 0
        assert current["security"]["code_block_count"] == 1
        checks.append("代码围栏渲染为代码区，消息中的 HTML 保持纯文本且不创建可执行元素")
        assert {delivery["recipient"] for delivery in current["messages"][0]["deliveries"]} == {
            "Engineer",
            "Reviewer",
        }
        await operation("ui_select_tab", {"main": "agents"})
        await operation("ui_select_tab", {"main": "collaboration", "section": "execution"})
        assert (await state())["fields"]["thread"] == "ui-mcp-native"
        checks.append("真实按钮定向投递与去重成功；跨 Tab 保留输入")
        filename = "ui-mcp-native-" + uuid.uuid4().hex + ".jsonl"  # 不覆盖任何已有导出文件。
        await operation("ui_invoke", {"action": "export_history", "export_filename": filename})
        exported = (ROOT / "exports" / filename).read_text(encoding="utf-8")
        assert len(exported.splitlines()) == 1 and json.loads(exported)["thread"] == "ui-mcp-native"
        await operation(
            "ui_invoke",
            {"action": "export_history", "export_filename": filename},
            expect_failure=True,
        )
        await operation(
            "ui_invoke",
            {"action": "export_history", "export_filename": "../outside.jsonl"},
            expect_failure=True,
        )
        stopped = await operation("ui_invoke", {"action": "stop_collaboration"})
        assert stopped["already_stopped"]
        checks.append("完整历史真实导出，拒绝覆盖/越界；停止操作幂等")
        # 人数变化贯通真实 DOM、原生桥和打包后端，准备身份不调用模型，也不伪造对话。
        for count in (4, 5, 20, 50):
            await operation(
                "ui_set_fields", {"fields": {"ai_count": count, "rounds": 3, "concurrency": 4}}
            )
            await operation("ui_invoke", {"action": "prepare_team"})
            current = await state()
            assert current["fields"]["ai_count"] == count
            assert (
                len(current["participants"]) == count + 2
            )  # Operator 与 MCPTester 不计入模板 AI 数。
            assert current["run"].get("status") != "running"
            assert (
                not current["layout"]["horizontal_overflow"]
                and not current["layout"]["active_page_overflow"]
            )
        await operation("ui_select_tab", {"main": "agents"})
        current = await state()
        assert any(item["name"] == "Agent50" for item in current["participants"])
        await operation("ui_select_agent", {"name": "Agent50"})
        await operation("ui_select_tab", {"main": "connection"})
        await operation("ui_invoke", {"action": "export_agent_config"})
        assert (data / f"connections/{current['project_id']}_Agent50.mcp.json").is_file()
        await operation("ui_select_tab", {"main": "collaboration", "section": "execution"})
        await operation("ui_set_fields", {"fields": {"ai_count": 2, "rounds": 2, "concurrency": 1}})
        checks.append("4/5/20/50 人准备、整数配置读回、50 号身份选择与导出贯通真实界面；未调用模型")
        return {
            "passed": True,
            "real_native_window": True,
            "transport": "official MCP client -> packaged stdio -> management MCP -> actual WebView2 DOM controls",
            "checks": checks,
            "model_started": False,
            "team_sizes_checked": [4, 5, 20, 50],
            "exported_file": str(ROOT / "exports" / filename),
        }


def main() -> None:
    """启动有独立数据目录的真实 MFC 宿主，执行 MCP 验收后只回收这个测试进程树。"""
    base = WORKSPACE / ".build/checks" / ("ui-mcp-" + uuid.uuid4().hex)  # 不依赖任何先前测试数据。
    data = base / "state"  # 测试后端数据。
    project = base / "project"  # 独立空测试项目。
    project.mkdir(parents=True)
    process = subprocess.Popen(
        [str(ROOT / "bin/XiaoguaiCollab.exe"), "--ui-test-host", str(data), str(project)]
    )  # 正常消息循环，但不显示窗口。
    try:
        deadline = time.monotonic() + 30  # 等候实际宿主发布管理配置。
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("原生测试宿主提前退出")
            if (data / "connections/desktop-ui.mcp.json").is_file() and (
                data / "state.json"
            ).is_file():
                time.sleep(1.2)  # 仅启动阶段等待首次原生心跳，不重试任何写操作。
                break
            time.sleep(0.05)
        else:
            raise AssertionError("原生宿主启动超时")
        result = asyncio.run(exercise(data, project))  # 全程只通过公开的 MCP 配置操作界面。
        (ROOT / "verification/ui-mcp-native.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(
                timeout=10
            )  # Job Object 自动回收这个宿主自己的后端，不按名称清理其他程序。


if __name__ == "__main__":
    main()
