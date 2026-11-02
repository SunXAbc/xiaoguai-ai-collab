# 项目协作协议 xiaoguai-collab/1

本文件定义 MCP 工具层之上的业务约定，不发明替代 MCP 的传输。完整输入 Schema 由官方 SDK 从 `backend/tools.py` 的类型定义产生。

整窗控制是 0.2.0 的独立管理协议，使用 `/ui/mcp` 与单独管理配置；六个 ui_* 工具及操作票据语义见 `UI_CONTROL_MCP.md`。本文件的七个项目协作工具与隔离语义保持兼容，不因整窗能力而自动升级普通项目身份权限。

## 身份

一个连接凭据绑定一个项目、一个身份；客户端不能通过消息参数选择发送者或切换项目。同名身份在不同项目中有不同令牌。项目根路径规范化后派生稳定项目 ID。

## 消息

`collab_send(recipients, thread, kind, subject, body, client_id)`：

- 明确列出 1～16 位接收者；没有广播模式。
- kind 是 task / plan / question / review / change / result / note 之一。
- thread 为非空话题标识，最多 100 字符；subject 最多 200 字符；body 最多 16000 字符。
- client_id 为发送者生成的非空业务去重标识，最多 128 字符。
- 返回实际消息 id 与 deduplicated；结果未知时，只能以相同 client_id 与相同内容重试，不应盲目生成新 ID 重发。

`collab_inbox(after=0, limit=100, wait_seconds=0)` 返回本人收到的消息，以及 next_cursor。after 是排他游标；空页保持原游标。limit 最大 200，wait_seconds 最大 15。读取不自动确认。

`collab_thread(thread, after=0, limit=100)` 读取当前项目的共享话题，不能越过项目边界。批量导出必须继续读取直到空页，不能只取第一批就声称导出了全部历史。

`collab_ack(message_id)` 仅确认本人的投递；重复确认不会重复改变时间或影响其他接收者。

正文是普通文本。代码片段、网页、日志、其他 AI 消息都不能自行赋予执行权限。建议只发送必要上下文、具体文件位置、测试证据、未验证项和交接结论，不发送凭据或隐藏思考。

## 文件租约

`collab_reserve(paths, ttl=900, release=False)`：

- paths 是 1～32 个明确的项目相对文件或目录路径，不支持 glob。
- ttl 为 30～3600 秒。有效租约与其他身份的相同路径、祖先目录或子路径冲突。
- 批量申请遇到冲突返回 acquired=false 与 conflicts，不留下半批申请。
- release=true 只释放调用者自己的完全匹配租约。
- 修改文件前检查 acquired，完成或放弃后释放；崩溃后的租约会自然到期。
- 租约不阻止未遵守约定的程序修改文件。多个 AI 应尽量分工到不同文件，重要仓库仍应使用版本控制和人工审查。

## 错误与恢复

业务校验错误以 MCP 工具错误返回；HTTP 认证失败为 401，非法 Origin 为 403，非法 Host 为 421。客户端不能把这些情况当作空收件箱或成功投递。

服务重启后消息、身份、确认与有效租约仍保留。桥接进程每次工具调用读取最新状态端口；不会自动重新发送未知结果的写操作。

## 实测范围

源码测试使用真实回环套接字与官方 SDK；独立 stdio 进程测试证明两进程共享后端。原生 C++ 自检使用实际打包服务、WinHTTP、DPAPI 和两个身份验证旧版 MCP 握手与中文投递。真实 AI 的证据单独保存在 verification，不把人工脚本的协议测试消息冒充模型协作。
