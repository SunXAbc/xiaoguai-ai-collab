"""收集构建环境内第三方发行版的原始许可文件，不把开发缓存或凭据打进交付物。"""

import importlib.metadata
import re
import shutil
import sys
from pathlib import Path

# 源码根目录及用户可查看的许可证目录。
ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "third-party-licenses"
DESTINATION.mkdir(exist_ok=True)
# 许可证盘点表只含包名、版本、许可证声明，不包含解释器环境变量。
rows = [
    "# 第三方依赖许可盘点",
    "",
    "以下按本次锁定构建环境提取；原始许可正文在同目录。开发工具也列入，便于复现。",
    "",
    "| 发行版 | 版本 | 声明 |",
    "|---|---|---|",
]
for distribution in sorted(
    importlib.metadata.distributions(), key=lambda item: item.metadata["Name"].lower()
):
    # 每个发行版的公开名称、版本与许可表达式。
    name = distribution.metadata["Name"]
    license_name = distribution.metadata.get("License-Expression") or distribution.metadata.get(
        "License", "参见原始许可"
    )
    license_name = license_name.splitlines()[0][:100].replace("|", "/")
    rows.append(f"| {name} | {distribution.version} | {license_name} |")
    for relative in distribution.files or []:
        # 只复制发行版自带的许可正文，不复制整个依赖源码。
        if not re.search(
            r"^(license|licence|copying|notice|authors)(\.|$)", Path(relative).name, re.IGNORECASE
        ):
            continue
        source = Path(distribution.locate_file(relative))
        if not source.is_file():
            continue
        directory = DESTINATION / f"{name}-{distribution.version}"
        directory.mkdir(exist_ok=True)
        destination = directory / str(relative).replace("/", "_").replace("\\", "_").replace(
            ":", "_"
        )
        shutil.copyfile(source, destination)
# Python 标准库许可不属于 wheel 元数据，单独保留。
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.exists():
    shutil.copyfile(python_license, DESTINATION / "Python-LICENSE.txt")
shutil.copyfile(
    ROOT / "native/third_party/LICENSE.nlohmann-json", DESTINATION / "nlohmann-json-LICENSE.txt"
)
shutil.copyfile(
    ROOT / "native/third_party/webview2/LICENSE.txt", DESTINATION / "WebView2-SDK-LICENSE.txt"
)
(DESTINATION / "README.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
print("第三方许可盘点已生成；不包含 Agent Mail 的受限代码或二进制。")
