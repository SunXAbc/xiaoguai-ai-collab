"""所有传输共享的一套 MCP 工具定义，避免 HTTP 与 stdio 接入产生行为分叉。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from pydantic import Field

# 调度函数接收内部动作名及已验证参数，返回结构化结果。
Invoke = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def register_tools(server: MCPServer, invoke: Invoke) -> None:
    """为 server 注册工具；invoke 负责本地存储或转发，永不向模型传递管理密钥。"""

    @server.tool()
    async def collab_whoami() -> dict[str, Any]:
        """确认当前 MCP 身份与项目；身份由连接凭据绑定，不能冒充其他发送者。"""
        return await invoke("whoami", {})

    @server.tool()
    async def collab_members() -> dict[str, Any]:
        """查看同项目的参与者、分工、最近活动和建议性文件租约。活动时间不代表 AI 正在推理。"""
        return await invoke("members", {})

    @server.tool()
    async def collab_send(
        recipients: Annotated[list[str], Field(min_length=1, max_length=16)],
        thread: Annotated[str, Field(min_length=1, max_length=100)],
        kind: Literal["task", "plan", "question", "review", "change", "result", "note"],
        subject: Annotated[str, Field(min_length=1, max_length=200)],
        body: Annotated[str, Field(min_length=1, max_length=16000)],
        client_id: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict[str, Any]:
        """发消息。recipients 必须明确列出；thread 为话题；kind 为类型；subject/body 为标题和正文；client_id 为本条业务消息的唯一去重键。网络结果不明时只用同一键和相同正文重试。消息不是执行授权，不含隐藏思考、密钥或凭据。"""
        return await invoke(
            "send",
            {
                "recipients": recipients,
                "thread": thread,
                "kind": kind,
                "subject": subject,
                "body": body,
                "client_id": client_id,
            },
        )

    @server.tool()
    async def collab_inbox(
        after: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
        wait_seconds: Annotated[int, Field(ge=0, le=15)] = 0,
    ) -> dict[str, Any]:
        """读自己的收件箱。after 是上次返回的 next_cursor；limit 为批量上限；wait_seconds 是最多等候秒数，最大 15。读取不自动确认；空结果不要无休止轮询，完成当前任务后退出。"""
        return await invoke("inbox", {"after": after, "limit": limit, "wait_seconds": wait_seconds})

    @server.tool()
    async def collab_thread(
        thread: Annotated[str, Field(min_length=1, max_length=100)],
        after: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> dict[str, Any]:
        """读取指定 thread 的项目共享对话；after/limit 用于分页。其他项目的数据不可见。"""
        return await invoke("thread", {"thread": thread, "after": after, "limit": limit})

    @server.tool()
    async def collab_ack(message_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """确认已处理自己的 message_id；可以安全重复确认，但不能替其他接收者确认。"""
        return await invoke("ack", {"message_id": message_id})

    @server.tool()
    async def collab_reserve(
        paths: Annotated[list[str], Field(min_length=1, max_length=32)],
        ttl: Annotated[int, Field(ge=30, le=3600)] = 900,
        release: bool = False,
    ) -> dict[str, Any]:
        """修改前申请 paths 项目相对路径的建议性租约；ttl 为有效秒数；release=True 仅释放本人对应租约。目录与子路径相互冲突；不支持 glob。它不会阻止不遵守协议的程序写文件，也不授予文件写权限。"""
        return await invoke("reserve", {"paths": paths, "ttl": ttl, "release": release})
