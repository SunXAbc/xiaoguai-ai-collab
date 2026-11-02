# 小怪 AI 协作中心

Windows 本地多 AI / MCP 协作工作台。**工作台 0.4 / 后端 0.3.0**。

本地 HTML/CSS/JS + WebView2 桌面窗口，C++/MFC 管理原生生命周期，Python 官方 MCP SDK 提供共享通信服务。不是聊天界面模型图，也不是 MCP 协议本身会自动生成回答。

## 能做什么

- **2～50 个独立 AI 身份**，可设置 4、5、20、50 等人数。
- **1～3 轮**，每名成员每轮最多调用一次；启动前显示最多调用数，准备团队不调用模型。
- **只读最多四路并发**；允许修改代码时全员串行，仅 Engineer 可以写，其他成员和 Reviewer 始终只读。
- 每轮工作成员完成后，Reviewer 独立审查并汇总。失败会中止同组和后续队列，不盲目重试。
- 定向消息、独立收件箱、确认、持久化去重、分页历史与建议性文件租约。
- 六个管理 MCP 工具直接读写真实网页控件，支持选项目、填参数、准备团队、发送、启动、停止和导出，不依赖鼠标坐标。
- 浅色高对比度工作台；成员、实际对话、运行进度与接入配置集中展示，不展示模型隐藏思考。

> 50 人团队不代表同时启动 50 个进程，也不代表 50 家不同厂商。内置执行器使用本机已登录 Codex CLI 的不同独立上下文与 MCP 身份；其他 MCP 客户端可单独登记身份接入。

![浅色协作工作台](docs/workspace.png)

*界面预览使用明确标注的布局夹具，不是真实 AI 回答，也不包含个人运行数据。*

## 从源码构建

需要 Windows x64、Python 3.13、Visual Studio 2022 的桌面 C++、MFC 与 Windows SDK，以及 WebView2 Runtime。构建和纯程序测试不要求登录模型；真正执行 AI 任务另需本机可用的 Codex CLI、已有登录及 Windows 沙箱。

```powershell
git clone https://github.com/SunXAbc/xiaoguai-ai-collab.git
cd xiaoguai-ai-collab
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

- 创建仓库内 `.venv`，按 `requirements.lock.txt` 安装精确版本依赖。
- 从官方 NuGet 获取锁定的 WebView2 SDK，校验 SHA512 后仅提取所需文件；不把 SDK 二进制提交到仓库。
- 编译到 `bin/`，运行 Python 检查、示例测试、原生双身份自检、三视口页面检查及真实 DOM/MCP 回归。**这些步骤不调用模型。**
- 默认不创建或覆盖桌面快捷方式。需要时显式加 `-CreateDesktopShortcut`；已有不同目标的同名快捷方式会被保护。
- 中间产物在 `.build/`，结果在 `verification/`，都不进入 Git。已安装依赖时可加 `-SkipInstall`。

`ExecutionPolicy Bypass` 只作用于这次脚本进程，不修改系统策略，不等于关闭 Codex 沙箱。请先阅读脚本；运行中的本目录程序须先关闭再构建。

## 使用

1. 打开 `bin/XiaoguaiCollab.exe`，随附 `sample-project` 可用于纯测试。
2. 在任务面板设置人数、轮次和只读并发；点“准备 / 更新团队”只登记身份，不调用模型。
3. 填写目标，确认项目与权限后再启动。**启动会向 CLI 使用的模型服务发送任务与所需代码，并消耗模型额度。**
4. 默认只读。启用写权限后仅 Engineer 可写，整轮串行；建议先备份或使用版本控制。
5. “停止”只结束本任务拥有的 AI 进程树；关闭窗口会停止本窗口的后端。不会修改其他客户端的全局配置，不安装开机服务。

模板成员为 Engineer、Reviewer 和 Agent03～Agent50；额外成员侧重测试、架构、风险与文档。减少人数不会删除旧身份、历史或配置；手工登记的其他身份不会被自动启动。

## MCP 接入

每个项目身份有自己的标准 stdio 配置桥，连接同一个回环 HTTP 服务与 SQLite 数据库：

```text
AI A ─ 身份桥 A ─┐
AI B ─ 身份桥 B ─┼─ 本机 MCP 共享服务 ─ SQLite
AI C ─ 身份桥 C ─┘         ↑
                    WebView2 工作台
```

在“MCP 接入”导出选中身份的配置，合并对应 `mcpServers` 条目，不要覆盖宿主已有配置。界面管理使用单独的 `xiaoguai_ui` 身份，普通项目身份不能控制整窗。窗口需要保持运行，正常重启自动发现新端口。

| 协作工具 | 用途 |
|---|---|
| `collab_whoami` | 确认凭据绑定的项目与身份 |
| `collab_members` | 成员、分工、最近活动与有效租约 |
| `collab_send` | 显式定向投递，业务键持久化去重 |
| `collab_inbox` | 本人收件箱、游标与有界等候 |
| `collab_thread` | 项目内话题历史 |
| `collab_ack` | 按接收者独立、幂等确认 |
| `collab_reserve` | 建议性文件/目录租约与冲突检查 |

完整接口与安全边界见 [协议](PROTOCOL.md)、[管理 MCP](UI_CONTROL_MCP.md) 和 [架构](ARCHITECTURE.md)。

## 测试与限制

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check backend main.py tests scripts
```

当前开源副本已独立通过 78 项程序测试、7 项示例测试、原生自检及真实 DOM/MCP 回归。

源码测试覆盖持久化、隔离、事务、租约、2/4/5/20/50 人有界调度、审查屏障、失败中止及真实子进程回收。调度测试里的阶段替身不是 AI。原生界面测试会实际经过 DOM、窗口线程、打包服务和 MCP，但不消耗模型额度。

开发阶段已实跑四个模型身份的一轮只读协作，三工作成员并行结束后独立审查，另有独立测试复核。**未批量实跑 20/50 个模型，也未验证不同模型厂商的组合。** 个人对话、运行日志和凭据不公开；公开仓库不伪造缺失的验收记录。

服务只监听回环地址，凭据由当前 Windows 用户 DPAPI 加密，数据目录限制为当前用户与 SYSTEM。它不是抵御同一用户恶意进程的沙箱；项目消息只是资料，不是执行授权。租约是建议性协调，不能阻止其他程序写文件。任务结束不等于代码质量自动担保。

## 发布范围和许可证

这是**源码备份，不是个人运行数据的备份**。不发布 `data/`、`exports/`、`verification/`、测试运行目录、登录信息、对话记录、日志或编译 EXE。运行时生成的数据应由使用者自行妥善保管。

项目自有源码使用 [MIT License](LICENSE)。第三方组件保留各自许可，见 [第三方说明](THIRD_PARTY_NOTICES.md)。问题报告请勿附带密钥、加密凭据、完整私有项目或未经脱敏的日志，见 [安全说明](SECURITY.md)。
