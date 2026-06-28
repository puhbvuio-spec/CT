from datetime import datetime
from pathlib import Path
import sys

project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

import src.platforms.tiktok.hashtag_author_works as hashtag_author_works
from src.platforms.tiktok.hashtag_author_works import (
    _author_row_for_hashtag,
    collect_hashtag_seed_authors,
    normalize_hashtag_input,
    parse_hashtag_sources,
)
from src.platforms.tiktok.keyword_author_works import TikTokAuthorSeed
from src.platforms.tiktok.windows import TikTokHashtagAuthorWorksWindow
from src.studio.discovery import discover_tools
from src.studio.registry import TOOLS


def test_normalize_hashtag_inputs():
    source = normalize_hashtag_input("https://www.tiktok.com/tag/palworld?lang=en")
    assert source.label == "#palworld"
    assert source.url == "https://www.tiktok.com/tag/palworld"

    source = normalize_hashtag_input("palworld")
    assert source.label == "#palworld"
    assert source.url == "https://www.tiktok.com/tag/palworld"

    source = normalize_hashtag_input("#monster taming")
    assert source.label == "#monster taming"
    assert source.url == "https://www.tiktok.com/tag/monster%20taming"

    sources = parse_hashtag_sources(["palworld", "https://www.tiktok.com/tag/palworld", "https://www.tiktok.com/@notatag"], skip_invalid=True)
    assert len(sources) == 1


def test_hashtag_author_row_uses_topic_column():
    seed = TikTokAuthorSeed(
        profile_url="https://www.tiktok.com/@demo",
        author_name="Demo",
        author_id="@demo",
        keywords=["#palworld"],
        seed_links=["https://www.tiktok.com/@demo/video/1"],
    )
    row = _author_row_for_hashtag(
        seed,
        {"博主主页链接": "https://www.tiktok.com/@demo", "博主名称": "Demo", "博主ID": "@demo", "粉丝量": "100", "作者简介": "bio"},
        [
            {"desc": "first\nvideo", "video_url": "https://www.tiktok.com/@demo/video/2", "published_at": "2026-06-01 00:00:00"},
            {"desc": "old video", "video_url": "https://www.tiktok.com/@demo/video/3", "published_at": "2025-01-01 00:00:00"},
        ],
        True,
        datetime(2026, 6, 1),
        datetime(2026, 6, 30),
    )

    assert "搜索词" not in row
    assert row["话题"] == "#palworld"
    assert row["采集作品数"] == "2"
    assert row["时间窗口内作品数"] == "1"
    assert row["作品标题列表"] == "first video\nold video"


def test_hashtag_seed_prefilter_skips_obvious_out_of_window_video_id():
    old_timestamp = int(datetime(2020, 1, 1, 12, 0, 0).timestamp())
    old_video_id = str(old_timestamp << 32)
    old_video_url = f"https://www.tiktok.com/@demo/video/{old_video_id}"
    extract_calls = []

    originals = {
        "open_hashtag_page": hashtag_author_works.open_hashtag_page,
        "dynamic_search_scroll_limit": hashtag_author_works.dynamic_search_scroll_limit,
        "collect_visible_video_items": hashtag_author_works.collect_visible_video_items,
        "extract_video_row": hashtag_author_works.extract_video_row,
        "trigger_search_lazy_load": hashtag_author_works.trigger_search_lazy_load,
        "interruptible_sleep": hashtag_author_works.interruptible_sleep,
    }
    try:
        hashtag_author_works.open_hashtag_page = lambda *args, **kwargs: True
        hashtag_author_works.dynamic_search_scroll_limit = lambda *args, **kwargs: 1
        hashtag_author_works.collect_visible_video_items = lambda page, seen: [
            {"视频链接": old_video_url, "播放量": "", "博主主页链接": "https://www.tiktok.com/@demo"}
        ]
        hashtag_author_works.extract_video_row = lambda *args, **kwargs: extract_calls.append(args[2]) or {}
        hashtag_author_works.trigger_search_lazy_load = lambda *args, **kwargs: None
        hashtag_author_works.interruptible_sleep = lambda *args, **kwargs: False

        authors = collect_hashtag_seed_authors(
            object(),
            object(),
            parse_hashtag_sources(["palworld"]),
            datetime(2026, 6, 1),
            datetime(2026, 6, 30),
            True,
            lambda message: None,
            max_seed_works=10,
            max_authors=10,
            max_topic_scrolls=1,
        )
    finally:
        for name, value in originals.items():
            setattr(hashtag_author_works, name, value)

    assert authors == {}
    assert extract_calls == []


def test_hashtag_page_failure_skips_source():
    sources = parse_hashtag_sources(["palworld"])
    originals = {
        "open_hashtag_page": hashtag_author_works.open_hashtag_page,
        "dynamic_search_scroll_limit": hashtag_author_works.dynamic_search_scroll_limit,
        "collect_visible_video_items": hashtag_author_works.collect_visible_video_items,
    }
    try:
        hashtag_author_works.open_hashtag_page = lambda *args, **kwargs: False
        hashtag_author_works.dynamic_search_scroll_limit = lambda *args, **kwargs: 1
        hashtag_author_works.collect_visible_video_items = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should skip failed page"))

        authors = collect_hashtag_seed_authors(
            object(),
            object(),
            sources,
            None,
            None,
            False,
            lambda message: None,
            max_seed_works=10,
            max_authors=10,
            max_topic_scrolls=1,
        )
    finally:
        for name, value in originals.items():
            setattr(hashtag_author_works, name, value)

    assert authors == {}


def test_hashtag_author_works_tool_registered():
    window = TikTokHashtagAuthorWorksWindow.__new__(TikTokHashtagAuthorWorksWindow)
    defaults = {param.key: param.default for param in window.tool_config_params()}
    assert defaults["max_profile_works_per_author"] == 50
    assert defaults["max_topic_scrolls"] == 360

    static_ids = {tool.tool_id for tool in TOOLS}
    assert "tiktok_hashtag_author_works" in static_ids

    discovered, errors = discover_tools()
    discovered_ids = {tool.tool_id for tool in discovered}
    assert "tiktok_hashtag_author_works" in discovered_ids
    assert not [error for error in errors if "tiktok_hashtag_author_works" in error]


if __name__ == "__main__":
    test_normalize_hashtag_inputs()
    test_hashtag_author_row_uses_topic_column()
    test_hashtag_seed_prefilter_skips_obvious_out_of_window_video_id()
    test_hashtag_page_failure_skips_source()
    test_hashtag_author_works_tool_registered()
    print("tiktok hashtag author works tests passed")
