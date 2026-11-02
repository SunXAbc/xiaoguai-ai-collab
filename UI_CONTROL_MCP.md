# AI 整窗控制 MCP

从 0.2.0 起，协作中心提供独立“界面管理 MCP”。0.3 的界面改用本地网页：相同六个工具直接读取/修改 WebView2 中的真实 DOM 控件，并复用原有业务入口，不是隐藏的旧控件或假界面，也不依赖鼠标、坐标或 OCR。

## 接入

保持协作中心窗口运行，使用程序生成的：

- `data/connections/desktop-ui.mcp.json`：通用 stdio MCP 配置。
- `data/connections/desktop-ui.codex.toml`：Codex 配置片段。
- `data/connections/desktop-ui.dpapi`：当前 Windows 用户加密的管理凭据，**不要分享**。

可以在左侧“MCP 接入”点击“导出界面控制配置”重新生成。将配置中的服务条目合并到可信 AI 宿主，不要覆盖原有配置。程序不会自动改写客户端全局设置。配置会自动发现当前端口，窗口正常重启不必改 URL。

**普通 Engineer/Reviewer 等项目消息配置仍只有原来的七个工具，不会自动获得整窗权限。** 要让某个 AI 管理界面，单独给它加载 `xiaoguai_ui` 服务。管理配置默认使用 MCP 宿主的提示确认模式；只应交给可信管理 AI。

## 六个工具

| 工具 | 能力 |
|---|---|
| `ui_get_state` | 读真实控件字段、可见/可用状态、页签、参与者、运行状态与最近消息；默认最多三条消息，可选 0～10 |
| `ui_set_fields` | 批量填写项目目录、身份名称/角色、话题、接收者、标题、正文、写权限；先整批校验再修改 |
| `ui_select_tab` | 切换主 Tab，以及对话/执行子 Tab；不清空输入 |
| `ui_select_agent` | 在真实参与者列表中选择身份，用于导出对应配置 |
| `ui_invoke` | 打开项目、登记身份、发送消息、导出两类 MCP 配置、导出历史、启动/停止协作、打开本程序数据目录 |
| `ui_wait_operation` | 等待/查询操作票据的实际执行结果，不把排队当成功 |

### 字段名称

`project_path`、`agent_name`、`agent_role`、`thread`、`recipient`、`subject`、`body`、`allow_write`。

0.4 增加严格整数字段：`ai_count`（2～50，不含 Operator）、`rounds`（1～3）、`concurrency`（1～4）。不接受字符串、布尔伪整数或越界值；运行中不能改变团队、轮次、并发、项目或写权限。写入模式的实际并发强制为 1。

例如先调用 `ui_set_fields`：`{"fields":{"ai_count":50,"rounds":1,"concurrency":4,"allow_write":false},"request_id":"team-fields-001"}`，等待回执，再 `ui_invoke`：`{"action":"prepare_team","request_id":"prepare-team-001"}`。这仅准备身份，模型调用仍为零；之后另行授权并明确启动才会最多调用 50 次。每轮末尾的审查员不与工作成员重叠执行。

`recipient` 必须与 `ui_get_state` 返回的 `recipient_options` 中某项完全匹配；可用 `Engineer + Reviewer` 明确投递给这两人。未知字段、任意 HWND、系统消息和命令行均不接受。

### 主/子页面

- 主页面 `collaboration`：协作空间；`messages` 为宽屏对话，`execution` 同时展示右侧任务面板。
- 主页面 `agents`：AI 团队与参与者登记。
- 主页面 `connection`：两类 MCP 接入配置和非敏感状态。

### 业务动作

`open_project`、`register_agent`、`send_message`、`export_agent_config`、`export_ui_config`、`export_history`、`start_collaboration`、`stop_collaboration`、`open_data_directory`。

新增 `prepare_team`：按 `ai_count` 登记模板成员，返回实际控件回执，不调用模型、不删除缩减人数后的旧成员。启动会自动准备所需身份，但只有 `start_collaboration` 加 `confirm_start:true` 才调用模型。六个管理工具名称和旧字段用法保持兼容；默认仍为 2 人 × 2 轮、串行。

“选择目录”对应先填写 `project_path` 再 `open_project`；保存历史由 `export_filename` 指定文件名，不需要 AI 操作 Windows 文件对话框。它覆盖本程序的业务控件，不开放任意桌面、其他应用或系统设置。

## 推荐调用顺序

1. `ui_get_state`，确认窗口已连接及当前字段。
2. `ui_set_fields`，每次变更提供唯一 `request_id`。
3. `ui_wait_operation(operation_id=...)`，确认 `completed`，不是只看工具调用已返回。
4. `ui_invoke` 执行业务入口，再等待实际结果。
5. 用 `ui_get_state` 读回界面效果。启动动作完成只代表已启动，不代表整个团队任务结束；继续查看 `run.status`、`run.active`、`run.stages` 与 `run.planned_calls`。

例如，先填写目录并等候，再打开项目：

```json
{"fields":{"project_path":"C:\\your-project"},"request_id":"open-project-fields-001"}
```

```json
{"action":"open_project","request_id":"open-project-001"}
```

填写任务后，明确获得用户授权，再启动只读协作：

```json
{"fields":{"body":"独立检查代码与测试，发送实际证据，不修改源码","allow_write":false},"request_id":"goal-fields-001"}
```

```json
{"action":"start_collaboration","confirm_start":true,"request_id":"start-001"}
```

开启工程师写权限需要在 `ui_set_fields` 中同时提供 `allow_write:true` 与 `confirm_write:true`。这些确认参数只是显式意图门槛，不是替代用户授权的证明；MCP 宿主仍应按其审批策略确认敏感操作。

## 执行结果与去重

- `queued`：已排队，尚未执行。
- `executing`：原生窗口已领取，但结果尚未确认。
- `completed`：原生控件已修改，或相应业务操作的真实快照已返回。
- `failed`：操作被拒绝或执行/观察失败，不能当成功。

同一运行期内，原样重试相同 `request_id` 返回原操作，不重复点击业务入口；同一键改参数会被拒绝。消息发送还把该键传递到 SQLite 的持久化去重机制。观察超时不自动取消、不重放。最近 128 个操作记录仅在当前服务内存中保留，重启/淘汰后的未知结果必须人工核对，不能盲目重提启动等有副作用的操作。

## 安全边界

- 管理 MCP 使用独立 `/ui/mcp` 路径和本机管理身份；普通项目令牌访问会被拒绝。
- 所有请求只经回环地址与当前用户的加密配置；控件操作在真正的 MFC UI 线程中执行。
- `ui_get_state` 不返回认证头、管理密钥、DPAPI 内容或模型隐藏推理。
- 启动模型需要 `confirm_start:true`，会消耗模型额度。写权限默认关闭，且运行中不能改权限/项目。
- MCP 导出历史只能在本程序 `exports` 目录新建简单 `.jsonl` 文件名；拒绝越界、设备名和覆盖已有文件。人工保存对话框的原有行为不变。
- 这仍是单个 Windows 用户的本机工具，不抵御同一用户下的恶意进程；管理凭据也不应交给普通、不可信项目 AI。

## 验收

`scripts/test_ui_mcp_native.py` 会创建隐藏但真实的 MFC 窗口、正常消息循环和独立数据目录，然后仅通过导出的标准 MCP 配置完成读状态、填写、切页、登记、选人、发送、去重、导出及拒绝路径检查。它不启动模型，不用截图/坐标，也不以替身闭合原生界面的功能验收。结果在 `verification/ui-mcp-native.json`。
