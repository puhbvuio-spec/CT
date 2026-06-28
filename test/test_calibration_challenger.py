from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest

from src.tools.calibration import STATUS_EMPTY_RESULT, main, parse_games_definition, parse_platforms, run_calibration_task


def create_mock_excel(file_path: Path, platform: str, values: list[object]) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    if platform == "x_twitter":
        sheet.title = "数据"
        sheet.append(["原始搜索词", "完整搜索语法", "序号", "推文内容", "浏览量", "点赞量", "转发量", "评论数", "发帖时间", "推文链接", "标签"])
        for index, value in enumerate(values, 1):
            sheet.append(["kw", "kw", index, "content", "1", "1", "1", "1", "2026-06-18", value, "tag"])
    else:
        sheet.title = "视频信息"
        sheet.append(["搜索词", "序号", "视频标题", "播放量", "点赞数", "发布时间", "视频链接"])
        for index, value in enumerate(values, 1):
            sheet.append(["kw", index, "title", "1", "1", "2026-06-18", value])
    file_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(file_path)
    workbook.close()


def test_parse_platforms_keeps_unknown_for_cli_reporting():
    assert parse_platforms("youtube, unknown_platform") == ["youtube", "unknown_platform"]


def test_legacy_output_file_path_creates_output_calibration_run_dir(tmp_path):
    baseline_excel = tmp_path / "baseline.xlsx"
    create_mock_excel(baseline_excel, "youtube", [])

    def mock_youtube(*args, **kwargs):
        kwargs["finish_callback"](str(baseline_excel))

    config = {
        "platforms": ["youtube"],
        "time_period": {"days": 7},
        "youtube": {"api_keys": ["key"], "max_results": 10},
        "games": [{"name": "Game A", "baseline_query": "base", "keyword_groups": [["kw1"]]}],
    }

    with patch("src.tools.calibration.run_youtube_spider", side_effect=mock_youtube):
        run_dir = Path(run_calibration_task(config, str(tmp_path / "report.md")))

    assert run_dir.parent.name == "calibration"
    assert (run_dir / "reports" / "calibration_report.md").exists()


def test_baseline_empty_result_is_not_treated_as_baseline_failed(tmp_path):
    baseline_excel = tmp_path / "baseline.xlsx"
    group_excel = tmp_path / "group.xlsx"
    create_mock_excel(baseline_excel, "youtube", [])
    create_mock_excel(group_excel, "youtube", ["https://www.youtube.com/watch?v=abcdefghijk"])

    def mock_youtube(*args, **kwargs):
        keyword = kwargs["keywords_list"][0]
        kwargs["finish_callback"](str(baseline_excel if keyword == "base" else group_excel))

    config = {
        "platforms": ["youtube"],
        "time_period": {"days": 7},
        "youtube": {"api_keys": ["key"], "max_results": 10},
        "games": [{"name": "Game A", "baseline_query": "base", "keyword_groups": [["kw1"]]}],
    }

    with patch("src.tools.calibration.run_youtube_spider", side_effect=mock_youtube):
        run_dir = Path(run_calibration_task(config, str(tmp_path / "output")))

    raw_baseline = json.loads((run_dir / "raw" / "youtube" / "baseline.json").read_text(encoding="utf-8"))
    assert raw_baseline["status"] == STATUS_EMPTY_RESULT

    csv_text = (run_dir / "reports" / "calibration_report.csv").read_text(encoding="utf-8-sig")
    assert "BASELINE_FAILED" not in csv_text
    assert ",100.0," in csv_text


def test_main_handles_string_days_and_invalid_days(tmp_path):
    valid_config = tmp_path / "valid.json"
    invalid_config = tmp_path / "invalid.json"
    valid_config.write_text(
        json.dumps({"games": [{"name": "Game A", "baseline_query": "base", "keyword_groups": []}], "time_period": {"days": "7"}}),
        encoding="utf-8",
    )
    invalid_config.write_text(
        json.dumps({"games": [{"name": "Game A", "baseline_query": "base", "keyword_groups": []}], "time_period": {"days": "seven"}}),
        encoding="utf-8",
    )

    with patch.object(sys, "argv", ["calibration.py", "--config", str(valid_config)]), patch(
        "src.tools.calibration.run_calibration_task"
    ) as mock_run:
        main()
        mock_run.assert_called_once()

    with patch.object(sys, "argv", ["calibration.py", "--config", str(invalid_config)]):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1


def test_window_validation_skips_youtube_key_when_platform_is_omitted():
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from src.tools.windows import CalibrationToolWindow

    window = CalibrationToolWindow()
    values = {
        "days": 7,
        "platforms": "tiktok, x_twitter",
        "youtube_api_keys": "",
        "youtube_max_results": 10,
        "tiktok_max_videos": 10,
        "x_max_scrolls": 2,
        "x_search_tab": "latest",
        "cdp_url": "http://localhost:9222",
        "output_path": "output/calibration",
        "games_definition": "Game A | base\nkw1",
    }
    window.validate_values(values)
    assert app is not None


def test_games_editor_widget_emits_valid_definition_json():
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from src.tools.windows import CalibrationToolWindow

    window = CalibrationToolWindow()
    editor = window.widgets["games_definition"]
    serialized = editor.text()
    parsed = parse_games_definition(serialized)

    assert parsed[0]["name"] == "Genshin Impact"
    assert parsed[0]["keyword_groups"][0] == ["原神 攻略", "原神 角色"]
    assert app is not None
