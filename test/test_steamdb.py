import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.platforms.steam.steamdb import (
    SteamDbPageSnapshot,
    SteamDbWorkItem,
    build_depot_rows,
    build_dlc_rows,
    build_history_rows,
    build_overview_row,
    build_package_rows,
    build_steamdb_app_url,
    detect_steamdb_block,
    parse_steamdb_app_ids,
)


def test_parse_steamdb_app_ids():
    value = """
    https://steamdb.info/app/1623730/charts/
    https://store.steampowered.com/app/730/CounterStrike_2/
    570
    https://steamdb.info/app/1623730/depots/
    """
    assert parse_steamdb_app_ids(value) == [1623730, 730, 570]


def test_build_steamdb_app_url():
    assert build_steamdb_app_url(1623730, "overview") == "https://steamdb.info/app/1623730/"
    assert build_steamdb_app_url(1623730, "charts") == "https://steamdb.info/app/1623730/charts/"
    assert build_steamdb_app_url(1623730, "packages") == "https://steamdb.info/app/1623730/subs/"
    assert build_steamdb_app_url(1623730, "dlcs") == "https://steamdb.info/app/1623730/dlc/"
    assert build_steamdb_app_url(1623730, "depots") == "https://steamdb.info/app/1623730/depots/"
    assert build_steamdb_app_url(1623730, "history") == "https://steamdb.info/app/1623730/history/"
    assert "branch=" not in build_steamdb_app_url(1623730, "depots")
    assert "changeid=" not in build_steamdb_app_url(1623730, "history")


def test_detect_steamdb_block():
    assert detect_steamdb_block("Checking your browser", "")
    assert detect_steamdb_block("", "STOP. Do not make any further requests")
    assert detect_steamdb_block("Forbidden", "Ray ID: test")
    assert detect_steamdb_block("SteamDB", "normal page content") == ""


def _snapshot(page_type="packages"):
    item = SteamDbWorkItem(appid=1623730, source_type="直接输入", source="")
    return SteamDbPageSnapshot(
        item=item,
        page_type=page_type,
        url=build_steamdb_app_url(item.appid, page_type),
        final_url=build_steamdb_app_url(item.appid, page_type),
        title="Palworld · SteamDB",
        h1="Palworld",
        body_text="Palworld\nRelease Date 19 Jan 2024\nDeveloper Pocketpair\nPublisher Pocketpair\n12,345 players right now\n45,678 24-hour peak\n2,101,535 all-time peak",
        tables=[
            {
                "table_index": 1,
                "title": "Packages",
                "headers": ["ID", "Name", "Price"],
                "rows": [
                    {
                        "row_index": 1,
                        "cells": ["123456", "Palworld Standard", "$29.99"],
                        "links": [{"text": "Palworld Standard", "href": "/sub/123456/"}],
                    }
                ],
            }
        ],
        status="ok",
        note="test",
        query_time="2026-06-30 00:00:00",
    )


def test_build_overview_row():
    row = build_overview_row(_snapshot("overview"))
    assert row["AppID"] == 1623730
    assert row["游戏名"] == "Palworld"
    assert row["当前在线"] == "12,345"
    assert row["24小时峰值"] == "45,678"
    assert row["历史峰值"] == "2,101,535"


def test_build_page_rows_from_visible_tables():
    package_row = build_package_rows(_snapshot("packages"))[0]
    assert package_row["PackageID"] == "123456"
    assert package_row["套餐链接"] == "https://steamdb.info/sub/123456/"

    dlc_snapshot = _snapshot("dlcs")
    dlc_snapshot.tables[0]["rows"][0]["links"] = [{"text": "DLC", "href": "/app/2000000/"}]
    dlc_row = build_dlc_rows(dlc_snapshot)[0]
    assert dlc_row["DLC AppID"] == "2000000"

    depot_snapshot = _snapshot("depots")
    depot_snapshot.tables[0]["rows"][0]["links"] = [{"text": "Depot", "href": "/depot/1623731/"}]
    depot_row = build_depot_rows(depot_snapshot)[0]
    assert depot_row["DepotID"] == "1623731"

    history_snapshot = _snapshot("history")
    history_snapshot.tables[0]["rows"][0]["links"] = [{"text": "Change", "href": "/app/1623730/history/?changeid=987654"}]
    history_row = build_history_rows(history_snapshot)[0]
    assert history_row["变更ID"] == "987654"


def run_all_tests():
    tests = [
        test_parse_steamdb_app_ids,
        test_build_steamdb_app_url,
        test_detect_steamdb_block,
        test_build_overview_row,
        test_build_page_rows_from_visible_tables,
    ]
    for test in tests:
        test()
    print("[PASS] SteamDB dynamic-window tests passed")


if __name__ == "__main__":
    run_all_tests()
