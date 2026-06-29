from __future__ import annotations

from src.platforms.steam.api import normalize_keywords, parse_app_ids
from src.studio.discovery import discover_tools
from src.studio.registry import TOOLS


def test_parse_steam_app_ids_from_numbers_and_urls():
    values = """
    1623730
    https://store.steampowered.com/app/294100/RimWorld/
    https://store.steampowered.com/app/1623730/Palworld/
    not-a-steam-link
    """
    assert parse_app_ids(values) == [1623730, 294100]


def test_normalize_keywords_deduplicates_case_and_spaces():
    values = "monster taming games\n Monster   Taming Games \nポケモンライク\n"
    assert normalize_keywords(values) == ["monster taming games", "ポケモンライク"]


def test_steam_api_tool_registered():
    static_ids = {tool.tool_id for tool in TOOLS}
    assert "steam_api_research" in static_ids
    discovered, errors = discover_tools()
    discovered_ids = {tool.tool_id for tool in discovered}
    assert "steam_api_research" in discovered_ids
    assert not [error for error in errors if "steam" in error.lower()]


if __name__ == "__main__":
    test_parse_steam_app_ids_from_numbers_and_urls()
    test_normalize_keywords_deduplicates_case_and_spaces()
    test_steam_api_tool_registered()
    print("[PASS] Steam API tests passed")

