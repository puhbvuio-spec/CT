"""更新检查模块 unit test。

使用 mock 模拟 GitHub API 响应，覆盖正常、异常、边界场景。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# 将被测模块加入 path
sys.path.insert(0, ".")

from src.core.updater import check_for_updates, is_newer, parse_semver


# ── parse_semver 测试 ────────────────────────────────────────────

@pytest.mark.parametrize(
    "version_str, expected",
    [
        ("0.0.0", (0, 0, 0)),
        ("1.0.0", (1, 0, 0)),
        ("2.1.0", (2, 1, 0)),
        ("10.20.30", (10, 20, 30)),
        ("2.1.0-beta", (2, 1, 0)),
        ("v2.1.0", None),       # v 前缀不会自动去除
        ("2.1.0-rc.1", (2, 1, 0)),
        ("2.1.0+build123", (2, 1, 0)),
    ],
)
def test_parse_semver(version_str, expected):
    assert parse_semver(version_str) == expected


def test_parse_semver_invalid():
    assert parse_semver("") is None
    assert parse_semver("abc") is None
    assert parse_semver("v2.1") is None
    assert parse_semver(None) is None   # type: ignore
    assert parse_semver("2.1") is None
    assert parse_semver("2") is None


# ── is_newer 测试 ────────────────────────────────────────────────

def test_is_newer_remote_is_newer():
    assert is_newer("1.0.0", "2.0.0") is True
    assert is_newer("1.0.0", "1.1.0") is True
    assert is_newer("1.0.0", "1.0.1") is True
    assert is_newer("0.9.9", "1.0.0") is True


def test_is_newer_same_version():
    assert is_newer("1.0.0", "1.0.0") is False


def test_is_newer_local_is_newer():
    assert is_newer("2.0.0", "1.0.0") is False
    assert is_newer("1.1.0", "1.0.0") is False
    assert is_newer("1.0.1", "1.0.0") is False


def test_is_newer_fallback_string_compare():
    """退回到字符串比较时仍能合理工作。"""
    assert is_newer("dev", "v2.0") is True
    assert is_newer("v2.0", "dev") is False


# ── check_for_updates 测试 ───────────────────────────────────────

def test_check_no_release():
    """仓库没有 release 时返回无更新。"""
    mock_response = MagicMock()
    mock_response.status_code = 404

    with patch("src.core.updater.requests.get", return_value=mock_response):
        has_update, latest, url = check_for_updates("1.0.0", "test", "repo")
        assert has_update is False
        assert latest is None
        assert url is None


def test_check_has_update():
    """远程版本大于本地版本时返回有更新。"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "tag_name": "v2.0.0",
        "html_url": "https://github.com/test/repo/releases/tag/v2.0.0",
    }

    with patch("src.core.updater.requests.get", return_value=mock_response):
        has_update, latest, url = check_for_updates("1.0.0", "test", "repo")
        assert has_update is True
        assert latest == "2.0.0"
        assert url == "https://github.com/test/repo/releases/tag/v2.0.0"


def test_check_no_update_same_version():
    """同版本返回无更新。"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "tag_name": "v1.0.0",
        "html_url": "https://github.com/test/repo/releases/tag/v1.0.0",
    }

    with patch("src.core.updater.requests.get", return_value=mock_response):
        has_update, latest, url = check_for_updates("1.0.0", "test", "repo")
        assert has_update is False
        assert latest == "1.0.0"


def test_check_local_newer():
    """本地版本比远程新时返回无更新。"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "tag_name": "v1.0.0",
        "html_url": "https://github.com/test/repo/releases/tag/v1.0.0",
    }

    with patch("src.core.updater.requests.get", return_value=mock_response):
        has_update, latest, url = check_for_updates("2.0.0", "test", "repo")
        assert has_update is False


def test_check_tag_without_v_prefix():
    """tag 不带 v 前缀也能正确解析。"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "tag_name": "3.0.0",
        "html_url": "https://github.com/test/repo/releases/tag/3.0.0",
    }

    with patch("src.core.updater.requests.get", return_value=mock_response):
        has_update, latest, url = check_for_updates("1.0.0", "test", "repo")
        assert has_update is True
        assert latest == "3.0.0"


def test_check_missing_tag_name():
    """API 返回数据缺少 tag_name 字段时抛出 ValueError。"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"html_url": "..."}

    with patch("src.core.updater.requests.get", return_value=mock_response):
        with pytest.raises(ValueError, match="tag_name"):
            check_for_updates("1.0.0", "test", "repo")


def test_check_network_error():
    """网络异常时抛出 requests.RequestException。"""
    import requests as rq

    with patch("src.core.updater.requests.get", side_effect=rq.ConnectionError("timeout")):
        with pytest.raises(rq.ConnectionError):
            check_for_updates("1.0.0", "test", "repo")


def test_check_http_error():
    """HTTP 5xx 错误时抛出异常。"""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.raise_for_status.side_effect = Exception("500 Server Error")

    with patch("src.core.updater.requests.get", return_value=mock_response):
        with pytest.raises(Exception):
            check_for_updates("1.0.0", "test", "repo")


def test_check_pre_release_tag():
    """pre-release 后缀的 tag 只比较数字部分。"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "tag_name": "v2.0.0-beta",
        "html_url": "https://github.com/test/repo/releases/tag/v2.0.0-beta",
    }

    with patch("src.core.updater.requests.get", return_value=mock_response):
        has_update, latest, url = check_for_updates("1.0.0", "test", "repo")
        assert has_update is True
        assert latest == "2.0.0-beta"


# ── 冒烟：真实 API 调用（可能被限流，故标记为可选） ──────────────

@pytest.mark.skip(reason="需要网络且可能触发 GitHub API 限流")
def test_check_real_repo():
    """对真实仓库发起请求，验证不会崩溃。"""
    has_update, latest, url = check_for_updates(
        "1.0.0", "helloworld856", "social-platform-scraper"
    )
    # 无论结果如何，不应抛异常
    assert isinstance(has_update, bool)


# ── 运行入口 ─────────────────────────────────────────────────────

if __name__ == "__main__":
    # 支持 python test/test_updater.py 直接运行
    pytest.main([__file__, "-v"])
