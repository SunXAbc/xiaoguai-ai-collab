"""专用界面控制 MCP：命令由原生窗口实际执行，不模拟鼠标或开放任意 Win32 消息。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, StrictBool


class UIFields(BaseModel):
    """窗口中允许编辑的业务字段；额外字段被拒绝，不接受 HWND、命令行或凭据。"""

    model_config = ConfigDict(extra="forbid")  # 不允许通过拼写错误隐式扩大能力。
    project_path: str | None = Field(default=None, max_length=32000)  # 项目目录输入框。
    agent_name: str | None = Field(default=None, max_length=48)  # 参与者名称输入框。
    agent_role: str | None = Field(default=None, max_length=80)  # 参与者分工输入框。
    thread: str | None = Field(default=None, max_length=100)  # 消息话题。
    recipient: str | None = Field(default=None, max_length=100)  # 必须匹配现有下拉选项。
    subject: str | None = Field(default=None, max_length=200)  # 人工消息标题。
    body: str | None = Field(default=None, max_length=8000)  # 任务目标或人工消息。
    allow_write: StrictBool | None = None  # 工程师写权限，开启时另需明确确认。
    ai_count: Annotated[int, Field(strict=True, ge=2, le=50)] | None = (
        None  # 模板内参与的 AI 数，不含 Operator。
    )
    rounds: Annotated[int, Field(strict=True, ge=1, le=3)] | None = None  # 每人最多执行的轮次。
    concurrency: Annotated[int, Field(strict=True, ge=1, le=4)] | None = (
        None  # 只读并发请求；写入模式强制串行。
    )


class UIBroker:
    """事件循环内的有界操作队列；窗口轮询领取，结果只由同一管理身份提交。"""

    def __init__(self) -> None:
        self.operations: dict[str, dict[str, Any]] = {}  # 最近操作及结果，不落盘存储输入。
        self.pending: list[str] = []  # 等待原生窗口领取的操作编号。
        self.last_poll = 0.0  # 最后一次原生窗口心跳，不能以服务存活代替窗口在线。

    def enqueue(self, operation: str, arguments: dict[str, Any], request_id: str) -> dict[str, Any]:
        """以 request_id 幂等提交 operation/arguments；返回排队状态，不冒充执行成功。"""
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id 必须为 1～128 个字符")
        digest = hashlib.sha256(
            json.dumps([operation, arguments], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()  # 原样重试判定。
        previous = self.operations.get(request_id)  # 已提交过的业务操作。
        if previous:
            if previous["digest"] != digest:
                raise ValueError("同一 request_id 不能用于不同操作")
            return self.public(previous)
        if time.monotonic() - self.last_poll > 5:
            raise ValueError("原生窗口尚未连接或已离线；不要自动重复提交写操作")
        if (
            sum(item["status"] in ("queued", "executing") for item in self.operations.values())
            >= 32
        ):
            raise ValueError("界面操作队列已满，请先等待已有操作")
        if len(self.operations) >= 128:
            for key in list(self.operations):  # 只淘汰已完成记录，永不丢弃执行中的未知结果。
                if self.operations[key]["status"] in ("completed", "failed"):
                    del self.operations[key]
                    break
            else:
                raise ValueError("界面操作记录已满")
        record = {
            "operation_id": request_id,
            "operation": operation,
            "arguments": arguments,
            "digest": digest,
            "status": "queued",
            "created": time.monotonic(),
            "event": asyncio.Event(),
        }  # 内部状态含等待事件。
        self.operations[request_id] = record
        self.pending.append(request_id)
        return self.public(record)

    def next_command(self) -> dict[str, Any]:
        """由真实窗口领取一个命令并更新心跳；未领取过的变更不可能已经执行。"""
        self.last_poll = time.monotonic()
        if not self.pending:
            return {"command": None}
        key = self.pending.pop(0)  # 单个命令按提交顺序派发。
        record = self.operations[key]  # 领取后不自动重放，防止窗口崩溃造成重复提交。
        record["status"] = "executing"
        return {
            "command": {
                "id": key,
                "operation": record["operation"],
                "arguments": record["arguments"],
            }
        }

    def complete(
        self, operation_id: str, result: dict[str, Any], error: str = ""
    ) -> dict[str, Any]:
        """接受原生窗口对 operation_id 的 result/error；重复或未领取结果不能覆盖真结果。"""
        record = self.operations.get(operation_id)  # 对应的已领取操作。
        if not record or record["status"] != "executing":
            raise ValueError("操作不存在、尚未领取或已经结束")
        record.update(status="failed" if error else "completed", result=result, error=error)
        record["event"].set()
        return {"accepted": True}

    def public(self, record: dict[str, Any]) -> dict[str, Any]:
        """返回 record 的非敏感状态；不向调用方返回内部摘要、等待事件或私有认证数据。"""
        return {
            key: record[key]
            for key in ("operation_id", "status", "operation", "result", "error")
            if key in record
        }

    async def wait(self, operation_id: str, seconds: float) -> dict[str, Any]:
        """最多等候 seconds 秒；观察超时不取消、不重发正在执行的操作。"""
        record = self.operations.get(operation_id)  # 同一操作在超时后仍可查询。
        if not record:
            raise ValueError("操作未知；服务重启后不会猜测先前结果")
        if record["status"] in ("queued", "executing") and seconds > 0:
            with suppress(TimeoutError):
                await asyncio.wait_for(record["event"].wait(), timeout=seconds)
        return self.public(record)

    async def invoke(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """MCP 调度入口；state 等待读回值，其他动作先返回操作票据供显式等待。"""
        if operation == "wait":
            return await self.wait(arguments["operation_id"], arguments["wait_seconds"])
        if operation == "state":
            key = "read-" + uuid.uuid4().hex  # 读状态也由原生窗口实际回答，不用后端数据假冒界面。
            self.enqueue(operation, arguments, key)
            return await self.wait(key, 10)
        values = dict(arguments)  # 业务参数不能带入 broker 的去重键。
        key = values.pop("request_id")  # 变更操作必须由客户端明确提供去重键。
        return self.enqueue(operation, values, key)


def register_ui_tools(
    server: MCPServer, invoke: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
) -> None:
    """向 server 注册整窗管理工具；invoke 可为本机 broker 或标准 MCP 桥的转发函数。"""

    async def dispatch(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用 operation/arguments；预期拒绝保留可读说明，不伪装成 SDK 内部崩溃。"""
        try:
            return await invoke(operation, arguments)
        except ValueError as error:
            raise ToolError(str(error)) from None

    @server.tool()
    async def ui_get_state(message_limit: Annotated[int, Field(ge=0, le=10)] = 3) -> dict[str, Any]:
        """读取真实原生界面的页签、字段、选中身份、控件可见/可用状态与最近 message_limit 条消息。只控制小怪协作中心，不读其他应用或认证信息。"""
        return await dispatch("state", {"message_limit": message_limit})

    @server.tool()
    async def ui_set_fields(
        fields: UIFields,
        request_id: Annotated[str, Field(min_length=1, max_length=128)],
        confirm_write: StrictBool = False,
    ) -> dict[str, Any]:
        """批量填写 fields；request_id 为唯一去重键。开启 allow_write 必须明确 confirm_write=True；不会自动启动 AI。返回 queued 不等于完成，随后 ui_wait_operation。"""
        values = fields.model_dump(exclude_none=True)  # 只修改调用者明确给出的字段。
        if not values:
            raise ToolError("没有要修改的字段")
        if values.get("allow_write") and not confirm_write:
            raise ToolError("开启工程师写权限需要 confirm_write=True")
        return await dispatch(
            "set_fields",
            {"fields": values, "confirm_write": confirm_write, "request_id": request_id},
        )

    @server.tool()
    async def ui_select_tab(
        main: Literal["collaboration", "agents", "connection"],
        request_id: Annotated[str, Field(min_length=1, max_length=128)],
        section: Literal["messages", "execution"] = "messages",
    ) -> dict[str, Any]:
        """选择主页面 main，以及协作流程的子页 section；不清空输入、不触发模型。request_id 用于去重。"""
        return await dispatch(
            "select_tab", {"main": main, "section": section, "request_id": request_id}
        )

    @server.tool()
    async def ui_select_agent(
        name: Annotated[str, Field(min_length=2, max_length=48)],
        request_id: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict[str, Any]:
        """在参与者表格选择真实存在的 name，用于导出对应身份配置；request_id 用于去重。"""
        return await dispatch("select_agent", {"name": name, "request_id": request_id})

    @server.tool()
    async def ui_invoke(
        action: Literal[
            "open_project",
            "register_agent",
            "send_message",
            "export_agent_config",
            "export_ui_config",
            "export_history",
            "start_collaboration",
            "prepare_team",
            "stop_collaboration",
            "open_data_directory",
        ],
        request_id: Annotated[str, Field(min_length=1, max_length=128)],
        export_filename: Annotated[str, Field(max_length=120)] = "",
        confirm_start: StrictBool = False,
    ) -> dict[str, Any]:
        """执行 action 对应的真实界面业务入口。先 ui_set_fields 准备参数；启动模型需要 confirm_start=True，会消耗额度，写权限另受界面选项控制。导出历史仅允许新建本程序 exports 下的简单文件名，不覆盖现有文件。返回票据后必须 ui_wait_operation 核对完成状态；未知结果用同一 request_id 原样查询/重试，不能盲目重发。"""
        if action == "start_collaboration" and not confirm_start:
            raise ToolError("启动真实模型需要 confirm_start=True，并先取得用户授权")
        return await dispatch(
            "invoke",
            {
                "action": action,
                "export_filename": export_filename,
                "confirm_start": confirm_start,
                "request_id": request_id,
            },
        )

    @server.tool()
    async def ui_wait_operation(
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
        wait_seconds: Annotated[int, Field(ge=0, le=20)] = 10,
    ) -> dict[str, Any]:
        """读取 operation_id 的实际执行结果，最多等待 wait_seconds 秒。queued/executing 表示尚未证实完成；failed 不能当成功；超时不会自动重放。"""
        return await dispatch("wait", {"operation_id": operation_id, "wait_seconds": wait_seconds})
