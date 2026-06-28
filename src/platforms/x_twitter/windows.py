from __future__ import annotations

from src.core import DEFAULT_X_CDP_URL, cdp_url_for_browser, debug_port_from_cdp_url
from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"
BROWSER_OPTIONS = ("全局设置", "Chrome", "Edge")


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _browser_config_param() -> ConfigParam:
    return ConfigParam(
        "browser",
        "浏览器",
        kind="combo",
        default="全局设置",
        options=BROWSER_OPTIONS,
        tooltip="Chrome 走 9222；Edge 走 9223 和独立 user_data_edge，适合 Chrome 被 X 风控时切换。",
    )


def _browser_value(values) -> str | None:
    value = str(values.get("browser", "全局设置")).strip().lower()
    if value == "edge":
        return "edge"
    if value == "chrome":
        return "chrome"
    return None


def _x_cdp_url(values) -> str:
    return cdp_url_for_browser(_browser_value(values), DEFAULT_X_CDP_URL)


def _x_config(values, keys):
    config = {k: v for k, v in values.items() if k in keys}
    browser = _browser_value(values)
    if browser:
        config["browser"] = browser
    return config


class XKeywordWindow(SimpleToolWindow):
    tool_id = "x_keyword_video_search"

    def tool_config_params(self):
        return [
            _browser_config_param(),
            ConfigParam("max_parallel_tabs", "关键词爬取并行tab数", kind="int", default=1, minimum=1, maximum=3,
                        tooltip="同时处理几个关键词。1=顺序处理。最大3。"),
            ConfigParam("max_comment_tabs", "评论爬取并行tab数", kind="int", default=1, minimum=1, maximum=3,
                        tooltip="每个关键词同时用几个tab采集评论。1=顺序。最大3。"),
            ConfigParam("max_queue_size", "评论队列最大长度", kind="int", default=5000, minimum=10, maximum=10000,
                        tooltip="待爬评论链接的缓冲上限。满了则暂停采集新推文。"),
            ConfigParam("slice_days", "时间切片跨度(天)", kind="int", default=7, minimum=1, maximum=365),
            ConfigParam("search_page_timeout", "页面加载超时(毫秒)", kind="int", default=40000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("no_new_scroll_limit", "搜索页无新内容停止阈值", kind="int", default=5, minimum=2, maximum=30),
            ConfigParam("comment_no_new_scroll_limit", "评论页无新内容停止阈值", kind="int", default=5, minimum=2, maximum=30),
            ConfigParam("search_refresh_count", "搜索页刷新次数", kind="int", default=3, minimum=0, maximum=10,
                        tooltip="搜索页未加载出内容时，自动刷新重试的次数。0=不刷新。"),
            ConfigParam("search_refresh_interval", "搜索页刷新间隔(秒)", kind="float", default=5.0, minimum=1.0, maximum=30.0, step=0.5, decimals=1),
            ConfigParam("comment_refresh_count", "评论页刷新次数", kind="int", default=3, minimum=0, maximum=10,
                        tooltip="评论页未加载出内容时，自动刷新重试的次数。0=不刷新。"),
            ConfigParam("comment_refresh_interval", "评论页刷新间隔(秒)", kind="float", default=5.0, minimum=1.0, maximum=30.0, step=0.5, decimals=1),
            ConfigParam("empty_retry_rounds", "空关键词补爬轮数", kind="int", default=2, minimum=0, maximum=10,
                        tooltip="关键词结果为空时自动重新爬取的轮数。0=不补爬。"),
            ConfigParam("empty_retry_cooldown_min", "补爬冷却最小(秒)", kind="float", default=15.0, minimum=0.0, maximum=300.0, step=1.0, decimals=1),
            ConfigParam("empty_retry_cooldown_max", "补爬冷却最大(秒)", kind="float", default=30.0, minimum=0.0, maximum=600.0, step=1.0, decimals=1),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 关键词搜索",
            [
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", default="AI生成动画\nAI animation", required=True, placeholder="每行一个关键词"),
                FieldSpec(
                    "lang",
                    "目标语言",
                    kind="combo",
                    default="日文 (ja)",
                    options=("不限 (Any)", "中文 (zh)", "英文 (en)", "日文 (ja)", "韩文 (ko)", "俄文 (ru)", "西语 (es)", "法语 (fr)", "德语 (de)"),
                ),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("get_comments", "是否获取推文评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=500, minimum=10, maximum=10000),
            ],
            height=820,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if values.get("limit_time") == "是":
            if not values.get("start_date") or not values.get("end_date"):
                raise ValueError("开始日期和结束日期不能为空。")

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.keyword import run_x_spider

        lang_map = {
            "不限 (Any)": "any",
            "中文 (zh)": "zh",
            "英文 (en)": "en",
            "日文 (ja)": "ja",
            "韩文 (ko)": "ko",
            "俄文 (ru)": "ru",
            "西语 (es)": "es",
            "法语 (fr)": "fr",
            "德语 (de)": "de",
        }
        adv_params = {
            "lang": lang_map.get(values["lang"], "any"),
            "limit_time": values["limit_time"],
            "start_date": values["start_date"],
            "end_date": values["end_date"],
            "get_comments": values["get_comments"],
            "max_comments": int(values["max_comments"]),
        }
        config = _x_config(values, ("slice_days", "search_page_timeout", "cooldown_min", "cooldown_max", "no_new_scroll_limit", "comment_no_new_scroll_limit", "max_scrolls", "max_parallel_tabs", "max_comment_tabs", "max_queue_size", "search_refresh_count", "search_refresh_interval", "comment_refresh_count", "comment_refresh_interval", "empty_retry_rounds", "empty_retry_cooldown_min", "empty_retry_cooldown_max"))
        return run_x_spider(_lines(values["keywords"]), adv_params, debug_port_from_cdp_url(_x_cdp_url(values)), log_callback, finish_callback, stop_event, config=config, pause_event=pause_event)


class XKeywordAuthorWorksWindow(SimpleToolWindow):
    tool_id = "x_keyword_author_works"

    def tool_config_params(self):
        return [
            _browser_config_param(),
            ConfigParam("max_seed_works", "关键词入口最多检查作品数", kind="int", default=300, minimum=1, maximum=5000),
            ConfigParam("max_authors", "最多进入作者主页数", kind="int", default=100, minimum=1, maximum=1000),
            ConfigParam("max_profile_works_per_author", "非快速模式每个作者最多采集作品数", kind="int", default=50, minimum=1, maximum=2000),
            ConfigParam("max_search_scrolls", "关键词搜索最大滚动次数", kind="int", default=200, minimum=5, maximum=2000),
            ConfigParam("max_profile_scrolls", "作者主页最大滚动次数", kind="int", default=300, minimum=10, maximum=2000),
            ConfigParam("slice_days", "时间切片跨度(天)", kind="int", default=7, minimum=1, maximum=365),
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=30000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_interval_min", "主页滚动间隔最小(秒)", kind="float", default=2.4, minimum=0.5, maximum=30.0, step=0.1, decimals=1),
            ConfigParam("scroll_interval_max", "主页滚动间隔最大(秒)", kind="float", default=5.6, minimum=0.5, maximum=60.0, step=0.1, decimals=1),
            ConfigParam("no_new_scroll_limit", "连续无新增停止阈值", kind="int", default=10, minimum=2, maximum=50),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 关键词作者作品采集",
            [
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", default="AI生成动画\nAI animation", required=True, placeholder="每行一个关键词"),
                FieldSpec(
                    "lang",
                    "目标语言",
                    kind="combo",
                    default="日文 (ja)",
                    options=("不限 (Any)", "中文 (zh)", "英文 (en)", "日文 (ja)", "韩文 (ko)", "俄文 (ru)", "西语 (es)", "法语 (fr)", "德语 (de)"),
                ),
                FieldSpec("limit_time", "是否限制关键词命中时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("quick_mode", "快速模式（作品最多取最新50条）？", kind="combo", options=("是", "否"), default="是"),
            ],
            height=760,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])

    def validate_values(self, values):
        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if values.get("limit_time") == "是":
            if not values.get("start_date") or not values.get("end_date"):
                raise ValueError("开始日期和结束日期不能为空。")

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.keyword_author_works import run_x_keyword_author_works_spider

        lang_map = {
            "不限 (Any)": "any",
            "中文 (zh)": "zh",
            "英文 (en)": "en",
            "日文 (ja)": "ja",
            "韩文 (ko)": "ko",
            "俄文 (ru)": "ru",
            "西语 (es)": "es",
            "法语 (fr)": "fr",
            "德语 (de)": "de",
        }
        adv_params = {
            "lang": lang_map.get(values["lang"], "any"),
            "limit_time": values["limit_time"],
            "start_date": values["start_date"],
            "end_date": values["end_date"],
            "quick_mode": values.get("quick_mode", "是"),
        }
        config = _x_config(values, ("max_seed_works", "max_authors", "max_profile_works_per_author", "max_search_scrolls", "max_profile_scrolls", "slice_days", "page_load_timeout", "scroll_interval", "scroll_interval_min", "scroll_interval_max", "no_new_scroll_limit", "search_refresh_count", "search_refresh_interval", "scroll_px", "initial_load_delay"))
        return run_x_keyword_author_works_spider(
            _lines(values["keywords"]),
            adv_params,
            debug_port_from_cdp_url(_x_cdp_url(values)),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )


class XProfilesWindow(SimpleToolWindow):
    tool_id = "x_tweet_author_profiles"

    def tool_config_params(self):
        return [
            _browser_config_param(),
            ConfigParam("tweet_ready_timeout", "推文渲染等待(毫秒)", kind="int", default=12000, minimum=3000, maximum=60000, step=1000),
            ConfigParam("cooldown_every", "冷却间隔(个)", kind="int", default=5, minimum=1, maximum=50),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 博主信息",
            [
                FieldSpec(
                    "input_mode",
                    "输入方式",
                    kind="combo",
                    default="推文链接",
                    options=("推文链接", "博主链接"),
                ),
                FieldSpec("txt_path", "链接列表，每行一个", kind="text_or_file", required=True, placeholder="每行一个链接"),
            ],
        )

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.profiles import run_scraper

        config = _x_config(values, ("page_load_timeout", "tweet_ready_timeout", "cooldown_min", "cooldown_max", "cooldown_every"))
        return run_scraper(
            self._text_to_tempfile(values["txt_path"]),
            values["input_mode"],
            _x_cdp_url(values),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )


class XContextWindow(SimpleToolWindow):
    tool_id = "x_paired_context_metrics"

    def tool_config_params(self):
        return [
            _browser_config_param(),
            ConfigParam("context_size", "目标推文前后各取几条", kind="int", default=5, minimum=1, maximum=20),
            ConfigParam("max_profile_scrolls", "主页最大滚动次数", kind="int", default=45, minimum=5, maximum=300),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 推文上下文数据",
            [FieldSpec("txt_path", "推文链接 + 博主主页，每行一对", kind="text_or_file", required=True, placeholder="推文链接 博主主页链接")],
        )

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.context import run_scraper

        config = _x_config(values, ("context_size", "max_profile_scrolls", "scroll_interval", "page_load_timeout"))
        return run_scraper(self._text_to_tempfile(values["txt_path"]), _x_cdp_url(values), log_callback, finish_callback, stop_event, config=config, pause_event=pause_event)


class XTweetMetricsWindow(SimpleToolWindow):
    tool_id = "x_tweet_metrics"

    def tool_config_params(self):
        return [
            _browser_config_param(),
            ConfigParam("page_ready_wait", "页面就绪等待(秒)", kind="float", default=2.5, minimum=0.5, maximum=15.0, step=0.1, decimals=1,
                        tooltip="goto 后等待 React 渲染推文的缓冲时间。网络慢或经常未找到 DOM 可调大。"),
            ConfigParam("cooldown_every", "冷却间隔(条)", kind="int", default=3, minimum=1, maximum=50),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 推文详情采集",
            [
                FieldSpec("txt_path", "推文链接，每行一个", kind="text_or_file", required=True, placeholder="https://x.com/user/status/123"),
                FieldSpec("get_comments", "是否获取推文评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=500, minimum=10, maximum=10000),
            ],
        )
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.tweet_metrics import run_x_tweet_metrics_spider

        config = _x_config(values, ("page_load_timeout", "page_ready_wait", "comment_top_limit", "cooldown_every", "cooldown_min", "cooldown_max"))
        return run_x_tweet_metrics_spider(self._text_to_tempfile(values["txt_path"]), values["get_comments"], int(values["max_comments"]), _x_cdp_url(values), log_callback, finish_callback, stop_event, config=config, pause_event=pause_event)


class XProfileTweetsWindow(SimpleToolWindow):
    tool_id = "x_profile_tweets"

    def tool_config_params(self):
        return [
            _browser_config_param(),
            ConfigParam("max_tweets_per_author", "每个博主最新推文数", kind="int", default=50, minimum=1, maximum=5000),
            ConfigParam("max_scrolls", "主页最大滚动次数", kind="int", default=80, minimum=1, maximum=2000),
            ConfigParam("initial_load_delay", "初始加载等待(秒)", kind="float", default=2.0, minimum=0.5, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("page_load_timeout", "页面加载超时(ms)", kind="int", default=30000, minimum=5000, maximum=120000, step=1000),
            ConfigParam("scroll_interval_min", "滚动间隔最小(秒)", kind="float", default=2.4, minimum=0.5, maximum=30.0, step=0.1, decimals=1),
            ConfigParam("scroll_interval_max", "滚动间隔最大(秒)", kind="float", default=5.6, minimum=0.5, maximum=60.0, step=0.1, decimals=1),
            ConfigParam("scroll_px", "每次滚动像素", kind="int", default=2800, minimum=500, maximum=10000, step=100),
            ConfigParam("no_new_scroll_limit", "连续无新增停止阈值", kind="int", default=10, minimum=3, maximum=50,
                        tooltip="连续多少次滚动无新增帖子时停止。"),
            ConfigParam("save_batch_size", "批量保存条数", kind="int", default=10, minimum=1, maximum=100),
            ConfigParam("cooldown_min", "冷却最小秒数", kind="float", default=6.0, minimum=0, maximum=30.0, step=0.5, decimals=1),
            ConfigParam("cooldown_max", "冷却最大秒数", kind="float", default=15.0, minimum=0, maximum=60.0, step=0.5, decimals=1),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 博主推文采集",
            [
                FieldSpec(
                    "profile_urls",
                    "博主主页链接，每行一个",
                    kind="text_or_file",
                    placeholder="https://x.com/username",
                    required=True,
                ),
            ],
            height=660,
        )

    def validate_values(self, values):
        if not _lines(values["profile_urls"]):
            raise ValueError("至少需要输入一个 X 博主主页链接。")

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.profile_tweets import run_x_profile_tweets_spider

        config = _x_config(values, ("max_tweets_per_author", "page_load_timeout", "scroll_interval", "scroll_interval_min", "scroll_interval_max", "no_new_scroll_limit", "max_scrolls", "save_batch_size", "cooldown_min", "cooldown_max", "scroll_px", "initial_load_delay"))
        return run_x_profile_tweets_spider(
            values["profile_urls"],
            values.get("keywords", ""),
            "否",
            DEFAULT_START_DATE,
            DEFAULT_END_DATE,
            "否",
            0,
            _x_cdp_url(values),
            int(values.get("max_scrolls", 300)),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )


class XProfileBundleWindow(SimpleToolWindow):
    tool_id = "x_profile_bundle"

    def tool_config_params(self):
        return [
            _browser_config_param(),
            ConfigParam("max_tweets_per_author", "每个作者最新推文数", kind="int", default=50, minimum=1, maximum=5000),
            ConfigParam("max_scrolls", "主页最大滚动次数", kind="int", default=80, minimum=1, maximum=2000),
            ConfigParam("initial_load_delay", "初始加载等待(秒)", kind="float", default=2.0, minimum=0.5, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("page_load_timeout", "页面加载超时(ms)", kind="int", default=30000, minimum=5000, maximum=120000, step=1000),
            ConfigParam("scroll_interval_min", "滚动间隔最小(秒)", kind="float", default=2.4, minimum=0.5, maximum=30.0, step=0.1, decimals=1),
            ConfigParam("scroll_interval_max", "滚动间隔最大(秒)", kind="float", default=5.6, minimum=0.5, maximum=60.0, step=0.1, decimals=1),
            ConfigParam("scroll_px", "每次滚动像素", kind="int", default=2800, minimum=500, maximum=10000, step=100),
            ConfigParam("no_new_scroll_limit", "连续无新增停止阈值", kind="int", default=10, minimum=3, maximum=50),
        ]

    def __init__(self) -> None:
        super().__init__(
            "X 博主信息+推文采集",
            [
                FieldSpec(
                    "profile_urls",
                    "博主主页链接，每行一个",
                    kind="text_or_file",
                    placeholder="https://x.com/username",
                    required=True,
                ),
                FieldSpec("include_reposts", "是否包含转推/转发？", kind="combo", options=("是", "否"), default="否"),
            ],
            height=700,
        )

    def validate_values(self, values):
        if not _lines(values["profile_urls"]):
            raise ValueError("至少需要输入一个 X 博主主页链接。")

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.profile_bundle import run_x_profile_bundle_spider

        config = _x_config(values, ("max_tweets_per_author", "max_scrolls", "initial_load_delay", "page_load_timeout", "scroll_interval", "scroll_interval_min", "scroll_interval_max", "scroll_px", "no_new_scroll_limit", "include_reposts"))
        return run_x_profile_bundle_spider(
            values["profile_urls"],
            "否",
            DEFAULT_START_DATE,
            DEFAULT_END_DATE,
            _x_cdp_url(values),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )


class XCommentsWindow(SimpleToolWindow):
    """X (Twitter) 热门评论抓取窗口。

    读取每行一个推文链接的 TXT 文件，连接浏览器扫描首层评论，并按点赞量降序输出。
    """
    tool_id = "x_top_comments"

    def tool_config_params(self):
        return [_browser_config_param()]

    def __init__(self) -> None:
        super().__init__(
            "X 热门评论",
            [
                FieldSpec(
                    "txt_path",
                    "推文链接，每行一个",
                    kind="text_or_file",
                    required=True,
                    placeholder="https://x.com/username/status/1234567890",
                ),
                FieldSpec(
                    "max_scan_comments",
                    "每个推文最多扫描评论数",
                    kind="int",
                    default=500,
                    minimum=100,
                    maximum=10000,
                ),
            ],
        )

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.x_twitter.comments import run_x_top_comments_spider

        config = _x_config(values, ("page_load_timeout", "comment_top_limit", "scroll_interval", "no_new_scroll_limit"))
        return run_x_top_comments_spider(
            self._text_to_tempfile(values["txt_path"]),
            _x_cdp_url(values),
            int(values["max_scan_comments"]),
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )
