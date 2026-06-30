from __future__ import annotations

from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


class TwitchGameContentWindow(SimpleToolWindow):
    tool_id = "twitch_game_content"

    def __init__(self) -> None:
        super().__init__(
            "Twitch 游戏内容采集",
            [
                FieldSpec("client_id", "Twitch Client ID", required=True, placeholder="从 Twitch Developer Console 获取"),
                FieldSpec("client_secret", "Twitch Client Secret", required=True, placeholder="仅保存在本机配置，不写入仓库"),
                FieldSpec("games", "游戏名 / Game ID，每行一个", kind="text_or_file", placeholder="Palworld\nTeamfight Tactics\n509658"),
                FieldSpec("collect_streams", "是否采集当前直播？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("stream_count", "每个游戏最多直播数", kind="int", default=50, minimum=1, maximum=5000),
                FieldSpec("collect_videos", "是否采集 VOD/回放？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("video_count", "每个游戏最多 VOD 数", kind="int", default=50, minimum=1, maximum=5000),
                FieldSpec("collect_clips", "是否采集 Clips？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("clip_count", "每个游戏最多 Clip 数", kind="int", default=50, minimum=1, maximum=5000),
                FieldSpec("collect_top_games", "是否采集全平台 Top Games？", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("top_games_count", "Top Games 数量", kind="int", default=50, minimum=1, maximum=5000),
            ],
            height=820,
        )
        self.bind_field_visibility("collect_streams", "是", ["stream_count"])
        self.bind_field_visibility("collect_videos", "是", ["video_count"])
        self.bind_field_visibility("collect_clips", "是", ["clip_count"])
        self.bind_field_visibility("collect_top_games", "是", ["top_games_count"])

    def validate_values(self, values):
        from src.platforms.twitch.api import parse_game_inputs

        if not str(values.get("client_id", "")).strip():
            raise ValueError("Twitch Client ID 不能为空。")
        if not str(values.get("client_secret", "")).strip():
            raise ValueError("Twitch Client Secret 不能为空。")
        games = parse_game_inputs(values.get("games", ""))
        if not games and values.get("collect_top_games", "否") != "是":
            raise ValueError("请至少输入一个游戏名/Game ID，或开启 Top Games 采集。")

    def tool_config_params(self):
        return [
            ConfigParam("language", "语言过滤(可选)", kind="text", default="", tooltip="Twitch 语言代码，如 en、ja；留空不过滤。"),
            ConfigParam("video_period", "VOD 时间范围", kind="combo", options=("all", "month", "week", "day"), default="month"),
            ConfigParam("video_sort", "VOD 排序", kind="combo", options=("views", "time", "trending"), default="views"),
            ConfigParam("video_type", "VOD 类型", kind="combo", options=("archive", "highlight", "upload", "all"), default="archive"),
            ConfigParam("video_months_back", "VOD 客户端回溯月数(0=不限制)", kind="int", default=0, minimum=0, maximum=120),
            ConfigParam("video_min_views", "VOD 最低播放量", kind="int", default=0, minimum=0, maximum=1000000000),
            ConfigParam("clip_days_back", "Clip 回溯天数", kind="int", default=7, minimum=1, maximum=3650),
            ConfigParam("clip_min_views", "Clip 最低播放量", kind="int", default=0, minimum=0, maximum=1000000000),
            ConfigParam("request_timeout", "API 请求超时(秒)", kind="int", default=30, minimum=5, maximum=180),
            ConfigParam("request_delay", "单请求间隔(秒)", kind="float", default=0.1, minimum=0.0, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("save_batch_size", "每批保存条数", kind="int", default=10, minimum=1, maximum=100),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.twitch.api import run_twitch_game_content_spider

        config_keys = {
            "language",
            "video_period",
            "video_sort",
            "video_type",
            "video_months_back",
            "video_min_views",
            "clip_days_back",
            "clip_min_views",
            "request_timeout",
            "request_delay",
            "save_batch_size",
        }
        config = {key: values.get(key) for key in config_keys if key in values}
        return run_twitch_game_content_spider(
            str(values.get("client_id", "")).strip(),
            str(values.get("client_secret", "")).strip(),
            values.get("games", ""),
            values.get("collect_streams", "是"),
            int(values.get("stream_count", 50) or 50),
            values.get("collect_videos", "是"),
            int(values.get("video_count", 50) or 50),
            values.get("collect_clips", "是"),
            int(values.get("clip_count", 50) or 50),
            values.get("collect_top_games", "否"),
            int(values.get("top_games_count", 50) or 50),
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TwitchKolDiscoveryWindow(SimpleToolWindow):
    tool_id = "twitch_kol_discovery"

    def __init__(self) -> None:
        super().__init__(
            "Twitch KOL 发现",
            [
                FieldSpec("client_id", "Twitch Client ID", required=True, placeholder="从 Twitch Developer Console 获取"),
                FieldSpec("client_secret", "Twitch Client Secret", required=True, placeholder="仅保存在本机配置，不写入仓库"),
                FieldSpec(
                    "keywords",
                    "关键词，每行一个",
                    kind="text_or_file",
                    placeholder="upcoming monster taming games|P0|EN\nポケモンライク|P0|JP\nPalworld|P0|EN",
                ),
                FieldSpec(
                    "vod_game_names",
                    "VOD 挖掘游戏名/Game ID，每行一个（可选）",
                    kind="text_or_file",
                    placeholder="Palworld\nTeamfight Tactics\nPokemon GO",
                ),
            ],
            height=780,
        )

    def validate_values(self, values):
        from src.platforms.twitch.api import parse_game_inputs, parse_keyword_specs

        if not str(values.get("client_id", "")).strip():
            raise ValueError("Twitch Client ID 不能为空。")
        if not str(values.get("client_secret", "")).strip():
            raise ValueError("Twitch Client Secret 不能为空。")
        if not parse_keyword_specs(values.get("keywords", "")) and not parse_game_inputs(values.get("vod_game_names", "")):
            raise ValueError("请至少输入关键词，或输入用于 VOD 挖掘的游戏名/Game ID。")

    def tool_config_params(self):
        return [
            ConfigParam("search_count_per_keyword", "每关键词最多搜索频道数", kind="int", default=8, minimum=1, maximum=500),
            ConfigParam("search_live_only", "仅搜索正在直播频道", kind="combo", options=("否", "是"), default="否"),
            ConfigParam("max_vods_per_game", "每游戏最多挖掘 VOD 数", kind="int", default=10, minimum=0, maximum=500),
            ConfigParam("enrich_workers", "主播画像 API 并发数", kind="int", default=5, minimum=1, maximum=8),
            ConfigParam("min_followers", "推荐表最低关注者数", kind="int", default=50, minimum=0, maximum=1000000000),
            ConfigParam("min_total_score", "推荐表最低总分", kind="float", default=20.0, minimum=0.0, maximum=100.0, step=1.0, decimals=1),
            ConfigParam("request_timeout", "API 请求超时(秒)", kind="int", default=30, minimum=5, maximum=180),
            ConfigParam("request_delay", "单请求间隔(秒)", kind="float", default=0.1, minimum=0.0, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("save_batch_size", "每批保存条数", kind="int", default=10, minimum=1, maximum=100),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.twitch.api import run_twitch_kol_discovery_spider

        config_keys = {
            "search_count_per_keyword",
            "search_live_only",
            "max_vods_per_game",
            "enrich_workers",
            "min_followers",
            "min_total_score",
            "request_timeout",
            "request_delay",
            "save_batch_size",
        }
        config = {key: values.get(key) for key in config_keys if key in values}
        return run_twitch_kol_discovery_spider(
            str(values.get("client_id", "")).strip(),
            str(values.get("client_secret", "")).strip(),
            values.get("keywords", ""),
            values.get("vod_game_names", ""),
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )

