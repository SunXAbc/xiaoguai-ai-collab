"""检查即将公开的 Git 索引，只报文件与规则，不输出疑似秘密原文。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # 只检查当前开源仓库。
BLOCKED = {
    "data",
    "exports",
    "verification",
    "test-projects",
    "bin",
    ".venv",
    ".build",
    "third-party-licenses",
    "__pycache__",
}  # 明确不公开的运行目录。
SUFFIXES = {
    ".dpapi",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".jsonl",
    ".log",
    ".exe",
    ".dll",
    ".lib",
    ".pdb",
    ".pfx",
    ".pem",
    ".key",
    ".nupkg",
    ".pyc",
}  # 不发布凭据和编译文件。
PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}"),
    "api-key": re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    "local-user-path": re.compile(
        rb"[A-Z]:[\\/]+Users[\\/]+(?!Public|Default)[^\\/\s]+", re.IGNORECASE
    ),
}  # 仅是基础规则，不把扫描通过说成完整保密保证。


def check() -> None:
    """对 Git 索引中的真实 blob 做检查，避免工作树忽略规则掩盖已暂存文件。"""
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(
        b"\0"
    )  # 包括新增已暂存源码，不遍历任何凭据目录。
    problems = []  # 只保存相对路径和规则标签。
    for encoded in filter(None, paths):
        name = encoded.decode("utf-8")  # Git 相对文件名。
        path = Path(name)  # 文件名也必须满足公开边界。
        if (
            set(path.parts) & BLOCKED
            or path.suffix.lower() in SUFFIXES
            or path.name.startswith(".env")
        ):
            problems.append((name, "blocked-path"))
            continue
        content = subprocess.check_output(
            ["git", "show", ":" + name], cwd=ROOT
        )  # 读索引内容而不是可能不同的工作树。
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                problems.append((name, label))
    if problems:
        raise SystemExit(
            "公开检查失败：" + "; ".join(f"{name}: {label}" for name, label in problems)
        )
    print(
        f"Git 索引基础公开检查通过：{len(list(filter(None, paths)))} 个文件；未检查未知模式或 Git 外内容"
    )


if __name__ == "__main__":
    check()
