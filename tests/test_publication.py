"""开源依赖恢复与基础泄漏规则；缓存包和令牌都是明确的测试夹具。"""

import hashlib
import json
import zipfile

import pytest

from scripts import check_public_tree, restore_webview2


def cached_sdk(tmp_path):
    """在 tmp_path 创建明确的 SDK 压缩包替身，只用于检验恢复逻辑，不用于真实编译。"""
    destination = tmp_path / "native/third_party/webview2"  # 仅限此测试目录。
    destination.mkdir(parents=True)
    package = tmp_path / ".build/downloads/webview2-test.nupkg"  # 模拟已有缓存。
    package.parent.mkdir(parents=True)
    with zipfile.ZipFile(package, "w") as archive:
        for member in (
            "build/native/include/WebView2.h",
            "build/native/include/WebView2EnvironmentOptions.h",
            "build/native/x64/WebView2LoaderStatic.lib",
            "LICENSE.txt",
        ):
            archive.writestr(member, b"explicit test fixture, not an SDK")
        archive.writestr("../../must-not-extract.txt", b"unsafe unrelated path")
    lock = {
        "version": "test",
        "source": "https://api.nuget.org/locked-test.nupkg",
        "sha512": hashlib.sha512(package.read_bytes()).hexdigest(),
    }  # 只为校验逻辑构造的已知摘要。
    (destination / "SDK.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    return destination, lock


def test_cached_sdk_verifies_hash_and_only_extracts_named_files(tmp_path, monkeypatch):
    """不访问网络；恢复指定文件，忽略压缩包中的越界路径。"""
    destination, _ = cached_sdk(tmp_path)  # 本测试自己的缓存。
    monkeypatch.setattr(restore_webview2, "ROOT", tmp_path)
    monkeypatch.setattr(
        restore_webview2.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("缓存命中不应请求网络"),
    )
    restore_webview2.restore()
    assert (destination / "include/WebView2.h").is_file()
    assert (destination / "lib/WebView2LoaderStatic.lib").is_file()
    assert not (tmp_path / "must-not-extract.txt").exists()


def test_cached_sdk_rejects_bad_digest_before_extracting(tmp_path, monkeypatch):
    """摘要不匹配时失败，不生成 SDK 头文件或隐式绕过校验。"""
    destination, lock = cached_sdk(tmp_path)  # 明确被篡改摘要的测试夹具。
    lock["sha512"] = "0" * 128
    (destination / "SDK.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    monkeypatch.setattr(restore_webview2, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="摘要不匹配"):
        restore_webview2.restore()
    assert not (destination / "include").exists()


@pytest.mark.parametrize(
    "label,value",
    [
        ("github-token", b"ghp_" + b"a" * 30),
        ("api-key", b"sk-" + b"b" * 40),
        ("private-key", b"-----BEGIN " + b"PRIVATE KEY-----"),
        ("local-user-path", b"C:" + b"/Users/" + b"example/file.py"),
    ],
)
def test_publication_patterns_detect_synthetic_fixtures(label, value):
    """label/value 都是合成检测样例，不包含真实认证信息或本机用户目录。"""
    assert check_public_tree.PATTERNS[label].search(value)
