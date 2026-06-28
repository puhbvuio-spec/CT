from __future__ import annotations

import json
from pathlib import Path

from PyQt5.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.base import FieldSpec, SimpleToolWindow

DEFAULT_GAMES_DEFINITION = """Genshin Impact | 原神
原神 攻略, 原神 角色
Genshin guide, Genshin showcase

Honkai: Star Rail | 崩坏：星穹铁道
星铁 攻略, 星铁 角色
Honkai Star Rail guide"""


class CalibrationGamesEditor(QWidget):
    def __init__(self, initial_definition: str = "") -> None:
        super().__init__()
        self._games: list[dict[str, object]] = []
        self._current_index = -1
        self._loading = False
        self._build_ui()

        if initial_definition.strip():
            self.setText(initial_definition)
        else:
            self._games = [self._empty_game(1)]
            self._reload_game_list(select_index=0)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        hint = QLabel("左侧选择游戏，右侧填写基准词和测试词组。每行一个词组，组内关键词用逗号分隔。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        root.addLayout(body)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)
        body.addLayout(left, 1)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        self.add_button = QPushButton("新增")
        self.remove_button = QPushButton("删除")
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.import_button = QPushButton("导入 TXT")
        self.add_button.clicked.connect(self._add_game)
        self.remove_button.clicked.connect(self._remove_game)
        self.up_button.clicked.connect(lambda: self._move_game(-1))
        self.down_button.clicked.connect(lambda: self._move_game(1))
        self.import_button.clicked.connect(self._import_txt)
        controls.addWidget(self.add_button)
        controls.addWidget(self.remove_button)
        controls.addWidget(self.up_button)
        controls.addWidget(self.down_button)
        controls.addWidget(self.import_button)
        left.addLayout(controls)

        self.game_list = QListWidget()
        self.game_list.setMinimumWidth(240)
        self.game_list.currentRowChanged.connect(self._on_current_row_changed)
        left.addWidget(self.game_list, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        body.addWidget(right, 2)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        right_layout.addLayout(form)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：Genshin Impact")
        self.name_edit.textChanged.connect(self._refresh_current_item_label)
        form.addRow("游戏名称", self.name_edit)

        self.baseline_edit = QLineEdit()
        self.baseline_edit.setPlaceholderText("例如：原神")
        form.addRow("基准词", self.baseline_edit)

        self.groups_edit = QPlainTextEdit()
        self.groups_edit.setPlaceholderText("每行一个测试词组，例如：\n原神 攻略, 原神 角色\nGenshin guide")
        self.groups_edit.setMinimumHeight(220)
        right_layout.addWidget(QLabel("测试词组"))
        right_layout.addWidget(self.groups_edit, 1)

    def _empty_game(self, number: int) -> dict[str, object]:
        return {
            "name": f"游戏 {number}",
            "baseline_query": "",
            "keyword_groups": [],
        }

    def _clone_games(self, games: list[dict[str, object]]) -> list[dict[str, object]]:
        cloned: list[dict[str, object]] = []
        for game in games:
            cloned.append(
                {
                    "name": str(game.get("name", "")),
                    "baseline_query": str(game.get("baseline_query", "")),
                    "keyword_groups": [
                        [str(keyword) for keyword in group]
                        for group in game.get("keyword_groups", [])
                        if isinstance(group, list)
                    ],
                }
            )
        return cloned

    def _snapshot_current_game(self) -> dict[str, object]:
        from src.tools.calibration import parse_keyword_groups_text

        return {
            "name": self.name_edit.text().strip(),
            "baseline_query": self.baseline_edit.text().strip(),
            "keyword_groups": parse_keyword_groups_text(self.groups_edit.toPlainText()),
        }

    def _persist_current_game(self) -> None:
        if self._loading:
            return
        if 0 <= self._current_index < len(self._games):
            self._games[self._current_index] = self._snapshot_current_game()

    def _populate_editor(self, game: dict[str, object]) -> None:
        from src.tools.calibration import format_keyword_groups_text

        self._loading = True
        self.name_edit.setText(str(game.get("name", "")))
        self.baseline_edit.setText(str(game.get("baseline_query", "")))
        self.groups_edit.setPlainText(format_keyword_groups_text(game.get("keyword_groups", [])))
        self._loading = False

    def _reload_game_list(self, select_index: int = 0) -> None:
        self._loading = True
        self.game_list.clear()
        for index, game in enumerate(self._games, 1):
            name = str(game.get("name", "")).strip() or f"游戏 {index}"
            self.game_list.addItem(name)

        if self._games:
            select_index = max(0, min(select_index, len(self._games) - 1))
            self.game_list.setCurrentRow(select_index)
            self._current_index = select_index
            self._populate_editor(self._games[select_index])
        else:
            self._current_index = -1
            self.name_edit.clear()
            self.baseline_edit.clear()
            self.groups_edit.clear()
        self._loading = False

    def _refresh_current_item_label(self) -> None:
        if self._loading:
            return
        current_row = self.game_list.currentRow()
        if 0 <= current_row < self.game_list.count():
            label = self.name_edit.text().strip() or f"游戏 {current_row + 1}"
            self.game_list.item(current_row).setText(label)

    def _on_current_row_changed(self, row: int) -> None:
        if self._loading:
            return

        previous_index = self._current_index
        if 0 <= previous_index < len(self._games):
            self._games[previous_index] = self._snapshot_current_game()

        self._current_index = row
        if 0 <= row < len(self._games):
            self._populate_editor(self._games[row])

    def _add_game(self) -> None:
        self._persist_current_game()
        self._games.append(self._empty_game(len(self._games) + 1))
        self._reload_game_list(select_index=len(self._games) - 1)

    def _remove_game(self) -> None:
        self._persist_current_game()
        current_row = self.game_list.currentRow()
        if not (0 <= current_row < len(self._games)):
            return

        if len(self._games) == 1:
            self._games = [self._empty_game(1)]
            self._reload_game_list(select_index=0)
            return

        self._games.pop(current_row)
        self._reload_game_list(select_index=min(current_row, len(self._games) - 1))

    def _move_game(self, offset: int) -> None:
        self._persist_current_game()
        current_row = self.game_list.currentRow()
        target_row = current_row + offset
        if not (0 <= current_row < len(self._games)):
            return
        if not (0 <= target_row < len(self._games)):
            return

        self._games[current_row], self._games[target_row] = self._games[target_row], self._games[current_row]
        self._reload_game_list(select_index=target_row)

    def _import_txt(self) -> None:
        from src.tools.calibration import parse_games_definition

        path, _ = QFileDialog.getOpenFileName(self, "导入多游戏配置 TXT", str(Path.cwd()), "Text Files (*.txt);;All Files (*.*)")
        if not path:
            return

        try:
            content = Path(path).read_text(encoding="utf-8")
            games = parse_games_definition(content)
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return

        self._games = self._clone_games(games)
        self._reload_game_list(select_index=0)

    def text(self) -> str:
        self._persist_current_game()
        return json.dumps(self._clone_games(self._games), ensure_ascii=False)

    def setText(self, raw_definition: str) -> None:
        from src.tools.calibration import parse_games_definition

        games = parse_games_definition(raw_definition)
        self._games = self._clone_games(games)
        self._reload_game_list(select_index=0)


class CalibrationToolWindow(SimpleToolWindow):
    tool_id = "keyword_coverage_calibration"

    def __init__(self) -> None:
        super().__init__(
            "关键词可观察搜索覆盖实验",
            [
                FieldSpec(
                    "days",
                    "时间范围（过去多少天）",
                    kind="int",
                    default=7,
                    minimum=1,
                    maximum=365,
                    tooltip="设置采集时间范围。例如填 7，则工具会采集过去 7 天内的数据用于覆盖实验。",
                ),
                FieldSpec(
                    "platforms",
                    "运行平台（英文逗号分隔）",
                    default="youtube, tiktok, x_twitter",
                    tooltip="指定要运行的平台。可选 youtube、tiktok、x_twitter。多个平台用逗号分隔，留空默认全部。",
                ),
                FieldSpec(
                    "youtube_api_keys",
                    "YouTube API Keys（每行一个）",
                    kind="multiline",
                    placeholder="仅在选择 youtube 时必填",
                    tooltip="由于 YouTube 配额限制，建议提供多个 Key 换行分隔，工具会自动轮询。",
                ),
                FieldSpec(
                    "youtube_max_results",
                    "YouTube 每词最大采集数",
                    kind="int",
                    default=10,
                    minimum=1,
                    maximum=5000,
                    tooltip="每个关键词在 YouTube 上最多采集多少条结果。",
                ),
                FieldSpec(
                    "tiktok_max_videos",
                    "TikTok 每词最大采集数",
                    kind="int",
                    default=10,
                    minimum=1,
                    maximum=5000,
                    tooltip="每个关键词在 TikTok 上最多采集多少个视频。",
                ),
                FieldSpec(
                    "x_max_scrolls",
                    "X（Twitter）每词最大滚动数",
                    kind="int",
                    default=2,
                    minimum=1,
                    maximum=5000,
                    tooltip="X 网页搜索向下滚动的次数。",
                ),
                FieldSpec(
                    "x_search_tab",
                    "X Search Tab",
                    kind="combo",
                    options=("latest", "top"),
                    default="latest",
                    tooltip="关键词覆盖实验默认建议使用 latest；如需对比高曝光结果，可切换为 top。",
                ),
                FieldSpec(
                    "cdp_url",
                    "CDP 调试地址",
                    default="http://localhost:9222",
                    tooltip="Chrome 远程调试协议（CDP）地址。",
                ),
                FieldSpec(
                    "output_path",
                    "输出路径",
                    kind="text",
                    required=True,
                    default="output/calibration_report.md",
                    tooltip="输出根目录或兼容旧版的报告文件路径。工具会自动生成 run_id 目录并保存快照、raw 数据和报告。",
                ),
                FieldSpec(
                    "games_definition",
                    "多游戏实验配置",
                    kind="games_editor",
                    required=True,
                    default=DEFAULT_GAMES_DEFINITION,
                    tooltip="结构化编辑多游戏配置；也支持从旧版 TXT 块格式导入。",
                ),
            ],
            height=860,
            form_stretch=2,
        )

    def _create_field_widget(self, field: FieldSpec):
        if field.kind == "games_editor":
            widget = CalibrationGamesEditor(str(field.default or ""))
            self.widgets[field.name] = widget
            if field.tooltip:
                widget.setToolTip(field.tooltip)
            return widget
        return super()._create_field_widget(field)

    def tool_config_params(self):
        return []

    def validate_values(self, values):
        from src.tools.calibration import invalid_platforms, parse_games_definition, parse_platforms

        platforms = parse_platforms(values.get("platforms", ""))
        invalid = invalid_platforms(platforms)
        if invalid:
            raise ValueError(f"不支持的平台: {', '.join(invalid)}")

        if "youtube" in platforms and not values.get("youtube_api_keys", "").strip():
            raise ValueError("请至少提供一个 YouTube API Key")

        parse_games_definition(values.get("games_definition", ""))

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.tools.calibration import parse_games_definition, parse_platforms, run_calibration_task

        platforms = parse_platforms(values.get("platforms", ""))
        api_keys = [key.strip() for key in values.get("youtube_api_keys", "").split("\n") if key.strip()]
        games_config = parse_games_definition(values.get("games_definition", ""))

        config = {
            "platforms": platforms,
            "time_period": {
                "days": int(values.get("days", 7)),
            },
            "youtube": {
                "api_keys": api_keys,
                "max_results": int(values.get("youtube_max_results", 10)),
            },
            "tiktok": {
                "cdp_url": values.get("cdp_url", "http://localhost:9222"),
                "max_videos": int(values.get("tiktok_max_videos", 10)),
            },
            "x_twitter": {
                "cdp_url": values.get("cdp_url", "http://localhost:9222"),
                "max_scrolls": int(values.get("x_max_scrolls", 2)),
                "x_search_tab": values.get("x_search_tab", "latest"),
            },
            "games": games_config,
        }

        try:
            actual_output = run_calibration_task(config, values["output_path"], log_callback, stop_event, pause_event)
            if not stop_event.is_set():
                finish_callback(actual_output)
        except Exception as exc:
            log_callback(f"执行异常: {exc}")
            raise
