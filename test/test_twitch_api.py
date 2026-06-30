from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.platforms.twitch.api import (
    CHANNEL_PROFILE_FIELDS,
    CLIP_FIELDS,
    KOL_FIELDS,
    STREAM_FIELDS,
    SULLYGNOME_GAME_SUMMARY_FIELDS,
    SULLYGNOME_VISIBLE_TABLE_FIELDS,
    TOP_GAME_FIELDS,
    VIDEO_FIELDS,
    TwitchChannelCandidate,
    parse_game_inputs,
    parse_keyword_specs,
    score_channel,
)
from src.platforms.twitch.sullygnome import (
    SullyGnomeGameRef,
    build_sullygnome_game_url,
    build_sullygnome_visible_table_row,
    normalize_sullygnome_game_slug,
)
from src.studio.discovery import discover_tools
from src.studio.registry import TOOLS


def test_twitch_tool_registered():
    static_ids = {tool.tool_id for tool in TOOLS}
    assert "twitch_game_content" in static_ids
    assert "twitch_kol_discovery" in static_ids

    discovered, errors = discover_tools()
    discovered_ids = {tool.tool_id for tool in discovered}
    assert "twitch_game_content" in discovered_ids
    assert "twitch_kol_discovery" in discovered_ids
    assert not [error for error in errors if "twitch" in error.lower()]


def test_twitch_fields_present():
    assert "观众数" in STREAM_FIELDS
    assert "播放量" in VIDEO_FIELDS
    assert "剪辑者" in CLIP_FIELDS
    assert "封面图" in TOP_GAME_FIELDS
    assert "关注者" in CHANNEL_PROFILE_FIELDS
    assert "总分" in KOL_FIELDS
    assert "Hours watched" in SULLYGNOME_GAME_SUMMARY_FIELDS
    assert "可见指标文本" in SULLYGNOME_VISIBLE_TABLE_FIELDS


def test_parse_twitch_game_inputs():
    values = """
    Palworld
    509658
    Palworld
    """
    assert parse_game_inputs(values) == [
        {"type": "name", "value": "Palworld"},
        {"type": "id", "value": "509658"},
    ]


def test_parse_twitch_keyword_specs():
    specs = parse_keyword_specs("upcoming monster taming games|P0|EN\nポケモンライク|P1|JP")
    assert specs[0] == {"keyword": "upcoming monster taming games", "priority": "P0", "market": "EN"}
    assert specs[1] == {"keyword": "ポケモンライク", "priority": "P1", "market": "JP"}


def test_twitch_kol_score_tier():
    channel = TwitchChannelCandidate(
        login="demo",
        channel_id="123",
        language="en",
        hit_keywords=["Palworld", "Temtem"],
        hit_priorities=["P0", "P0"],
        vod_views=125000,
        followers=50000,
    )
    scores = score_channel(channel)
    assert scores["total"] >= 50
    assert scores["tier"] in {"S", "A"}


def test_sullygnome_slug_and_url_helpers():
    assert normalize_sullygnome_game_slug("Just Chatting") == "Just_Chatting"
    assert normalize_sullygnome_game_slug("https://sullygnome.com/game/League_of_Legends/7/watched") == "League_of_Legends"
    assert build_sullygnome_game_url("Just Chatting", "30") == "https://sullygnome.com/game/Just_Chatting"
    assert build_sullygnome_game_url("Just Chatting", "7") == "https://sullygnome.com/game/Just_Chatting/7/summary"


def test_sullygnome_visible_table_row_builder():
    game = SullyGnomeGameRef(game_id="509658", name="Just Chatting", source="Just Chatting", source_type="游戏名")
    row = build_sullygnome_visible_table_row(
        game,
        slug="Just_Chatting",
        table_type="watched",
        rank=1,
        entity_name="demo",
        entity_url="https://sullygnome.com/channel/demo",
        metrics_text="1 | demo | 100,000",
    )
    assert row["Game ID"] == "509658"
    assert row["SullyGnome Slug"] == "Just_Chatting"
    assert row["实体名称"] == "demo"
    assert row["状态"] == "ok"


if __name__ == "__main__":
    test_twitch_tool_registered()
    test_twitch_fields_present()
    test_parse_twitch_game_inputs()
    test_parse_twitch_keyword_specs()
    test_twitch_kol_score_tier()
    test_sullygnome_slug_and_url_helpers()
    test_sullygnome_visible_table_row_builder()
    print("[PASS] Twitch API tests passed")

