from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest

from src.tools.calibration import (
    STATUS_EMPTY_RESULT,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_UNKNOWN_PLATFORM,
    extract_id_from_link,
    format_keyword_groups_text,
    parse_games_definition,
    parse_keyword_groups_text,
    parse_platforms,
    run_calibration_task,
    run_platform_spider,
)


def create_mock_excel(file_path: Path, platform: str, urls: list[str]) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    if platform == "x_twitter":
        sheet.title = "数据"
        headers = ["原始搜索词", "完整搜索语法", "序号", "推文内容", "浏览量", "点赞量", "转发量", "评论数", "发帖时间", "推文链接", "标签"]
        sheet.append(headers)
        for index, url in enumerate(urls, 1):
            sheet.append(["kw", "kw", str(index), "content", "10", "1", "1", "1", "2026-06-18", url, "tag"])
    else:
        sheet.title = "视频信息"
        headers = ["搜索词", "序号", "视频标题", "播放量", "点赞数", "发布时间", "视频链接"]
        sheet.append(headers)
        for index, url in enumerate(urls, 1):
            sheet.append(["kw", str(index), "title", "10", "1", "2026-06-18", url])
    file_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(file_path)
    workbook.close()


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def test_parse_platforms_deduplicates_and_defaults():
    assert parse_platforms("youtube, tiktok, youtube, x_twitter") == ["youtube", "tiktok", "x_twitter"]
    assert parse_platforms("") == ["youtube", "tiktok", "x_twitter"]


def test_keyword_groups_text_helpers_round_trip():
    raw_text = "kw1, kw2\n\n词组A，词组B\nsolo"
    groups = parse_keyword_groups_text(raw_text)
    assert groups == [["kw1", "kw2"], ["词组A", "词组B"], ["solo"]]
    assert format_keyword_groups_text(groups) == "kw1, kw2\n词组A, 词组B\nsolo"


def test_parse_games_definition_supports_block_and_json_formats():
    block_definition = """
    # 注释会被忽略
    Game A | base
    kw1, kw2
    kw3

    Game B | 基准词
    词组A，词组B
    """.strip()

    assert parse_games_definition(block_definition) == [
        {
            "name": "Game A",
            "baseline_query": "base",
            "keyword_groups": [["kw1", "kw2"], ["kw3"]],
        },
        {
            "name": "Game B",
            "baseline_query": "基准词",
            "keyword_groups": [["词组A", "词组B"]],
        },
    ]

    json_definition = json.dumps(
        [
            {
                "name": "Game C",
                "baseline_query": "base-c",
                "keyword_groups": [["alpha", "beta"]],
            }
        ],
        ensure_ascii=False,
    )
    assert parse_games_definition(json_definition) == [
        {
            "name": "Game C",
            "baseline_query": "base-c",
            "keyword_groups": [["alpha", "beta"]],
        }
    ]


def test_parse_games_definition_rejects_invalid_block_header():
    with pytest.raises(ValueError, match="首行必须写成"):
        parse_games_definition("Game A\nkw1, kw2")


def test_extract_id_from_link_normalizes_platform_urls():
    assert extract_id_from_link("https://www.youtube.com/watch?v=abcdefghijk&feature=share", "youtube") == "abcdefghijk"
    assert extract_id_from_link("https://m.tiktok.com/v/7359934348222222222.html?foo=bar", "tiktok") == "7359934348222222222"
    assert extract_id_from_link("https://twitter.com/user/status/1234567890?s=20", "x_twitter") == "1234567890"


def test_run_platform_spider_reports_success_empty_and_unknown(tmp_path):
    success_excel = tmp_path / "success.xlsx"
    empty_excel = tmp_path / "empty.xlsx"
    create_mock_excel(success_excel, "youtube", ["https://www.youtube.com/watch?v=abcdefghijk"])
    create_mock_excel(empty_excel, "youtube", [])

    def mock_success(*args, **kwargs):
        kwargs["finish_callback"](str(success_excel))

    def mock_empty(*args, **kwargs):
        kwargs["finish_callback"](str(empty_excel))

    with patch("src.tools.calibration.run_youtube_spider", side_effect=mock_success):
        result = run_platform_spider("youtube", "kw", "2026-06-01", "2026-06-08", {}, 7)
        assert result.status == STATUS_SUCCESS
        assert result.ids == {"abcdefghijk"}
        assert result.output_path == str(success_excel)

    with patch("src.tools.calibration.run_youtube_spider", side_effect=mock_empty):
        result = run_platform_spider("youtube", "kw", "2026-06-01", "2026-06-08", {}, 7)
        assert result.status == STATUS_EMPTY_RESULT
        assert result.ids == set()

    result = run_platform_spider("unknown_platform", "kw", "2026-06-01", "2026-06-08", {}, 7)
    assert result.status == STATUS_UNKNOWN_PLATFORM


def test_x_search_tab_helpers_support_latest_and_top():
    source = Path("src/platforms/x_twitter/keyword.py").read_text(encoding="utf-8")
    assert '"latest": "live"' in source
    assert '"top": "top"' in source
    assert 'adv_params.get("search_tab", "top")' in source


def test_run_calibration_task_generates_run_bundle_and_new_metrics(tmp_path):
    baseline_excel = tmp_path / "baseline.xlsx"
    kw1_excel = tmp_path / "kw1.xlsx"
    kw2_excel = tmp_path / "kw2.xlsx"
    create_mock_excel(
        baseline_excel,
        "youtube",
        [
            "https://www.youtube.com/watch?v=aaaaaaaaaaa",
            "https://youtu.be/bbbbbbbbbbb",
        ],
    )
    create_mock_excel(
        kw1_excel,
        "youtube",
        [
            "https://www.youtube.com/watch?v=aaaaaaaaaaa",
            "https://www.youtube.com/watch?v=ccccccccccc",
        ],
    )
    create_mock_excel(kw2_excel, "youtube", ["https://www.youtube.com/watch?v=ddddddddddd"])

    def mock_youtube(*args, **kwargs):
        keyword = kwargs["keywords_list"][0]
        path_map = {
            "base": baseline_excel,
            "kw1": kw1_excel,
            "kw2": kw2_excel,
        }
        kwargs["finish_callback"](str(path_map[keyword]))

    config = {
        "platforms": ["youtube"],
        "time_period": {"days": 7},
        "youtube": {"api_keys": ["key"], "max_results": 10},
        "games": [
            {
                "name": "Game A",
                "baseline_query": "base",
                "keyword_groups": [["kw1", "kw2"]],
            }
        ],
    }

    with patch("src.tools.calibration.run_youtube_spider", side_effect=mock_youtube):
        run_dir = Path(run_calibration_task(config, str(tmp_path / "legacy_report.md")))

    assert run_dir.name
    assert (run_dir / "config_snapshot.json").exists()
    assert (run_dir / "environment_snapshot.json").exists()
    assert (run_dir / "raw" / "youtube" / "baseline.json").exists()
    assert (run_dir / "raw" / "youtube" / "group_01.json").exists()
    assert (run_dir / "reports" / "calibration_report.md").exists()
    assert (run_dir / "reports" / "calibration_report.csv").exists()

    rows = read_csv_rows(run_dir / "reports" / "calibration_report.csv")
    assert rows[0]["Result Count"] == "3"
    assert rows[0]["Raw Link Count"] == "3"
    assert rows[0]["Baseline Intersection Count"] == "1"
    assert rows[0]["Relative Result Volume (%)"] == "150.0"
    assert rows[0]["Baseline Overlap Rate (%)"] == "50.0"
    assert rows[0]["Unique Result Count"] == "2"
    assert rows[0]["Incremental Gain (%)"] == "50.0"
    assert rows[0]["Jaccard Similarity (%)"] == "25.0"
    assert "Volume Coverage (%)" not in rows[0]
    assert "Intersection Coverage (%)" not in rows[0]

    markdown = (run_dir / "reports" / "calibration_report.md").read_text(encoding="utf-8")
    assert "关键词可观察搜索覆盖实验报告" in markdown
    assert "本报告不代表平台全量内容覆盖率。" in markdown
    assert "Relative Result Volume" in markdown

    raw_group = json.loads((run_dir / "raw" / "youtube" / "group_01.json").read_text(encoding="utf-8"))
    assert raw_group["ids"] == ["aaaaaaaaaaa", "ccccccccccc", "ddddddddddd"]
    assert raw_group["keyword_runs"][0]["keyword"] == "kw1"


def test_run_calibration_task_marks_baseline_failed_groups(tmp_path):
    config = {
        "platforms": ["youtube"],
        "time_period": {"days": 7},
        "youtube": {"api_keys": ["key"], "max_results": 10},
        "games": [
            {
                "name": "Game A",
                "baseline_query": "base",
                "keyword_groups": [["kw1"]],
            }
        ],
    }

    def mock_youtube_fail(*args, **kwargs):
        raise RuntimeError("Quota exceeded")

    with patch("src.tools.calibration.run_youtube_spider", side_effect=mock_youtube_fail):
        run_dir = Path(run_calibration_task(config, str(tmp_path / "output")))

    rows = read_csv_rows(run_dir / "reports" / "calibration_report.csv")
    assert rows[0]["Baseline Status"] == "QUOTA_EXCEEDED"
    assert rows[0]["Group Status"] == "BASELINE_FAILED"
    assert rows[0]["Relative Result Volume (%)"] == ""
    assert rows[0]["Incremental Gain (%)"] == ""


def test_group_partial_failure_still_keeps_observed_results(tmp_path):
    success_excel = tmp_path / "kw1.xlsx"
    create_mock_excel(success_excel, "youtube", ["https://www.youtube.com/watch?v=aaaaaaaaaaa"])

    def mock_youtube(*args, **kwargs):
        keyword = kwargs["keywords_list"][0]
        if keyword == "kw2":
            raise RuntimeError("network down")
        kwargs["finish_callback"](str(success_excel))

    config = {
        "platforms": ["youtube"],
        "time_period": {"days": 7},
        "youtube": {"api_keys": ["key"], "max_results": 10},
        "games": [
            {
                "name": "Game A",
                "baseline_query": "base",
                "keyword_groups": [["kw1", "kw2"]],
            }
        ],
    }

    with patch("src.tools.calibration.run_youtube_spider", side_effect=mock_youtube):
        run_dir = Path(run_calibration_task(config, str(tmp_path / "output")))

    rows = read_csv_rows(run_dir / "reports" / "calibration_report.csv")
    assert rows[0]["Group Status"] == STATUS_FAILED
    assert rows[0]["Result Count"] == "1"
    assert "kw2" in rows[0]["Error Message"]
