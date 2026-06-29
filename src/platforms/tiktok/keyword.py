"""
TikTok 关键词视频及评论检索采集模块。
该模块具备以下核心特性：
1. 关键词并发检索：利用 ThreadPoolExecutor，在多关键词配置下，支持开启多达 max_parallel_tabs 个工作线程，独立控制各个词的搜索页面、页面滚动与数据拉取。
2. 生产者-消费者双并发模型：对于单个关键词的抓取，主抓取线程作为生产者扫描搜索网格并异步提取视频交互详情，符合条件的视频任务会被推送入 comment_queue 队列。同时拉起 max_comment_tabs 个子线程作为消费者，异步且并发地对视频评论进行拉取与保存。
3. 队列限制与限流悬挂：队列使用带 maxsize 的 queue.Queue 以保护内存不被暴涨的视频撑爆。消费者拉取在遇到网络限流时有随机冷却和阻塞挂起检测机制，以对抗 TikTok 严格的高频访问控制。
"""

from __future__ import annotations

import html as html_lib
import json
import queue
import random
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime, timedelta

try:
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_IMPORT_ERROR = None
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via import-time fallback
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = exc

from src.core import (
    XlsxRowWriter,
    MultiSheetXlsxWriter,
    _is_page_closed,
    _recreate_page,
    build_output_path,
    connect_existing_chromium,
    ensure_chrome_for_cdp,
    expand_compact_number,
    extract_tiktok_video_title,
    interruptible_sleep,
    log_error,
    log_line,
    make_keyword_log,
    random_cooldown,
    resolve_tiktok_card_container,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)
from src.platforms.tiktok.comments import collect_video_comments
from src.platforms.tiktok.profiles import (
    extract_profile_row as extract_tiktok_profile_row,
    normalize_profile_url as normalize_tiktok_profile_url,
    profile_id_from_url as tiktok_profile_id_from_url,
)

# 导出的视频元数据 Excel 表格头部字段
CSV_FIELDS = [
    "搜索词",
    "序号",
    "视频标题",
    "播放量",
    "点赞数",
    "收藏量",
    "评论数",
    "发布时间",
    "视频链接",
    "博主主页链接",
    "博主名称",
    "博主ID",
    "粉丝量",
    "作者简介",
    "标签",
]


def ensure_playwright_available():
    if sync_playwright is None:
        raise ModuleNotFoundError("playwright is required for TikTok keyword scraping") from PLAYWRIGHT_IMPORT_ERROR


def _tiktok_media_tag(item: dict, page=None) -> str:
    """
    根据后端 JSON 数据对视频的媒体类型进行判定与分类：
    '0'=图片+视频, '1'=图片(图集), '2'=视频, '3'=纯文本, '4'=其它
    若接口数据缺失或结构更新，自动 fallback 到 DOM 元素匹配（检查 swiper、video 节点）。
    """
    has_image = bool(item.get("image_post_info") or item.get("imagePost"))
    has_video = bool(item.get("video") or item.get("videoInfo"))
    if has_image and has_video:
        return "0"
    if has_image:
        return "1"
    if has_video:
        return "2"
    # JSON 结构返回为空，触发 DOM 降级判定
    if page is not None:
        try:
            dom_has_image = page.locator('[data-e2e="browse-image-item"], [class*="DivPhoto"], swiper, [class*="Swiper"]').count() > 0
            dom_has_video = page.locator("video, [data-e2e='video-player'], [class*='VideoPlayer']").count() > 0
            if dom_has_image and dom_has_video:
                return "0"
            if dom_has_image:
                return "1"
            if dom_has_video:
                return "2"
        except Exception:
            pass
    return "3"


DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"
MIN_SEARCH_SCROLLS = 60  # 最少搜索页面滚动轮数
MAX_SEARCH_SCROLLS = 360  # 最大搜索页面滚动轮数上限
SEARCH_SCROLL_PAUSE = 0.7  # 两次滚动之间的稳定等待时间（秒）
DEFAULT_CANDIDATE_MULTIPLIER = 3  # 默认候选乘数，控制扫描数量上限


def parse_date_range(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    """
    解析并检验开始和结束日期字符串。
    """
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期")
    return start_dt, end_dt


def parse_publish_date(value: str) -> datetime | None:
    """
    正则提取文本中可能包含的发布日期（年-月-日）。
    """
    text = (value or "").strip()
    if not text:
        return None

    now = datetime.now()
    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    match = re.search(r"\b(\d{1,2})[-/.](\d{1,2})\b", text)
    if match:
        try:
            publish_dt = datetime(now.year, int(match.group(1)), int(match.group(2)))
            if publish_dt.date() > now.date() + timedelta(days=1):
                publish_dt = publish_dt.replace(year=publish_dt.year - 1)
            return publish_dt
        except ValueError:
            return None

    lowered = text.lower()
    if "刚刚" in text or "just now" in lowered:
        return now
    if "昨天" in text or "yesterday" in lowered:
        return now - timedelta(days=1)

    relative_match = re.search(
        r"(\d+)\s*(秒|分钟|小时|天|周|月|年|sec|second|minute|min|hour|hr|day|week|month|year|s|m|h|d|w)\s*(前|ago)?",
        text,
        re.IGNORECASE,
    )
    if not relative_match:
        return None

    amount = int(relative_match.group(1))
    unit = relative_match.group(2).lower()
    if unit in {"秒", "sec", "second", "s"}:
        return now - timedelta(seconds=amount)
    if unit in {"分钟", "minute", "min", "m"}:
        return now - timedelta(minutes=amount)
    if unit in {"小时", "hour", "hr", "h"}:
        return now - timedelta(hours=amount)
    if unit in {"天", "day", "d"}:
        return now - timedelta(days=amount)
    if unit in {"周", "week", "w"}:
        return now - timedelta(weeks=amount)
    if unit in {"月", "month"}:
        return now - timedelta(days=amount * 30)
    if unit in {"年", "year"}:
        return now - timedelta(days=amount * 365)
    return None


def in_date_range(publish_time: str, start_dt: datetime, end_dt: datetime) -> bool:
    """
    判断发布时间是否包含在设定的日期过滤区间中。
    """
    publish_dt = parse_publish_date(publish_time)
    if not publish_dt:
        return False
    return start_dt.date() <= publish_dt.date() <= end_dt.date()


def clean_url(url: str) -> str:
    """
    去除 URL 的推荐参数等。
    """
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.tiktok.com" + value
    if not value.startswith("http"):
        value = "https://" + value
    return value.split("?")[0].split("#")[0]


def safe_filename_part(value: str) -> str:
    """
    对文件名中的特殊符号进行过滤与安全编码。
    """
    cleaned = re.sub(r'[\\/*?:"<>|]', "", value or "").strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:80] or "keyword"


def extract_author_url(video_url: str) -> str:
    """
    从视频 URL 中正则匹配作者句柄，并拼成主页 URL。
    """
    value = clean_url(video_url)
    for pattern in (r"tiktok\.com/(@[^/?#]+)/video/", r"(?:^|/)@([^/?#]+)/video/"):
        match = re.search(pattern, value or "")
        if not match:
            continue
        handle = match.group(1)
        if not handle.startswith("@"):
            handle = f"@{handle}"
        return f"https://www.tiktok.com/{handle}"
    return ""


def extract_tiktok_video_id(url: str) -> str:
    """
    提取纯数字视频 ID。
    """
    match = re.search(r"/video/(\d+)", url or "")
    return match.group(1) if match else ""


def derive_publish_time_from_video_url(video_url: str) -> str:
    """
    从 TikTok 视频 ID 的高位时间戳推导发布时间，作为最终兜底。
    """
    video_id = extract_tiktok_video_id(video_url)
    if not video_id or not video_id.isdigit():
        return ""
    try:
        unix_ts = int(video_id) >> 32
        if unix_ts > 1500000000:
            return format_publish_time(unix_ts)
    except Exception:
        return ""
    return ""


def format_plain_text(value) -> str:
    """
    过滤 None、NaN 类似空串。
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"none", "null", "undefined", "nan"} else text


def format_count(value) -> str:
    """
    规整数字。
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    if text.lower() in {"none", "null", "undefined", "nan"}:
        return ""
    return expand_compact_number(text)


def count_to_int(value) -> int:
    """
    统一数字指标强转整型以进行数值对比。
    """
    text = format_count(value).replace(",", "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def format_publish_time(value) -> str:
    """
    格式化发布时间戳。
    """
    try:
        timestamp = int(str(value).strip())
        if timestamp > 10**12:
            timestamp //= 1000
        if timestamp > 0:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except Exception:
        pass
    return format_plain_text(value)


def normalize_publish_time_text(value: str) -> str:
    """
    将 TikTok 页面上可能出现的多种发布时间文本归一化为可比较格式。
    """
    text = format_plain_text(value)
    if not text:
        return ""

    normalized_from_timestamp = format_publish_time(text)
    if normalized_from_timestamp and normalized_from_timestamp != text:
        return normalized_from_timestamp

    try:
        iso_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return iso_dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    publish_dt = parse_publish_date(text)
    if publish_dt is not None:
        return publish_dt.strftime("%Y-%m-%d %H:%M:%S")
    return text


def iter_dicts(value):
    """
    深度优先遍历任意嵌套字典或列表，生成其中所有的 dict 子节点。
    """
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def parse_script_json(html: str, script_id: str):
    """
    匹配 HTML 中的特定 script 标签并反序列化 JSON。
    """
    pattern = rf'<script[^>]+id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.S)
    if not match:
        return None
    try:
        return json.loads(html_lib.unescape(match.group(1)).strip())
    except Exception:
        return None


def page_state_sources(page) -> list[dict]:
    """
    从页面中获取 SIGI_STATE 与 __UNIVERSAL_DATA_FOR_REHYDRATION__ 状态源。
    """
    sources: list[dict] = []
    try:
        raw = page.evaluate(
            """() => JSON.stringify({
                sigi: window.SIGI_STATE || null,
                universal: window.__UNIVERSAL_DATA_FOR_REHYDRATION__ || null
            })"""
        )
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                sources.append(data)
    except Exception:
        pass

    try:
        html = page.content()
        for script_id in ("SIGI_STATE", "__UNIVERSAL_DATA_FOR_REHYDRATION__"):
            data = parse_script_json(html, script_id)
            if isinstance(data, dict):
                sources.append(data)
    except Exception:
        pass
    return sources


def find_item_in_state(sources: list[dict], video_id: str) -> dict:
    """
    从候选反序列化数据源中查找 video_id 对应的 Item 字典。
    """
    if not video_id:
        return {}
    for source in sources:
        for item_module_key in ("ItemModule", "itemModule"):
            item_module = source.get(item_module_key)
            if isinstance(item_module, dict):
                item = item_module.get(video_id)
                if isinstance(item, dict):
                    return item
        for node in iter_dicts(source):
            item_struct = node.get("itemStruct")
            if isinstance(item_struct, dict) and str(item_struct.get("id", "")) == video_id:
                return item_struct
            if str(node.get("id", "")) == video_id and ("stats" in node or "createTime" in node or "desc" in node):
                return node
    return {}


def item_metric(item: dict, *keys: str) -> str:
    """
    从 Item 字典的各个 stats 变体结构中提取指定属性的计数值。
    """
    stats_sources = []
    for key in ("stats", "statsV2", "stats_v2", "statistics"):
        value = item.get(key)
        if isinstance(value, dict):
            stats_sources.append(value)
    stats_sources.append(item)
    for source in stats_sources:
        for key in keys:
            if key in source:
                value = format_count(source.get(key))
                if value:
                    return value
    return ""


def item_metrics(item: dict) -> dict[str, str]:
    """
    统一格式化获取 Item 里的标题、播放量、点赞量、收藏量、评论数及发布时间。
    """
    if not item:
        return {}
    return {
        "视频标题": format_plain_text(item.get("desc") or item.get("description")),
        "播放量": item_metric(item, "playCount", "play_count", "viewCount", "view_count", "play_count_str"),
        "点赞数": item_metric(item, "diggCount", "digg_count", "digg_count_str", "likeCount", "like_count", "like_count_str"),
        "收藏量": item_metric(
            item, "collectCount", "collect_count", "favoriteCount", "favouriteCount", "favorite_count", "favourite_count", "saveCount", "save_count"
        ),
        "评论数": item_metric(item, "commentCount", "comment_count", "comments"),
        "发布时间": format_publish_time(item.get("createTime") or item.get("create_time")),
    }

def _first_count_from_sources(sources: list[dict], keys: tuple[str, ...]) -> str:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source:
                value = format_count(source.get(key))
                if value:
                    return value
    return ""


def author_profile_from_item(item: dict, fallback_profile_url: str = "") -> dict[str, str]:
    """
    从 TikTok 详情页状态树中的 author / authorStats 提取作者基础信息。
    这一步不需要跳转主页，能显著降低关键词采样时的风控概率。
    """
    profile_url = normalize_tiktok_profile_url(fallback_profile_url) or fallback_profile_url
    author = item.get("author") if isinstance(item, dict) else None
    stats_sources: list[dict] = []
    unique_id = ""
    author_name = ""
    bio = ""

    if isinstance(author, dict):
        unique_id = format_plain_text(author.get("uniqueId") or author.get("unique_id") or author.get("username")).lstrip("@")
        author_name = format_plain_text(author.get("nickname") or author.get("nickName") or author.get("displayName") or author.get("name"))
        bio = format_plain_text(author.get("signature") or author.get("bio") or author.get("description") or author.get("desc"))
        for key in ("stats", "statsV2", "stats_v2", "statistics"):
            if isinstance(author.get(key), dict):
                stats_sources.append(author[key])
    elif isinstance(author, str):
        unique_id = format_plain_text(author).lstrip("@")

    for key in ("authorStats", "author_stats", "authorStatsV2", "author_stats_v2", "stats"):
        if isinstance(item.get(key), dict):
            stats_sources.append(item[key])

    if unique_id:
        profile_url = f"https://www.tiktok.com/@{unique_id}"
    author_id = f"@{unique_id}" if unique_id else tiktok_profile_id_from_url(profile_url)
    followers = _first_count_from_sources(
        stats_sources,
        ("followerCount", "follower_count", "followers", "fans", "fansCount", "fans_count"),
    )
    return {
        "博主主页链接": normalize_tiktok_profile_url(profile_url) or profile_url,
        "博主名称": author_name or author_id,
        "博主ID": author_id,
        "粉丝量": followers,
        "作者简介": bio.replace("\r", "").replace("\n", " | "),
    }


def extract_author_url_from_detail_page(page) -> str:
    """
    从当前详情页 DOM 中兜底提取作者主页链接。
    """
    try:
        href = page.evaluate(
            """() => {
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    if (/\\/[^/?#]*@[^/?#]+($|[?#])/.test(href) && !href.includes('/video/')) return href;
                    if (/tiktok\\.com\\/@[^/?#]+($|[?#])/.test(href) && !href.includes('/video/')) return href;
                }
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    if (/\\/@[^/?#]+\\/video\\//.test(href)) return href;
                    if (/tiktok\\.com\\/@[^/?#]+\\/video\\//.test(href)) return href;
                }
                return '';
            }"""
        )
    except Exception:
        href = ""
    return normalize_tiktok_profile_url(href) or extract_author_url(href)


def extract_metric(page, data_e2e_candidates, removable_words=(), default=""):
    """
    UI 模式：通过 data-e2e 标记元素提取数值。
    """
    candidates = data_e2e_candidates if isinstance(data_e2e_candidates, (list, tuple)) else [data_e2e_candidates]
    for data_e2e in candidates:
        try:
            loc = page.locator(f"[data-e2e='{data_e2e}']").first
            if loc.count() <= 0:
                continue
            text = loc.inner_text(timeout=2500).strip()
            for word in removable_words:
                text = text.replace(word, "")
            text = text.strip()
            if text:
                return expand_compact_number(text)
        except Exception:
            continue
    return default


def extract_publish_time(page) -> str:
    """
    UI 模式：通过正则或选择器提取发布时间。
    """
    try:
        html = page.content()
        match = re.search(r'"createTime":"?(\d{10,13})"?', html)
        if match:
            return format_publish_time(match.group(1))
    except Exception:
        pass

    for selector in [
        "span[data-e2e='browser-nickname'] + span + span",
        "span[data-e2e='video-create-time']",
        "time",
    ]:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                if selector == "time":
                    datetime_attr = format_plain_text(loc.get_attribute("datetime"))
                    if datetime_attr:
                        normalized_attr = normalize_publish_time_text(datetime_attr)
                        if normalized_attr:
                            return normalized_attr
                text = loc.inner_text(timeout=1500).strip()
                if text:
                    return normalize_publish_time_text(text)
        except Exception:
            continue
    return ""


def extract_card_play_count(anchor) -> str:
    """
    从视频列表卡片 DOM 中抓取播放量指标（避免频繁进入详情页以防风控）。
    """
    try:
        container = resolve_tiktok_card_container(anchor)
        for selector in [
            "[data-e2e='video-views']",
            "strong[data-e2e='video-views']",
            "span[data-e2e='video-views']",
        ]:
            node = container.query_selector(selector)
            if node:
                text = node.inner_text().strip()
                if text:
                    return expand_compact_number(text)
    except Exception:
        pass
    return ""


def extract_author_url_from_card(anchor, video_url: str = "") -> str:
    """
    从搜索卡片周边 DOM 提取作者主页链接。若卡片 URL 已含 @user/video，优先使用 URL。
    """
    profile_url = extract_author_url(video_url)
    if profile_url:
        return profile_url
    try:
        href = anchor.evaluate(
            """a => {
                const root = a.closest('article, section, div[data-e2e], div[class]') || a.parentElement || a;
                const anchors = [a, ...Array.from(root.querySelectorAll('a[href]'))];
                for (const link of anchors) {
                    const href = link.getAttribute('href') || '';
                    if (/\\/@[^/?#]+($|[?#])/.test(href) && !href.includes('/video/')) return href;
                    if (/tiktok\\.com\\/@[^/?#]+($|[?#])/.test(href) && !href.includes('/video/')) return href;
                }
                for (const link of anchors) {
                    const href = link.getAttribute('href') || '';
                    if (/\\/@[^/?#]+\\/video\\//.test(href)) return href;
                    if (/tiktok\\.com\\/@[^/?#]+\\/video\\//.test(href)) return href;
                }
                return '';
            }"""
        )
    except Exception:
        href = ""
    return normalize_tiktok_profile_url(href) or extract_author_url(href)


def dynamic_search_scroll_limit(max_videos: int, max_search_scrolls: int = MAX_SEARCH_SCROLLS) -> int:
    return min(max_search_scrolls, max(MIN_SEARCH_SCROLLS, max_videos // 8 + 40))


def default_candidate_scan_limit(max_videos: int) -> int:
    return max(max_videos, min(max_videos * DEFAULT_CANDIDATE_MULTIPLIER, max_videos + 3000))


def trigger_search_lazy_load(page):
    """
    触发搜索页面的下拉懒加载：
    - 垂直滚动至底部；
    - 对所有带有滚动条的 overflow 子元素派发 scroll 滚动事件，唤醒 TikTok 网格的懒加载监听器。
    - 结合 mouse.wheel 与键盘 End 键做强力懒加载触发。
    """
    try:
        page.evaluate(
            """() => {
                const scrolling = document.scrollingElement || document.documentElement || document.body;
                scrolling.scrollTop = scrolling.scrollHeight;
                const scrollable = Array.from(document.querySelectorAll('body, main, section, div'))
                    .filter(el => {
                        const style = getComputedStyle(el);
                        return el.scrollHeight > el.clientHeight + 80 &&
                            ['auto', 'scroll', 'overlay'].includes(style.overflowY);
                    })
                    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                for (const el of scrollable.slice(0, 6)) {
                    el.scrollTop = el.scrollHeight;
                    el.dispatchEvent(new Event('scroll', {bubbles: true}));
                }
                window.dispatchEvent(new Event('scroll'));
            }"""
        )
    except Exception:
        pass
    try:
        page.mouse.wheel(0, 4200)
    except Exception:
        pass
    try:
        page.keyboard.press("End")
    except Exception:
        pass


def collect_visible_video_items(page, seen_links: set[str]) -> list[dict[str, str]]:
    """
    抓取当前视口内所有已加载视频卡片的 URL 及对应的播放量。
    """
    items: list[dict[str, str]] = []
    try:
        anchors = page.locator("a[href*='/video/'], a[href*='video/']").all()
    except Exception:
        anchors = []

    for anchor in anchors:
        try:
            href = clean_url(anchor.get_attribute("href") or "")
        except Exception:
            href = ""
        if href and "/video/" in href and href not in seen_links:
            items.append({
                "视频链接": href,
                "播放量": extract_card_play_count(anchor),
                "博主主页链接": extract_author_url_from_card(anchor, href),
            })
            seen_links.add(href)
    return items


# TikTok 错误态检测：错误文案关键词与可能的重试/刷新按钮选择器。
# 命中文案即判定当前为错误页，需要点击重试或重新导航。
_TIKTOK_ERROR_TEXTS = (
    "出错了",
    "服务器出现问题",
    "请重试",
    "网络异常",
    "加载失败",
    "something went wrong",
    "server issue",
    "try again",
    "unable to load",
    "network error",
)
_TIKTOK_RETRY_SELECTORS = (
    "button:has-text('重试')",
    "button:has-text('刷新')",
    "button:has-text('Try again')",
    "button:has-text('Refresh')",
    "a:has-text('重试')",
    "a:has-text('刷新')",
    "div:has-text('重试')",
    "span:has-text('重试')",
    "[role='button']:has-text('重试')",
    "[role='button']:has-text('Try again')",
    "[data-e2e='search-result-retry']",
    "[data-e2e*='retry']",
    "[data-e2e*='refresh']",
)
# JS 定位重试元素中心坐标脚本：返回 {x, y} 或 null。
# 用途：Playwright 选择器 click 命不中自定义组件时，先取元素 bounding box 中心坐标，
# 再用 page.mouse.click(x, y) 真实点击坐标。mouse.click 产生 trusted 事件（与真人
# 点击等价），而 evaluate(el.click()) 产生 non-trusted 事件会被 TikTok 反爬忽略。
_TIKTOK_RETRY_BBOX_JS = """
() => {
    const keywords = ['重试', '刷新', 'Try again', 'Refresh', 'try again', 'refresh'];
    const candidates = Array.from(document.querySelectorAll(
        'button, a, div, span, [role="button"], [data-e2e]'
    ));
    for (const el of candidates) {
        const text = (el.textContent || '').trim();
        if (text.length > 30) continue;
        if (!keywords.some(k => text.includes(k))) continue;
        try {
            el.scrollIntoView({block: 'center'});
        } catch (e) { continue; }
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        // 跳过不可见元素（display:none / visibility:hidden）
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) continue;
        return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
    }
    return null;
}
"""


def _detect_tiktok_search_error(page) -> str | None:
    """
    检测页面是否处于错误态。返回命中的错误文案，未命中返回 None。
    通过页面可见文本判断，兼容不同地区/语言的错误页。适用于搜索页与详情页。
    """
    try:
        body_text = page.inner_text("body", timeout=2000)
    except Exception:
        return None
    lowered = body_text.lower()
    for text in _TIKTOK_ERROR_TEXTS:
        if text in body_text or text.lower() in lowered:
            return text
    return None


def _click_tiktok_retry(page) -> bool:
    """
    尝试点击 TikTok 错误页上的重试/刷新按钮。成功返回 True。

    点击策略（按可信度从高到低）：
    1. Playwright loc.click()：trusted 事件，但依赖选择器精确匹配。
    2. JS 取重试元素中心坐标 + page.mouse.click(x, y)：trusted 事件且精确命中坐标，
       最接近真人点击，能命中 loc.click 选不到的自定义组件。
    （不再用 evaluate(el.click())——它产生 non-trusted 事件会被 TikTok 反爬忽略，
    这正是"人按有效、程序按无效"的根因。）
    """
    # 1. 选择器点击
    for selector in _TIKTOK_RETRY_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                loc.click(timeout=3000)
                return True
        except Exception:
            continue
    # 2. JS 取坐标 + 真实鼠标点击（trusted，绕过反爬对程序点击的拦截）
    try:
        bbox = page.evaluate(_TIKTOK_RETRY_BBOX_JS)
        if bbox and isinstance(bbox, dict) and bbox.get("x") is not None and bbox.get("y") is not None:
            # 先移动到目标附近再点击，更接近真人操作
            page.mouse.move(bbox["x"], bbox["y"])
            time.sleep(0.15)
            page.mouse.click(bbox["x"], bbox["y"])
            return True
    except Exception:
        pass
    return False


def _retry_backoff_seconds(attempt: int) -> float:
    """
    指数退避：2^attempt + 随机抖动，上限 15 秒。给 TikTok 服务端恢复时间。
    attempt 从 1 起：约 2→4→8→15→15 秒。
    """
    return min(2 ** attempt + random.uniform(0, 1), 15.0)


def open_search_page(page, keyword: str, stop_event=None, log_callback=None, max_attempts: int = 5, pause_event=None):
    """
    打开 TikTok 关键词搜索页，并检测错误态自动重试。

    TikTok 在首启或风控时可能返回"出错了，服务器出现问题，请重试"错误页，
    若不处理直接滚动，会导致所有候选因页面无内容被跳过。本函数在导航后
    检测错误态，优先点击页面上的重试按钮（含 JS 兜底），其次重新 goto/reload，
    采用指数退避循环重试至成功或耗尽次数。
    """
    search_url = f"https://www.tiktok.com/search/video?q={urllib.parse.quote(keyword)}"
    for attempt in range(1, max_attempts + 1):
        if should_stop(stop_event):
            return
        if wait_if_paused(pause_event, stop_event):
            return
        # 第 3 次及以后改用 reload 复用会话缓存，有时比重新 goto 更易恢复
        use_reload = attempt >= 3
        try:
            if use_reload:
                page.reload(wait_until="domcontentloaded", timeout=45000)
            else:
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            log_line(log_callback, f"  搜索页导航失败（第 {attempt}/{max_attempts} 次）：{exc}")
            interruptible_sleep(_retry_backoff_seconds(attempt), stop_event, pause_event=pause_event)
            continue

        # 等待页面初步稳定，再判断是否出现视频卡片或错误态
        interruptible_sleep(random.uniform(1.8, 2.8), stop_event, pause_event=pause_event)

        error_text = _detect_tiktok_search_error(page)
        if error_text is None:
            return  # 页面正常

        log_line(log_callback, f"  搜索页出现错误态「{error_text}」（第 {attempt}/{max_attempts} 次），尝试重试...")
        # 错误态多为风控触发，立刻点重试常因风控未冷却而再次出错。先等待冷却。
        cooldown = random.uniform(5.0, 9.0) if attempt <= 2 else _retry_backoff_seconds(attempt)
        log_line(log_callback, f"  等待风控冷却 {cooldown:.1f}s 后再重试...")
        interruptible_sleep(cooldown, stop_event, pause_event=pause_event)
        # 优先点击页面上的重试按钮（真实鼠标坐标点击，绕过反爬对程序点击的拦截）
        if _click_tiktok_retry(page):
            # 点击后等结果加载，给 TikTok 重新渲染搜索结果的时间
            interruptible_sleep(random.uniform(4.0, 6.0), stop_event, pause_event=pause_event)
            if _detect_tiktok_search_error(page) is None:
                log_line(log_callback, "  点击重试后页面已恢复。")
                return
        # 仍处于错误态，指数退避后下一轮重新导航/reload
        interruptible_sleep(_retry_backoff_seconds(attempt), stop_event, pause_event=pause_event)

    log_line(log_callback, f"  搜索页重试 {max_attempts} 次仍处于错误态，继续尝试采集（可能无结果）。")


def extract_video_row(page, keyword: str, video_url: str, play_count: str = "", profile_url: str = "", stop_event=None, pause_event=None) -> dict:
    # 详情页 goto 带错误态检测与重试：TikTok 详情页也可能返回错误页，
    # 命中则 reload + 退避重试，最多 2 次；仍失败则按空指标返回（让上层按发布时间为空跳过）。
    for _goto_attempt in range(3):
        if wait_if_paused(pause_event, stop_event):
            break
        page.goto(video_url, wait_until="domcontentloaded", timeout=25000)
        interruptible_sleep(random.uniform(0.25, 0.55), stop_event, pause_event=pause_event)
        if _detect_tiktok_search_error(page) is None:
            break
        # 详情页错误态：reload 重试
        try:
            page.reload(wait_until="domcontentloaded", timeout=25000)
        except Exception:
            pass
        interruptible_sleep(_retry_backoff_seconds(_goto_attempt + 1), stop_event, pause_event=pause_event)
    try:
        wait_if_paused(pause_event, stop_event)
        page.wait_for_selector(
            "script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE, script#RENDER_DATA, [data-e2e='like-count'], [data-e2e='browser-nickname']",
            timeout=8000,
        )
    except Exception:
        pass
    item = {}
    for _ in range(4):
        if wait_if_paused(pause_event, stop_event):
            break
        item = find_item_in_state(page_state_sources(page), extract_tiktok_video_id(video_url))
        if item and (item.get("createTime") or item.get("create_time") or item.get("desc") or item.get("description")):
            break
        if interruptible_sleep(0.8, stop_event, pause_event=pause_event):
            break
    json_metrics = item_metrics(item)
    publish_time = normalize_publish_time_text(json_metrics.get("发布时间") or extract_publish_time(page))
    author_profile = author_profile_from_item(item, profile_url or extract_author_url(video_url))
    if not author_profile.get("博主主页链接"):
        author_profile["博主主页链接"] = extract_author_url(page.url) or extract_author_url_from_detail_page(page)

    if not publish_time:
        # 网络不稳定或纯 CSR 渲染较慢时，增加额外等待时间以确保页面渲染出时间元素
        try:
            wait_if_paused(pause_event, stop_event)
            page.wait_for_selector("[data-e2e='video-create-time'], span[data-e2e='browser-nickname'] + span + span, time", timeout=8000)
            item = find_item_in_state(page_state_sources(page), extract_tiktok_video_id(video_url))
            json_metrics = item_metrics(item)
            publish_time = normalize_publish_time_text(json_metrics.get("发布时间") or extract_publish_time(page))
        except Exception:
            pass
    if not publish_time:
        publish_time = derive_publish_time_from_video_url(video_url)
    play_value = json_metrics.get("播放量") or play_count
    dom_like_value = extract_metric(page, "like-count", ["Likes", "Like", "赞", " "])
    like_value = json_metrics.get("点赞数") or dom_like_value
    if play_value and like_value and count_to_int(play_value) == count_to_int(like_value):
        if dom_like_value and count_to_int(dom_like_value) != count_to_int(play_value):
            like_value = dom_like_value
    row = {
        "搜索词": keyword,
        "序号": "",
        "视频标题": json_metrics.get("视频标题") or extract_tiktok_video_title(page),
        "播放量": play_value,
        "点赞数": like_value,
        "收藏量": json_metrics.get("收藏量")
        or extract_metric(page, ["favorite-count", "undefined-count"], ["Favorites", "Favorite", "Favourites", "Favourite", "收藏", " "]),
        "评论数": json_metrics.get("评论数") or extract_metric(page, "comment-count", ["Comments", "Comment", "评论", "評論", " "]),
        "发布时间": publish_time,
        "视频链接": video_url,
        "标签": _tiktok_media_tag(item, page=page),
    }
    row.update(author_profile)
    if not row.get("博主主页链接"):
        row["博主主页链接"] = extract_author_url(video_url)
    return row


def _make_keyword_log_callback(base_log_callback, keyword: str):
    """Wrap log_callback to prefix messages with [keyword] for disambiguation."""
    return make_keyword_log(base_log_callback, keyword)


AUTHOR_PROFILE_FIELDS = ("博主名称", "博主ID", "粉丝量", "作者简介")


def tiktok_profile_cache_key(profile_url: str) -> str:
    normalized = normalize_tiktok_profile_url(profile_url) or profile_url
    match = re.search(r"tiktok\.com/@([^/?#]+)", normalized or "")
    return match.group(1).lower() if match else normalized.lower()


def enrich_tiktok_profile_fields(page, row: dict[str, str], cache: dict[str, dict[str, str]], log, stop_event=None, pause_event=None) -> dict[str, str]:
    """
    对详情页没有给全的作者字段，进入博主主页补齐；按作者缓存，减少重复跳转。
    """
    profile_url = normalize_tiktok_profile_url(row.get("博主主页链接", "")) or row.get("博主主页链接", "")
    if not profile_url:
        return row
    if all(row.get(field) for field in AUTHOR_PROFILE_FIELDS):
        return row

    cache_key = tiktok_profile_cache_key(profile_url)
    profile_record = cache.get(cache_key)
    if profile_record is None:
        try:
            profile_record = extract_tiktok_profile_row(
                page,
                profile_url,
                page_load_timeout=25000,
                captcha_wait=8,
                stop_event=stop_event,
                pause_event=pause_event,
            )
        except Exception as exc:
            log(f"    博主主页补充失败，保留详情页字段：{profile_url}，{exc}")
            profile_record = {
                "博主主页链接": profile_url,
                "博主ID": tiktok_profile_id_from_url(profile_url),
            }
        cache[cache_key] = profile_record

    for field in ("博主主页链接", *AUTHOR_PROFILE_FIELDS):
        if not row.get(field) and profile_record.get(field):
            row[field] = profile_record[field]
    return row


def _tiktok_comment_consumer(
    keyword, queue_obj, cdp_port_or_url, writer, writer_lock, log_callback, stop_event, pause_event, comment_top_limit, consumers_ready=None
):
    """
    评论消费者线程函数：创建独立的 Playwright 连接与页面，从队列中消费视频任务并抓取评论。
    """
    log = _make_keyword_log_callback(log_callback, keyword)
    comments_page = None
    browser = None
    try:
        # 使用独立的 Playwright 实例防止多线程冲突
        with sync_playwright() as p:
            try:
                # 连接至已有的 Chromium 浏览器实例
                browser, context = connect_existing_chromium(p, cdp_port_or_url)
                comments_page = context.new_page()
            except Exception as exc:
                log(f"    评论线程连接浏览器失败: {exc}")
                return
            # 浏览器初始化完毕，通知生产者可以开始推送任务
            if consumers_ready is not None:
                consumers_ready.set()
            try:
                while True:
                    try:
                        # 从任务队列获取待抓取的视频（包含序号、链接、最大扫描数）
                        item = queue_obj.get(timeout=3)
                    except Exception:
                        # 队列超时后检查是否需要退出或暂停
                        if should_stop(stop_event):
                            break
                        if wait_if_paused(pause_event, stop_event):
                            break
                        continue
                    # None 作为哨兵值，表示结束信号
                    if item is None:
                        break
                    if should_stop(stop_event):
                        break
                    if wait_if_paused(pause_event, stop_event):
                        break
                    serial_number, video_url, max_scan = item
                    try:
                        # 执行评论采集逻辑
                        comments = collect_video_comments(
                            comments_page,
                            video_url,
                            max_scan,
                            log,
                            stop_event,
                            pause_event=pause_event,
                            comment_top_limit=comment_top_limit,
                        )
                        # 写文件时加锁以防止多线程并发写入冲突
                        with writer_lock:
                            comment_count = 0
                            for comment in comments:
                                comment_row = {
                                    "序号": str(serial_number),
                                    "视频链接": video_url,
                                    "评论的点赞量": comment.get("like_count", ""),
                                    "评论内容": comment.get("text", ""),
                                    "发布时间": comment.get("create_time", ""),
                                }
                                writer.writerow("评论信息", sanitize_csv_row(comment_row))
                                comment_count += 1
                                # 每写入 20 条保存一次，防止数据丢失
                                if comment_count % 20 == 0:
                                    writer.save()
                    except Exception as exc:
                        log(f"评论采集异常: {exc}")
            finally:
                if comments_page is not None and not comments_page.is_closed():
                    try:
                        comments_page.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as exc:
        log(f"评论线程异常: {exc}")


def _tiktok_metrics_consumer(
    keyword,
    metrics_queue,
    cdp_port_or_url,
    writer,
    writer_lock,
    log_callback,
    stop_event,
    pause_event,
    start_dt,
    end_dt,
    get_comments_bool,
    max_comments,
    comment_queue,
    consumers_ready,
    comment_consumers_ready,
    written_counter,
    cooldown_min,
    cooldown_max,
):
    """
    详情页指标消费者线程：创建独立的 Playwright 连接与页面，从队列并发消费候选视频任务，
    提取详情指标、做日期过滤、写入视频信息行，并视情况推送评论抓取任务。

    结构与 _tiktok_comment_consumer 对称，实现单关键词内的详情页多 tab 并发。
    """
    log = _make_keyword_log_callback(log_callback, keyword)
    metrics_page = None
    browser = None
    profile_cache: dict[str, dict[str, str]] = {}
    try:
        with sync_playwright() as p:
            try:
                browser, context = connect_existing_chromium(p, cdp_port_or_url)
                metrics_page = context.new_page()
            except Exception as exc:
                log(f"    详情线程连接浏览器失败: {exc}")
                return
            if consumers_ready is not None:
                consumers_ready.set()
            try:
                while True:
                    try:
                        item = metrics_queue.get(timeout=3)
                    except Exception:
                        if should_stop(stop_event):
                            break
                        if wait_if_paused(pause_event, stop_event):
                            break
                        continue
                    if item is None:
                        break
                    if should_stop(stop_event):
                        break
                    if wait_if_paused(pause_event, stop_event):
                        break
                    if len(item) >= 4:
                        serial_number, video_url, play_count, profile_url = item
                    else:
                        serial_number, video_url, play_count = item
                        profile_url = ""
                    try:
                        # 详情页可能被关闭，检测并重建
                        if _is_page_closed(metrics_page):
                            metrics_page = _recreate_page(context, metrics_page)
                        # 提取指标；goto 撞上 page 关闭则重建重试一次
                        try:
                            row = extract_video_row(metrics_page, keyword, video_url, play_count, profile_url=profile_url, stop_event=stop_event, pause_event=pause_event)
                        except Exception as row_exc:
                            if not _is_page_closed(metrics_page):
                                raise
                            log(f"  详情页抓取失败（{row_exc}），重建页面后重试...")
                            metrics_page = _recreate_page(context, metrics_page)
                            row = extract_video_row(metrics_page, keyword, video_url, play_count, profile_url=profile_url, stop_event=stop_event, pause_event=pause_event)

                        # 时间范围过滤
                        if start_dt is not None:
                            if not in_date_range(row["发布时间"], start_dt, end_dt):
                                log(f"    跳过：发布时间不在范围内（{row['发布时间'] or '未解析'}）")
                                continue

                        row = enrich_tiktok_profile_fields(metrics_page, row, profile_cache, log, stop_event=stop_event, pause_event=pause_event)
                        row["序号"] = str(serial_number)

                        # 写入视频信息行（加锁防并发冲突）
                        with writer_lock:
                            if get_comments_bool:
                                writer.writerow("视频信息", sanitize_csv_row(row))
                            else:
                                writer.writerow(sanitize_csv_row(row))
                        with written_counter["lock"]:
                            written_counter["count"] += 1
                            written = written_counter["count"]
                        log(f"  [已写{written}] {video_url}")

                        # 推送评论抓取任务
                        if get_comments_bool and comment_queue is not None and count_to_int(row.get("评论数", "0")) > 0:
                            if comment_consumers_ready is not None and comment_consumers_ready.wait(timeout=0.5):
                                try:
                                    comment_queue.put((serial_number, video_url, max_comments), block=True, timeout=15)
                                except Exception:
                                    log("    评论队列已满或消费线程异常，跳过本条评论采集。")
                            else:
                                log("    跳过评论采集：评论消费线程连接失败。")
                    except Exception as exc:
                        log(f"    跳过：{exc}")
            finally:
                if metrics_page is not None and not metrics_page.is_closed():
                    try:
                        metrics_page.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as exc:
        log(f"详情线程异常: {exc}")


def _scrape_single_tiktok_keyword(
    keyword,
    keyword_index,
    total_keywords,
    max_videos,
    max_candidates,
    start_dt,
    end_dt,
    get_comments_bool,
    max_comments,
    max_comment_tabs,
    max_metrics_tabs,
    max_queue_size,
    cdp_port_or_url,
    log_callback,
    stop_event,
    pause_event,
    search_scroll_pause,
    config_max_search_scrolls,
    no_new_scroll_limit,
    comment_top_limit,
    run_stamp,
    cooldown_min=3.0,
    cooldown_max=8.0,
    shared_search_page=None,
):
    """
    抓取单个 TikTok 关键词的线程函数。
    根据指定的限制参数，滚动搜索结果页面，提取视频元数据。如果开启了评论抓取，将启动多个消费者子线程并发抓取评论。
    """
    log = _make_keyword_log_callback(log_callback, keyword)
    output_path = None
    writer = None
    writer_lock = None
    comment_queue = None
    comment_threads: list[threading.Thread] = []
    metrics_threads: list[threading.Thread] = []
    metrics_queue = None
    metrics_consumers_ready = None
    comment_consumers_ready = None
    written_counter = {"count": 0, "lock": threading.Lock()}
    # 串行多关键词模式下，外层传入已建好的 search_page 复用，避免每个关键词新开标签页
    owns_playwright = shared_search_page is None
    search_page = shared_search_page
    browser = None
    try:
        # 检查是否已请求停止或暂停
        if should_stop(stop_event):
            log("任务已停止。")
            return None
        if wait_if_paused(pause_event, stop_event):
            log("任务已停止。")
            return None

        log(f"[{keyword_index}/{total_keywords}] 搜索关键词：{keyword}")
        # 构建输出文件路径
        output_path = build_output_path(
            "tiktok",
            f"tiktok_keyword_{safe_filename_part(keyword)}_{run_stamp}.xlsx",
            channel="keyword",
        )
        log(f"  输出文件：{output_path}")
        if start_dt is not None:
            log(f"  日期范围：{start_dt.strftime('%Y-%m-%d')} 至 {end_dt.strftime('%Y-%m-%d')}")

        # 复用模式下不新建 playwright/标签页，仅用传入的 search_page；
        # 新建模式下 with sync_playwright() 管理生命周期。nullcontext 保持主体缩进不变。
        with (sync_playwright() if owns_playwright else nullcontext()) as p:
            if owns_playwright:
                # 连接现有的浏览器
                browser, context = connect_existing_chromium(p, cdp_port_or_url)
                # 仅创建搜索页；详情页由 metrics 消费者线程各自独立创建，实现并发
                search_page = context.new_page()
            try:
                # 写锁始终创建：metrics 消费者并发写入视频信息行需要加锁
                writer_lock = threading.Lock()
                # 详情页任务队列与消费者就绪事件（始终启用，实现详情页多 tab 并发）
                metrics_queue = queue.Queue(maxsize=max_queue_size)
                metrics_consumers_ready = threading.Event()

                # 如果需要采集评论，初始化多表 XLSX 写入器、评论队列及评论消费者
                if get_comments_bool:
                    comment_fields = ["序号", "视频链接", "评论的点赞量", "评论内容", "发布时间"]
                    writer = MultiSheetXlsxWriter(output_path, {"视频信息": CSV_FIELDS, "评论信息": comment_fields}, autosave_every=10)
                    comment_queue = queue.Queue(maxsize=max_queue_size)
                    comment_consumers_ready = threading.Event()
                    # 启动指定数量的评论抓取消费者线程
                    for _ in range(max_comment_tabs):
                        t = threading.Thread(
                            target=_tiktok_comment_consumer,
                            args=(
                                keyword,
                                comment_queue,
                                cdp_port_or_url,
                                writer,
                                writer_lock,
                                log_callback,
                                stop_event,
                                pause_event,
                                comment_top_limit,
                                comment_consumers_ready,
                            ),
                            daemon=True,
                        )
                        t.start()
                        comment_threads.append(t)
                else:
                    # 仅采集视频信息，使用普通的 XLSX 单表写入器
                    writer = XlsxRowWriter(output_path, CSV_FIELDS, autosave_every=10)

                # 启动详情页指标消费者线程（并发提取候选视频详情）
                for _ in range(max(1, max_metrics_tabs)):
                    t = threading.Thread(
                        target=_tiktok_metrics_consumer,
                        args=(
                            keyword,
                            metrics_queue,
                            cdp_port_or_url,
                            writer,
                            writer_lock,
                            log_callback,
                            stop_event,
                            pause_event,
                            start_dt,
                            end_dt,
                            get_comments_bool,
                            max_comments,
                            comment_queue,
                            metrics_consumers_ready,
                            comment_consumers_ready,
                            written_counter,
                            cooldown_min,
                            cooldown_max,
                        ),
                        daemon=True,
                    )
                    t.start()
                    metrics_threads.append(t)
                # 等待至少一个详情消费者就绪，再开始推送任务
                metrics_consumers_ready.wait(timeout=15)

                serial_number = 1
                # 打开 TikTok 搜索页面（含错误态自动重试）
                open_search_page(search_page, keyword, stop_event=stop_event, log_callback=log, pause_event=pause_event)
                # 计算动态搜索滚动次数上限，防止无限滚动
                scroll_limit = dynamic_search_scroll_limit(max_videos, config_max_search_scrolls)
                seen_links: set[str] = set()
                scanned_count = 0
                no_new_visible_rounds = 0
                log("  开始边滚动边提取详情并按日期过滤")

                pushed_count = 0
                for scroll_index in range(scroll_limit):
                    if should_stop(stop_event):
                        log("  已请求停止，结束当前关键词。")
                        break
                    if wait_if_paused(pause_event, stop_event):
                        break
                    # 收集当前视口中未见过的视频元素
                    new_items = collect_visible_video_items(search_page, seen_links)
                    if not new_items:
                        no_new_visible_rounds += 1
                        # 兜底：连续无新内容时检测是否因 TikTok 错误态导致，若是则尝试恢复
                        if no_new_visible_rounds >= 2:
                            error_text = _detect_tiktok_search_error(search_page)
                            if error_text is not None:
                                log(f"  滚动过程中检测到搜索页错误态「{error_text}」，尝试重试恢复...")
                                open_search_page(search_page, keyword, stop_event=stop_event, log_callback=log, pause_event=pause_event)
                                no_new_visible_rounds = 0
                    else:
                        no_new_visible_rounds = 0

                    for video_item in new_items:
                        if should_stop(stop_event):
                            break
                        if wait_if_paused(pause_event, stop_event):
                            break
                        # 推送数达到上限即停止（写入由消费者异步完成，边界用推送数控制）
                        if pushed_count >= max_videos:
                            break
                        if scanned_count >= max_candidates:
                            break
                        scanned_count += 1
                        try:
                            video_url = video_item["视频链接"]
                            with written_counter["lock"]:
                                current_written = written_counter["count"]
                            log(f"  [候选{scanned_count}/已写{current_written}] {video_url}")
                            # 推入详情页任务队列，由 metrics 消费者并发提取指标。
                            # 队列满时阻塞背压，自然限速防风控。
                            try:
                                metrics_queue.put(
                                    (
                                        serial_number,
                                        video_url,
                                        video_item.get("播放量", ""),
                                        video_item.get("博主主页链接", ""),
                                    ),
                                    block=True,
                                    timeout=15,
                                )
                            except Exception:
                                log("    详情队列已满或消费线程异常，跳过本条。")
                                continue
                            serial_number += 1
                            pushed_count += 1
                        except Exception as exc:
                            log(f"    跳过：{exc}")
                        # 每推送 20 个候选视频进行一次随机冷却，防止风控
                        if scanned_count and scanned_count % 20 == 0:
                            if random_cooldown(log, stop_event, cooldown_min, cooldown_max, pause_event=pause_event):
                                break

                    # 各种边界条件判断，是否跳出搜索滚动循环
                    if pushed_count >= max_videos:
                        break
                    if scanned_count >= max_candidates:
                        log(f"  已检查 {scanned_count} 个候选，达到候选检查上限，停止当前关键词。")
                        break
                    if no_new_visible_rounds >= no_new_scroll_limit and scroll_index >= 20:
                        log("  连续多轮没有新视频链接，停止当前关键词。")
                        break
                    if scroll_index and scroll_index % 10 == 0:
                        with written_counter["lock"]:
                            current_written = written_counter["count"]
                        log(f"  已滚动 {scroll_index}/{scroll_limit} 轮，已扫描 {scanned_count} 个候选，写入 {current_written} 条")

                    # 触发惰性滚动加载
                    trigger_search_lazy_load(search_page)
                    interruptible_sleep(search_scroll_pause, stop_event, pause_event=pause_event)

                # 向所有详情消费者线程发送 None 哨兵值，等待它们处理完队列中剩余任务
                if metrics_queue is not None:
                    for _ in metrics_threads:
                        try:
                            metrics_queue.put(None, block=True, timeout=15)
                        except Exception:
                            pass
                    for t in metrics_threads:
                        t.join(timeout=180)
                with written_counter["lock"]:
                    written_count = written_counter["count"]
                log(f"  写入 {written_count} 条日期范围内的视频")

                # 向所有评论消费者线程发送 None 哨兵值作为结束标记
                if comment_threads and comment_queue is not None:
                    for _ in comment_threads:
                        comment_queue.put(None)
                    # 等待所有消费者子线程结束回收，超时时间 120 秒
                    for t in comment_threads:
                        t.join(timeout=120)

                # 保存并保存 Excel 文件
                writer.save()
                return output_path
            finally:
                # 仅关闭本函数自建的 search_page/browser；复用传入的 search_page 由外层管理
                if owns_playwright:
                    if search_page is not None and not search_page.is_closed():
                        try:
                            search_page.close()
                        except Exception:
                            pass
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass

    except Exception as exc:
        log(f"运行失败：{exc}")
        if writer is not None:
            try:
                writer.save()
            except Exception:
                pass
        return None
    finally:
        # 双重保险：确保消费者线程安全退出，关闭 Playwright 页面
        # 双重保险：确保详情消费者线程安全退出
        if metrics_threads and metrics_queue is not None:
            try:
                for _ in metrics_threads:
                    metrics_queue.put(None)
            except Exception:
                pass
            for t in metrics_threads:
                if t.is_alive():
                    t.join(timeout=10)
        # 双重保险：确保评论消费者线程安全退出
        if comment_threads and comment_queue is not None:
            try:
                for _ in comment_threads:
                    comment_queue.put(None)
            except Exception:
                pass
            for t in comment_threads:
                if t.is_alive():
                    t.join(timeout=10)


def run_tiktok_spider(
    keywords_list,
    max_videos,
    max_candidates,
    limit_time_str,
    start_date,
    end_date,
    get_comments_str,
    max_comments,
    cdp_port_or_url,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    """
    TikTok 关键词爬虫主入口函数。
    解析全局配置参数，视关键词数量与 max_parallel_tabs 决定是采用单线程顺序执行，还是采用 ThreadPoolExecutor 进行多关键词并发调度抓取。
    """
    ensure_playwright_available()
    if config is None:
        config = {}
    search_scroll_pause = float(config.get("scroll_interval", SEARCH_SCROLL_PAUSE))
    config_max_search_scrolls = int(config.get("max_search_scrolls", MAX_SEARCH_SCROLLS))
    no_new_scroll_limit = int(config.get("no_new_scroll_limit", 12))
    comment_top_limit = int(config.get("comment_top_limit", 100))
    max_parallel_tabs = max(1, min(3, int(config.get("max_parallel_tabs", 1))))
    max_comment_tabs = max(1, min(3, int(config.get("max_comment_tabs", 1))))
    max_metrics_tabs = max(1, min(3, int(config.get("max_metrics_tabs", 2))))
    max_queue_size = max(10, min(10000, int(config.get("max_queue_size", 5000))))
    cooldown_min_val = float(config.get("cooldown_min", 3.0))
    cooldown_max_val = float(config.get("cooldown_max", 8.0))

    output_paths: list[str] = []
    try:
        limit_time_bool = limit_time_str == "是"
        get_comments_bool = get_comments_str == "是"
        start_dt, end_dt = None, None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)

        run_stamp = time.strftime("%Y%m%d_%H%M%S")

        # 启动多线程前，预先启动 Chrome，建立 CDP 调试端口连接
        ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)

        # --- 串行分支（仅一个关键词或并发页面数限制为 1） ---
        if max_parallel_tabs <= 1 or len(keywords_list) <= 1:
            # 串行多关键词复用同一个搜索标签页，避免每个关键词新开窗口触发风控
            shared_pw = None
            shared_browser = None
            shared_context = None
            shared_search_page = None
            try:
                shared_pw = sync_playwright().start()
                shared_browser, shared_context = connect_existing_chromium(shared_pw, cdp_port_or_url)
                shared_search_page = shared_context.new_page()
                for idx, keyword in enumerate(keywords_list, 1):
                    if should_stop(stop_event):
                        log_line(log_callback, "任务已停止。")
                        break
                    if wait_if_paused(pause_event, stop_event):
                        break
                    # 容错：复用前检测标签页存活，失效则重建，避免影响后续关键词
                    if _is_page_closed(shared_search_page):
                        log_line(log_callback, "搜索标签页已失效，重新创建...")
                        try:
                            shared_search_page = shared_context.new_page()
                        except Exception as exc:
                            log_line(log_callback, f"重建搜索标签页失败：{exc}")
                            break
                    path = _scrape_single_tiktok_keyword(
                        keyword,
                        idx,
                        len(keywords_list),
                        max_videos,
                        max_candidates,
                        start_dt,
                        end_dt,
                        get_comments_bool,
                        max_comments,
                        max_comment_tabs,
                        max_metrics_tabs,
                        max_queue_size,
                        cdp_port_or_url,
                        log_callback,
                        stop_event,
                        pause_event,
                        search_scroll_pause,
                        config_max_search_scrolls,
                        no_new_scroll_limit,
                        comment_top_limit,
                        run_stamp,
                        cooldown_min_val,
                        cooldown_max_val,
                        shared_search_page=shared_search_page,
                    )
                    if path:
                        output_paths.append(path)
            finally:
                if shared_search_page is not None and not shared_search_page.is_closed():
                    try:
                        shared_search_page.close()
                    except Exception:
                        pass
                if shared_browser is not None:
                    try:
                        shared_browser.close()
                    except Exception:
                        pass
                if shared_pw is not None:
                    try:
                        shared_pw.stop()
                    except Exception:
                        pass

            log_line(log_callback, "完成，已按关键词分别保存：")
            for p in output_paths:
                log_line(log_callback, f"  {p}")
            finish_callback(output_paths if output_paths else None)
            return

        # --- 多线程并行分支 ---
        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            future_to_keyword = {}
            for idx, keyword in enumerate(keywords_list, 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                future = executor.submit(
                    _scrape_single_tiktok_keyword,
                    keyword,
                    idx,
                    len(keywords_list),
                    max_videos,
                    max_candidates,
                    start_dt,
                    end_dt,
                    get_comments_bool,
                    max_comments,
                    max_comment_tabs,
                    max_metrics_tabs,
                    max_queue_size,
                    cdp_port_or_url,
                    log_callback,
                    stop_event,
                    pause_event,
                    search_scroll_pause,
                    config_max_search_scrolls,
                    no_new_scroll_limit,
                    comment_top_limit,
                    run_stamp,
                    cooldown_min_val,
                    cooldown_max_val,
                )
                future_to_keyword[future] = keyword

            for future in as_completed(future_to_keyword):
                keyword = future_to_keyword[future]
                try:
                    path = future.result()
                    if path:
                        output_paths.append(path)
                except Exception as exc:
                    log_error(log_callback, f"[{keyword}] 线程异常: {exc}")

        log_line(log_callback, f"全部关键词处理完毕。{len(output_paths)}/{len(keywords_list)} 个成功。")
        for p in output_paths:
            log_line(log_callback, f"  {p}")
        finish_callback(output_paths if output_paths else None)

    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
        finish_callback(None)
