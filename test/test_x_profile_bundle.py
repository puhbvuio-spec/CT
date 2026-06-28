from pathlib import Path
import sys

project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from src.platforms.x_twitter.profile_bundle import build_summary_row
from src.platforms.x_twitter.windows import XProfileBundleWindow
from src.studio.discovery import discover_tools
from src.studio.registry import TOOLS


def test_x_profile_bundle_summary_row():
    row = build_summary_row(
        {
            "作者主页链接": "https://x.com/demo",
            "作者的名称": "Demo",
            "账号ID": "demo",
            "粉丝数": "1000",
            "简介": "bio",
        },
        [
            {"url": "https://x.com/demo/status/1", "published_at": "2026-06-01 00:00:00", "content": "first\npost"},
            {"url": "https://x.com/demo/status/2", "published_at": "2026-06-02 00:00:00", "content": "second post"},
        ],
    )

    assert row["作者主页链接"] == "https://x.com/demo"
    assert row["作者名称"] == "Demo"
    assert row["采集推文数"] == "2"
    assert row["推文链接列表"] == "https://x.com/demo/status/1\nhttps://x.com/demo/status/2"
    assert row["推文内容列表"] == "first post\nsecond post"


def test_x_profile_bundle_defaults_and_discovery():
    window = XProfileBundleWindow.__new__(XProfileBundleWindow)
    defaults = {param.key: param.default for param in window.tool_config_params()}

    assert defaults["max_tweets_per_author"] == 100
    assert defaults["max_scrolls"] == 80

    static_ids = {tool.tool_id for tool in TOOLS}
    assert "x_profile_bundle" in static_ids

    discovered, errors = discover_tools()
    discovered_ids = {tool.tool_id for tool in discovered}
    assert "x_profile_bundle" in discovered_ids
    assert not [error for error in errors if "x_profile_bundle" in error]


if __name__ == "__main__":
    test_x_profile_bundle_summary_row()
    test_x_profile_bundle_defaults_and_discovery()
    print("x profile bundle tests passed")
