from __future__ import annotations

from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


def _lines(value: str) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


class SteamApiResearchWindow(SimpleToolWindow):
    tool_id = "steam_api_research"

    def __init__(self) -> None:
        super().__init__(
            "Steam API 游戏数据采集",
            [
                FieldSpec("api_key", "Steam Web API Key（可选）", placeholder="用于 IStoreService AppList；留空也可采集商店公开数据"),
                FieldSpec("app_ids", "AppID / 商店链接，每行一个", kind="text_or_file", placeholder="https://store.steampowered.com/app/1623730/Palworld/\n1623730"),
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", placeholder="monster taming games\npokemon like"),
                FieldSpec(
                    "language",
                    "商店语言",
                    kind="combo",
                    options=("english", "schinese", "japanese", "tchinese", "koreana", "french", "german", "spanish", "russian"),
                    default="english",
                ),
                FieldSpec("country", "商店地区代码", default="US", placeholder="US / JP / CN"),
                FieldSpec("collect_reviews", "是否采集玩家评论/评价摘要？", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("max_reviews", "每个游戏最多玩家评论数（0=仅摘要）", kind="int", default=0, minimum=0, maximum=5000),
                FieldSpec("collect_news", "是否采集新闻？", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("max_news", "每个游戏最多新闻数", kind="int", default=5, minimum=1, maximum=100),
            ],
            height=780,
        )
        self.bind_field_visibility("collect_reviews", "是", ["max_reviews"])
        self.bind_field_visibility("collect_news", "是", ["max_news"])

    def validate_values(self, values):
        from src.platforms.steam.api import normalize_keywords, parse_app_ids

        if not parse_app_ids(values.get("app_ids", "")) and not normalize_keywords(values.get("keywords", "")):
            raise ValueError("至少需要输入一个 Steam AppID/商店链接，或一个关键词。")
        country = str(values.get("country", "")).strip()
        if country and not (len(country) == 2 and country.isalpha()):
            raise ValueError("商店地区代码请填写 2 位国家/地区代码，例如 US、JP、CN。")

    def tool_config_params(self):
        return [
            ConfigParam("parallel_workers", "API 并发数", kind="int", default=1, minimum=1, maximum=8),
            ConfigParam("max_apps_per_keyword", "每个关键词最多游戏数", kind="int", default=100, minimum=1, maximum=5000),
            ConfigParam(
                "keyword_search_mode",
                "关键词发现方式",
                kind="combo",
                options=("商店搜索接口（推荐）", "商店搜索后补 AppList", "AppList 本地匹配"),
                default="商店搜索接口（推荐）",
                tooltip="商店搜索更快；AppList 本地匹配会缓存完整应用列表，适合补漏但首次较慢。",
            ),
            ConfigParam("include_non_games", "是否保留非游戏 App", kind="combo", options=("否", "是"), default="否"),
            ConfigParam("collect_current_players", "采集当前在线人数", kind="combo", options=("是", "否"), default="是"),
            ConfigParam("collect_achievements", "采集成就数量", kind="combo", options=("否", "是"), default="否"),
            ConfigParam("review_language", "评论语言", kind="text", default="all", tooltip="Steam appreviews language 参数；all=全部，english/schinese/japanese 等。"),
            ConfigParam("reviews_filter", "评论排序/过滤", kind="combo", options=("all", "recent", "updated"), default="all"),
            ConfigParam("request_timeout", "API 请求超时(秒)", kind="int", default=30, minimum=5, maximum=180),
            ConfigParam("request_delay", "单请求间隔(秒)", kind="float", default=0.2, minimum=0.0, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("cache_ttl_hours", "关键词/AppList 缓存有效期(小时)", kind="int", default=168, minimum=0, maximum=8760),
            ConfigParam("save_batch_size", "每批保存条数", kind="int", default=10, minimum=1, maximum=100),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.steam.api import run_steam_api_spider

        config_keys = {
            "parallel_workers",
            "max_apps_per_keyword",
            "keyword_search_mode",
            "include_non_games",
            "collect_current_players",
            "collect_achievements",
            "review_language",
            "reviews_filter",
            "request_timeout",
            "request_delay",
            "cache_ttl_hours",
            "save_batch_size",
        }
        config = {k: v for k, v in values.items() if k in config_keys}
        return run_steam_api_spider(
            str(values.get("api_key", "")).strip(),
            values.get("app_ids", ""),
            values.get("keywords", ""),
            values.get("language", "english"),
            values.get("country", "US"),
            values.get("collect_reviews", "否"),
            int(values.get("max_reviews", 0) or 0),
            values.get("collect_news", "否"),
            int(values.get("max_news", 5) or 5),
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )
