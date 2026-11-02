# 第三方组件

根目录 MIT 许可证只适用于本项目自有代码，不替代上游许可。

- **nlohmann/json**：保留的单头文件位于 `native/third_party/json.hpp`，其 MIT 原文在同目录 `LICENSE.nlohmann-json`。
- **Microsoft.Web.WebView2 1.0.4314.35**：来源与 SHA512 在 `native/third_party/webview2/SDK.lock.json`；构建脚本从官方 NuGet 恢复头文件与静态加载器，不向 Git 提交 SDK 二进制。保留上游 `LICENSE.txt`，使用和分发时遵守该包及微软组件各自条款。
- **Python / MCP SDK / 构建依赖**：版本在 `requirements.lock.txt`；此仓库不复制第三方 Python 发行版代码。构建时 `collect_licenses.py` 从实际环境收集原始许可到忽略的 `third-party-licenses/`，分发成品时应一并保留。
- **Visual Studio、MFC、Windows SDK 和 WebView2 Runtime**：使用者需要自行安装及遵守相应条款，本仓库不重新许可这些平台组件。

本项目没有链接或复制 Agent Mail 的代码或二进制。
