# -*- coding: utf-8 -*-
"""Steam API based game research tool."""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from src.core import (
    build_output_path,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    should_stop,
    wait_if_paused,
)
from src.core.app_state import get_app_state_root
from src.core.task_checkpoint import open_checkpointed_multi_sheet_writer, open_task_checkpoint


STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STORE_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STORE_REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
CURRENT_PLAYERS_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
GLOBAL_ACHIEVEMENTS_URL = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v0002/"
PUBLIC_APP_LIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
ISTORE_APP_LIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"

DEFAULT_LANGUAGE = "english"
DEFAULT_COUNTRY = "US"

APP_FIELDS = [
    "来源类型",
    "搜索词",
    "AppID",
    "游戏名",
    "状态",
    "类型",
    "是否免费",
    "价格",
    "原价",
    "折扣",
    "发行日期",
    "是否即将发行",
    "开发商",
    "发行商",
    "类型标签",
    "分类标签",
    "支持平台",
    "支持语言",
    "年龄限制",
    "控制器支持",
    "PC最低配置",
    "PC推荐配置",
    "Mac最低配置",
    "Mac推荐配置",
    "Linux最低配置",
    "Linux推荐配置",
    "DLC AppID列表",
    "Demo列表",
    "所属完整游戏",
    "安装包ID列表",
    "套餐/版本列表",
    "内容描述",
    "分级信息",
    "客服网站",
    "客服邮箱",
    "客服电话",
    "第三方账号提示",
    "DRM提示",
    "法律声明",
    "短简介",
    "详细简介",
    "总评描述",
    "总评分数",
    "总评数",
    "好评数",
    "差评数",
    "好评率",
    "推荐数",
    "Metacritic",
    "当前在线",
    "成就数量",
    "成就样本列表",
    "新闻样本数",
    "玩家评论样本数",
    "商店链接",
    "官网",
    "头图",
    "胶囊图",
    "胶囊图V5",
    "背景图",
    "原始背景图",
    "截图列表",
    "视频列表",
    "查询时间",
]

REVIEW_FIELDS = [
    "来源类型",
    "搜索词",
    "AppID",
    "游戏名",
    "评论ID",
    "SteamID",
    "是否推荐",
    "语言",
    "Steam购买",
    "免费获得",
    "抢先体验期间",
    "主要在SteamDeck",
    "拥有游戏数",
    "作者评论数",
    "总游玩小时",
    "近两周游玩小时",
    "评价时游玩小时",
    "最后游玩时间",
    "有价值票数",
    "欢乐票数",
    "加权评分",
    "开发者回复",
    "开发者回复时间",
    "发布时间",
    "最后更新时间",
    "评论链接",
    "评论内容",
    "查询时间",
]

NEWS_FIELDS = [
    "来源类型",
    "搜索词",
    "AppID",
    "游戏名",
    "新闻ID",
    "标题",
    "作者",
    "发布时间",
    "来源名称",
    "来源标签",
    "Feed名称",
    "Feed类型",
    "外部链接",
    "链接",
    "内容摘要",
    "查询时间",
]


@dataclass(frozen=True)
class SteamWorkItem:
    appid: int
    source_type: str
    source: str = ""
    seed_name: str = ""

    @property
    def checkpoint_key(self) -> str:
        label = self.source if self.source_type == "关键词" else "direct"
        return f"{label}|{self.appid}".lower()


@dataclass
class SteamAppBundle:
    app_row: dict[str, Any]
    review_rows: list[dict[str, Any]]
    news_rows: list[dict[str, Any]]
    meta: dict[str, Any]


def parse_app_ids(value: str | list[str] | tuple[str, ...]) -> list[int]:
    """Extract Steam appids from bare numbers or Steam store URLs."""
    if isinstance(value, str):
        lines = value.splitlines()
    else:
        lines = list(value or [])
    appids: list[int] = []
    seen: set[int] = set()
    for raw in lines:
        text = str(raw or "").strip()
        if not text:
            continue
        match = re.search(r"(?:/app/|^)(\d{2,12})(?:\D|$)", text)
        if not match:
            continue
        appid = int(match.group(1))
        if appid not in seen:
            seen.add(appid)
            appids.append(appid)
    return appids


def normalize_keywords(value: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(value, str):
        lines = value.splitlines()
    else:
        lines = list(value or [])
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        text = str(raw or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", text).lower()
        if key not in seen:
            seen.add(key)
            keywords.append(text)
    return keywords


def parse_date_range(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期")
    return start_dt, end_dt


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _cache_dir() -> Path:
    path = get_app_state_root() / "steam_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _read_cache(path: Path, max_age_hours: float) -> Any | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        updated_ts = float(data.get("updated_ts", 0) or 0)
        if updated_ts and time.time() - updated_ts <= max(0, max_age_hours) * 3600:
            return data.get("payload")
    except Exception:
        return None
    return None


def _write_cache(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"updated_ts": time.time(), "payload": payload}, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        tmp.replace(path)
    except OSError:
        path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


def _clean_text(value: Any, *, max_length: int | None = None) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = text.strip()
    if max_length and len(text) > max_length:
        return text[:max_length].rstrip()
    return text


def _limit_text(value: Any, max_length: int = 12000) -> str:
    text = str(value or "").strip()
    if len(text) > max_length:
        return text[:max_length].rstrip()
    return text


def _join(values: list[Any]) -> str:
    cleaned = [str(value).strip() for value in values if str(value or "").strip()]
    return _limit_text("\n".join(cleaned))


def _list_desc(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    return " / ".join(str(item.get("description") or item.get("name") or "").strip() for item in values if isinstance(item, dict) and (item.get("description") or item.get("name")))


def _format_timestamp(value: Any) -> str:
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _minutes_to_hours(value: Any) -> str:
    try:
        return str(round(float(value or 0) / 60, 1))
    except (TypeError, ValueError):
        return ""


def _yes_no(value: Any) -> str:
    return "是" if bool(value) else "否"


def _requirements_text(value: Any, key: str) -> str:
    if isinstance(value, dict):
        return _clean_text(value.get(key), max_length=3000)
    if key == "minimum":
        return _clean_text(value, max_length=3000)
    return ""


def _appid_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    appid = str(value.get("appid") or "").strip()
    name = str(value.get("name") or "").strip()
    if appid and name:
        return f"{appid} {name}"
    return appid or name


def _demos_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    rows = []
    for item in value:
        if isinstance(item, dict):
            appid = str(item.get("appid") or "").strip()
            description = _clean_text(item.get("description"), max_length=120)
            rows.append(f"{appid} {description}".strip())
    return _join(rows)


def _package_groups_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    rows: list[str] = []
    for group in value:
        if not isinstance(group, dict):
            continue
        title = _clean_text(group.get("title") or group.get("name"), max_length=120)
        description = _clean_text(group.get("description"), max_length=240)
        subs = []
        for sub in group.get("subs", []) or []:
            if not isinstance(sub, dict):
                continue
            packageid = str(sub.get("packageid") or "").strip()
            option_text = _clean_text(sub.get("option_text"), max_length=160)
            subs.append(f"{packageid} {option_text}".strip())
        rows.append(" | ".join(part for part in [title, description, "; ".join(subs)] if part))
    return _join(rows)


def _content_descriptors_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    ids = value.get("ids", [])
    ids_text = ", ".join(str(item) for item in ids) if isinstance(ids, list) else str(ids or "")
    notes = _clean_text(value.get("notes"), max_length=1000)
    return " | ".join(part for part in [f"ids: {ids_text}" if ids_text else "", notes] if part)


def _ratings_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    rows: list[str] = []
    for rating_name, rating_data in value.items():
        if isinstance(rating_data, dict):
            summary = ", ".join(f"{key}={_clean_text(val, max_length=100)}" for key, val in rating_data.items())
            rows.append(f"{rating_name}: {summary}")
        else:
            rows.append(f"{rating_name}: {_clean_text(rating_data, max_length=200)}")
    return _join(rows)


def _achievement_highlights_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    highlighted = value.get("highlighted", [])
    if not isinstance(highlighted, list):
        return ""
    rows = []
    for item in highlighted:
        if isinstance(item, dict):
            name = _clean_text(item.get("name"), max_length=160)
            path = str(item.get("path") or "").strip()
            rows.append(" | ".join(part for part in [name, path] if part))
    return _join(rows)


def _request_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    request_delay: float = 0.0,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> Any:
    last_exc: Exception | None = None
    headers = {
        "User-Agent": "Mozilla/5.0 social-platform-scraper Steam API collector",
        "Accept": "application/json,text/plain,*/*",
    }
    for attempt in range(max(1, int(max_retries or 1))):
        if wait_if_paused(pause_event, stop_event) or should_stop(stop_event):
            raise InterruptedError("任务已停止")
        try:
            response = session.get(url, params=params, headers=headers, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504} and attempt < max_retries - 1:
                wait_seconds = min(30.0, 2.0 ** attempt + 1.0)
                log_warn(log_callback, f"Steam API HTTP {response.status_code}，{wait_seconds:.1f} 秒后重试。")
                interruptible_sleep(wait_seconds, stop_event)
                continue
            response.raise_for_status()
            payload = response.json()
            if request_delay > 0:
                interruptible_sleep(request_delay, stop_event)
            return payload
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait_seconds = min(30.0, 2.0 ** attempt + 1.0)
                log_warn(log_callback, f"Steam API 请求失败，{wait_seconds:.1f} 秒后重试：{exc}")
                interruptible_sleep(wait_seconds, stop_event)
                continue
            break
    if last_exc:
        raise last_exc
    raise RuntimeError("Steam API 请求失败")


def _config_bool(config: dict[str, Any], key: str, default: str = "否") -> bool:
    return str(config.get(key, default)).strip() == "是"


def discover_apps_for_keyword(
    keyword: str,
    *,
    api_key: str = "",
    language: str = DEFAULT_LANGUAGE,
    country: str = DEFAULT_COUNTRY,
    max_apps: int = 100,
    config: dict[str, Any] | None = None,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> list[SteamWorkItem]:
    config = config or {}
    max_apps = max(1, int(max_apps or 1))
    timeout = float(config.get("request_timeout", 30) or 30)
    request_delay = float(config.get("request_delay", 0.2) or 0.0)
    cache_hours = float(config.get("cache_ttl_hours", 168) or 0)
    search_mode = str(config.get("keyword_search_mode", "商店搜索接口（推荐）") or "商店搜索接口（推荐）")

    cache_payload = {
        "keyword": keyword,
        "language": language,
        "country": country,
        "max_apps": max_apps,
        "search_mode": search_mode,
        "api_key_present": bool(api_key),
    }
    cache_path = _cache_dir() / f"keyword_{_cache_key(cache_payload)}.json"
    cached = _read_cache(cache_path, cache_hours)
    if isinstance(cached, list):
        log_line(log_callback, f"关键词缓存命中：{keyword}，{len(cached)} 个 AppID。")
        return [
            SteamWorkItem(appid=int(item["appid"]), source_type="关键词", source=keyword, seed_name=str(item.get("name") or ""))
            for item in cached
            if str(item.get("appid") or "").isdigit()
        ]

    session = requests.Session()
    discovered: list[dict[str, Any]] = []
    if search_mode != "AppList 本地匹配":
        discovered = _search_store_keyword(
            session,
            keyword,
            max_apps=max_apps,
            language=language,
            country=country,
            timeout=timeout,
            request_delay=request_delay,
            log_callback=log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
        )
    if len(discovered) < max_apps and search_mode in {"AppList 本地匹配", "商店搜索后补 AppList"}:
        app_list_matches = _match_app_list_keyword(
            keyword,
            api_key=api_key,
            max_apps=max_apps,
            timeout=timeout,
            cache_hours=cache_hours,
            request_delay=request_delay,
            log_callback=log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
        )
        seen = {int(item["appid"]) for item in discovered if str(item.get("appid") or "").isdigit()}
        for item in app_list_matches:
            appid = int(item["appid"])
            if appid not in seen:
                seen.add(appid)
                discovered.append(item)
            if len(discovered) >= max_apps:
                break

    payload = [{"appid": int(item["appid"]), "name": str(item.get("name") or "")} for item in discovered[:max_apps]]
    _write_cache(cache_path, payload)
    log_line(log_callback, f"关键词发现：{keyword}，找到 {len(payload)} 个候选 App。")
    return [SteamWorkItem(appid=item["appid"], source_type="关键词", source=keyword, seed_name=item.get("name", "")) for item in payload]


def _search_store_keyword(
    session: requests.Session,
    keyword: str,
    *,
    max_apps: int,
    language: str,
    country: str,
    timeout: float,
    request_delay: float,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[int] = set()
    start = 0
    while len(results) < max_apps:
        if wait_if_paused(pause_event, stop_event) or should_stop(stop_event):
            break
        count = min(100, max_apps - len(results))
        payload = _request_json(
            session,
            STORE_SEARCH_URL,
            params={"term": keyword, "l": language, "cc": country, "start": start, "count": count},
            timeout=timeout,
            request_delay=request_delay,
            log_callback=log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            appid = item.get("id") or item.get("appid")
            try:
                appid = int(appid)
            except (TypeError, ValueError):
                continue
            if appid in seen:
                continue
            seen.add(appid)
            results.append({"appid": appid, "name": item.get("name", "")})
            if len(results) >= max_apps:
                break
        total = int(payload.get("total", 0) or 0) if isinstance(payload, dict) else 0
        start += len(items)
        if total and start >= total:
            break
        if len(items) < count:
            break
    return results


def _load_app_list(
    *,
    api_key: str,
    timeout: float,
    cache_hours: float,
    request_delay: float,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> list[dict[str, Any]]:
    cache_path = _cache_dir() / ("app_list_istore.json" if api_key else "app_list_public.json")
    cached = _read_cache(cache_path, cache_hours)
    if isinstance(cached, list):
        return cached

    session = requests.Session()
    apps: list[dict[str, Any]] = []
    if api_key:
        try:
            last_appid = 0
            while True:
                params: dict[str, Any] = {
                    "key": api_key,
                    "include_games": "true",
                    "include_dlc": "false",
                    "include_software": "false",
                    "max_results": 50000,
                }
                if last_appid:
                    params["last_appid"] = last_appid
                payload = _request_json(
                    session,
                    ISTORE_APP_LIST_URL,
                    params=params,
                    timeout=timeout,
                    request_delay=request_delay,
                    log_callback=log_callback,
                    stop_event=stop_event,
                    pause_event=pause_event,
                )
                page_apps = ((payload or {}).get("response") or {}).get("apps", []) if isinstance(payload, dict) else []
                if not page_apps:
                    break
                apps.extend({"appid": int(item.get("appid")), "name": item.get("name", "")} for item in page_apps if str(item.get("appid") or "").isdigit())
                last_appid = int(page_apps[-1].get("appid") or 0)
                if len(page_apps) < 50000:
                    break
        except Exception as exc:
            log_warn(log_callback, f"IStoreService AppList 失败，将使用公开 AppList 兜底：{exc}")
            apps = []

    if not apps:
        payload = _request_json(
            session,
            PUBLIC_APP_LIST_URL,
            timeout=timeout,
            request_delay=request_delay,
            log_callback=log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
        )
        raw_apps = ((payload or {}).get("applist") or {}).get("apps", []) if isinstance(payload, dict) else []
        apps = [{"appid": int(item.get("appid")), "name": item.get("name", "")} for item in raw_apps if str(item.get("appid") or "").isdigit()]

    _write_cache(cache_path, apps)
    return apps


def _match_app_list_keyword(
    keyword: str,
    *,
    api_key: str,
    max_apps: int,
    timeout: float,
    cache_hours: float,
    request_delay: float,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> list[dict[str, Any]]:
    apps = _load_app_list(
        api_key=api_key,
        timeout=timeout,
        cache_hours=cache_hours,
        request_delay=request_delay,
        log_callback=log_callback,
        stop_event=stop_event,
        pause_event=pause_event,
    )
    keyword_norm = re.sub(r"\s+", " ", keyword.strip().lower())
    matches: list[dict[str, Any]] = []
    for item in apps:
        name = str(item.get("name") or "")
        if keyword_norm and keyword_norm in name.lower():
            matches.append(item)
            if len(matches) >= max_apps:
                break
    return matches


def collect_steam_app_bundle(
    item: SteamWorkItem,
    *,
    api_key: str = "",
    language: str = DEFAULT_LANGUAGE,
    country: str = DEFAULT_COUNTRY,
    collect_reviews: bool = False,
    max_reviews: int = 0,
    collect_news: bool = False,
    max_news: int = 0,
    config: dict[str, Any] | None = None,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> SteamAppBundle:
    config = config or {}
    timeout = float(config.get("request_timeout", 30) or 30)
    request_delay = float(config.get("request_delay", 0.2) or 0.0)
    include_non_games = _config_bool(config, "include_non_games", "否")
    collect_current_players = _config_bool(config, "collect_current_players", "是")
    collect_achievements = _config_bool(config, "collect_achievements", "否")
    review_language = str(config.get("review_language", "all") or "all").strip() or "all"
    reviews_filter = str(config.get("reviews_filter", "all") or "all").strip() or "all"

    session = requests.Session()
    query_time = _now_text()
    details = fetch_app_details(
        session,
        item.appid,
        language=language,
        country=country,
        timeout=timeout,
        request_delay=request_delay,
        log_callback=log_callback,
        stop_event=stop_event,
        pause_event=pause_event,
    )
    data = details.get("data") if details.get("success") else None
    if not isinstance(data, dict):
        row = _base_app_row(item, query_time)
        row.update({"游戏名": item.seed_name, "状态": "not_found", "商店链接": _store_url(item.appid)})
        return SteamAppBundle(row, [], [], {"app_count": 1, "review_count": 0, "news_count": 0})

    app_type = str(data.get("type") or "")
    if app_type and app_type != "game" and not include_non_games:
        row = _build_app_row(item, data, query_time, status="skipped_non_game")
        return SteamAppBundle(row, [], [], {"app_count": 1, "review_count": 0, "news_count": 0, "skipped_non_game": 1})

    review_summary: dict[str, Any] = {}
    review_rows: list[dict[str, Any]] = []
    if collect_reviews:
        review_summary, reviews = fetch_reviews(
            session,
            item.appid,
            max_reviews=max(0, int(max_reviews or 0)),
            language=review_language,
            filter_mode=reviews_filter,
            timeout=timeout,
            request_delay=request_delay,
            log_callback=log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
        )
        review_rows = [_build_review_row(item, data, review, query_time) for review in reviews]

    news_rows: list[dict[str, Any]] = []
    if collect_news and max_news > 0:
        news_items = fetch_news(
            session,
            item.appid,
            max_news=max(0, int(max_news or 0)),
            timeout=timeout,
            request_delay=request_delay,
            log_callback=log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
        )
        news_rows = [_build_news_row(item, data, news, query_time) for news in news_items]

    current_players = ""
    if collect_current_players:
        try:
            current_players = fetch_current_players(
                session,
                item.appid,
                timeout=timeout,
                request_delay=request_delay,
                log_callback=log_callback,
                stop_event=stop_event,
                pause_event=pause_event,
            )
        except Exception as exc:
            log_warn(log_callback, f"当前在线人数获取失败 AppID={item.appid}: {exc}")

    achievement_count = ""
    achievement_sample = ""
    if collect_achievements:
        try:
            achievement_count, achievement_sample = fetch_achievement_summary(
                session,
                item.appid,
                timeout=timeout,
                request_delay=request_delay,
                log_callback=log_callback,
                stop_event=stop_event,
                pause_event=pause_event,
            )
        except Exception as exc:
            log_warn(log_callback, f"成就摘要获取失败 AppID={item.appid}: {exc}")

    app_row = _build_app_row(
        item,
        data,
        query_time,
        status="ok",
        review_summary=review_summary,
        current_players=current_players,
        achievement_count=achievement_count,
        achievement_sample=achievement_sample,
        news_count=len(news_rows),
        review_count=len(review_rows),
    )
    return SteamAppBundle(
        app_row,
        review_rows,
        news_rows,
        {"app_count": 1, "review_count": len(review_rows), "news_count": len(news_rows)},
    )


def fetch_app_details(
    session: requests.Session,
    appid: int,
    *,
    language: str,
    country: str,
    timeout: float,
    request_delay: float,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> dict[str, Any]:
    payload = _request_json(
        session,
        STORE_APPDETAILS_URL,
        params={"appids": appid, "l": language, "cc": country},
        timeout=timeout,
        request_delay=request_delay,
        log_callback=log_callback,
        stop_event=stop_event,
        pause_event=pause_event,
    )
    item = payload.get(str(appid), {}) if isinstance(payload, dict) else {}
    return item if isinstance(item, dict) else {}


def fetch_reviews(
    session: requests.Session,
    appid: int,
    *,
    max_reviews: int,
    language: str,
    filter_mode: str,
    timeout: float,
    request_delay: float,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    reviews: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    cursor = "*"
    first = True
    while first or len(reviews) < max_reviews:
        first = False
        if wait_if_paused(pause_event, stop_event) or should_stop(stop_event):
            break
        remaining = max(0, max_reviews - len(reviews))
        page_size = min(100, remaining if remaining > 0 else 1)
        payload = _request_json(
            session,
            STORE_REVIEWS_URL.format(appid=appid),
            params={
                "json": 1,
                "filter": filter_mode,
                "language": language,
                "review_type": "all",
                "purchase_type": "all",
                "num_per_page": page_size,
                "cursor": cursor,
            },
            timeout=timeout,
            request_delay=request_delay,
            log_callback=log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
        )
        if isinstance(payload, dict) and isinstance(payload.get("query_summary"), dict):
            summary = payload.get("query_summary", {})
        page_reviews = payload.get("reviews", []) if isinstance(payload, dict) else []
        if max_reviews <= 0:
            break
        if not page_reviews:
            break
        for review in page_reviews:
            if isinstance(review, dict):
                reviews.append(review)
                if len(reviews) >= max_reviews:
                    break
        next_cursor = str(payload.get("cursor") or "") if isinstance(payload, dict) else ""
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return summary, reviews


def fetch_news(
    session: requests.Session,
    appid: int,
    *,
    max_news: int,
    timeout: float,
    request_delay: float,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> list[dict[str, Any]]:
    payload = _request_json(
        session,
        STEAM_NEWS_URL,
        params={"appid": appid, "count": max_news, "maxlength": 1200, "format": "json"},
        timeout=timeout,
        request_delay=request_delay,
        log_callback=log_callback,
        stop_event=stop_event,
        pause_event=pause_event,
    )
    items = ((payload or {}).get("appnews") or {}).get("newsitems", []) if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)][:max_news]


def fetch_current_players(
    session: requests.Session,
    appid: int,
    *,
    timeout: float,
    request_delay: float,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> str:
    payload = _request_json(
        session,
        CURRENT_PLAYERS_URL,
        params={"appid": appid},
        timeout=timeout,
        request_delay=request_delay,
        log_callback=log_callback,
        stop_event=stop_event,
        pause_event=pause_event,
    )
    response = payload.get("response", {}) if isinstance(payload, dict) else {}
    if int(response.get("result", 0) or 0) != 1:
        return ""
    return str(response.get("player_count", ""))


def fetch_achievement_summary(
    session: requests.Session,
    appid: int,
    *,
    timeout: float,
    request_delay: float,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> tuple[str, str]:
    payload = _request_json(
        session,
        GLOBAL_ACHIEVEMENTS_URL,
        params={"gameid": appid, "format": "json"},
        timeout=timeout,
        request_delay=request_delay,
        log_callback=log_callback,
        stop_event=stop_event,
        pause_event=pause_event,
    )
    achievements = ((payload or {}).get("achievementpercentages") or {}).get("achievements", []) if isinstance(payload, dict) else []
    if not isinstance(achievements, list):
        return "", ""
    rows = []
    for item in achievements[:50]:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"), max_length=160)
        percent = item.get("percent", "")
        rows.append(f"{name}: {percent}%".strip())
    return str(len(achievements)), _join(rows)


def fetch_achievement_count(
    session: requests.Session,
    appid: int,
    *,
    timeout: float,
    request_delay: float,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> str:
    count, _ = fetch_achievement_summary(
        session,
        appid,
        timeout=timeout,
        request_delay=request_delay,
        log_callback=log_callback,
        stop_event=stop_event,
        pause_event=pause_event,
    )
    return count


def _base_app_row(item: SteamWorkItem, query_time: str) -> dict[str, Any]:
    return {
        "来源类型": item.source_type,
        "搜索词": item.source,
        "AppID": item.appid,
        "查询时间": query_time,
    }


def _build_app_row(
    item: SteamWorkItem,
    data: dict[str, Any],
    query_time: str,
    *,
    status: str,
    review_summary: dict[str, Any] | None = None,
    current_players: Any = "",
    achievement_count: Any = "",
    achievement_sample: Any = "",
    news_count: int = 0,
    review_count: int = 0,
) -> dict[str, Any]:
    review_summary = review_summary or {}
    price = data.get("price_overview") if isinstance(data.get("price_overview"), dict) else {}
    release = data.get("release_date") if isinstance(data.get("release_date"), dict) else {}
    platforms = data.get("platforms") if isinstance(data.get("platforms"), dict) else {}
    metacritic = data.get("metacritic") if isinstance(data.get("metacritic"), dict) else {}
    recommendations = data.get("recommendations") if isinstance(data.get("recommendations"), dict) else {}
    screenshots = data.get("screenshots") if isinstance(data.get("screenshots"), list) else []
    movies = data.get("movies") if isinstance(data.get("movies"), list) else []
    pc_requirements = data.get("pc_requirements")
    mac_requirements = data.get("mac_requirements")
    linux_requirements = data.get("linux_requirements")
    support_info = data.get("support_info") if isinstance(data.get("support_info"), dict) else {}
    achievements = data.get("achievements") if isinstance(data.get("achievements"), dict) else {}
    if not achievement_count:
        achievement_count = achievements.get("total", "")
    if not achievement_sample:
        achievement_sample = _achievement_highlights_text(achievements)
    total_reviews = _to_int(review_summary.get("total_reviews"))
    total_positive = _to_int(review_summary.get("total_positive"))
    total_negative = _to_int(review_summary.get("total_negative"))
    positive_rate = ""
    if total_reviews:
        positive_rate = f"{total_positive / total_reviews:.2%}"
    return {
        "来源类型": item.source_type,
        "搜索词": item.source,
        "AppID": item.appid,
        "游戏名": data.get("name") or item.seed_name,
        "状态": status,
        "类型": data.get("type", ""),
        "是否免费": _yes_no(data.get("is_free")),
        "价格": price.get("final_formatted") or ("免费" if data.get("is_free") else ""),
        "原价": price.get("initial_formatted", ""),
        "折扣": str(price.get("discount_percent", "")),
        "发行日期": release.get("date", ""),
        "是否即将发行": _yes_no(release.get("coming_soon")),
        "开发商": " / ".join(data.get("developers", []) or []),
        "发行商": " / ".join(data.get("publishers", []) or []),
        "类型标签": _list_desc(data.get("genres")),
        "分类标签": _list_desc(data.get("categories")),
        "支持平台": " / ".join(key for key, enabled in platforms.items() if enabled),
        "支持语言": _clean_text(data.get("supported_languages")),
        "年龄限制": str(data.get("required_age", "")),
        "控制器支持": data.get("controller_support", ""),
        "PC最低配置": _requirements_text(pc_requirements, "minimum"),
        "PC推荐配置": _requirements_text(pc_requirements, "recommended"),
        "Mac最低配置": _requirements_text(mac_requirements, "minimum"),
        "Mac推荐配置": _requirements_text(mac_requirements, "recommended"),
        "Linux最低配置": _requirements_text(linux_requirements, "minimum"),
        "Linux推荐配置": _requirements_text(linux_requirements, "recommended"),
        "DLC AppID列表": _join(data.get("dlc", []) or []),
        "Demo列表": _demos_text(data.get("demos")),
        "所属完整游戏": _appid_name(data.get("fullgame")),
        "安装包ID列表": _join(data.get("packages", []) or []),
        "套餐/版本列表": _package_groups_text(data.get("package_groups")),
        "内容描述": _content_descriptors_text(data.get("content_descriptors")),
        "分级信息": _ratings_text(data.get("ratings")),
        "客服网站": support_info.get("url", ""),
        "客服邮箱": support_info.get("email", ""),
        "客服电话": support_info.get("phone", ""),
        "第三方账号提示": _clean_text(data.get("ext_user_account_notice"), max_length=1000),
        "DRM提示": _clean_text(data.get("drm_notice"), max_length=1000),
        "法律声明": _clean_text(data.get("legal_notice"), max_length=1000),
        "短简介": _clean_text(data.get("short_description"), max_length=1000),
        "详细简介": _clean_text(data.get("detailed_description"), max_length=3000),
        "总评描述": review_summary.get("review_score_desc", ""),
        "总评分数": review_summary.get("review_score", ""),
        "总评数": total_reviews or "",
        "好评数": total_positive or "",
        "差评数": total_negative or "",
        "好评率": positive_rate,
        "推荐数": recommendations.get("total", ""),
        "Metacritic": metacritic.get("score", ""),
        "当前在线": current_players,
        "成就数量": achievement_count,
        "成就样本列表": achievement_sample,
        "新闻样本数": news_count,
        "玩家评论样本数": review_count,
        "商店链接": _store_url(item.appid),
        "官网": data.get("website", ""),
        "头图": data.get("header_image", ""),
        "胶囊图": data.get("capsule_image", ""),
        "胶囊图V5": data.get("capsule_imagev5", ""),
        "背景图": data.get("background", ""),
        "原始背景图": data.get("background_raw", ""),
        "截图列表": _join([shot.get("path_full", "") for shot in screenshots if isinstance(shot, dict)]),
        "视频列表": _join([_movie_url(movie) for movie in movies if isinstance(movie, dict)]),
        "查询时间": query_time,
    }


def _build_review_row(item: SteamWorkItem, app_data: dict[str, Any], review: dict[str, Any], query_time: str) -> dict[str, Any]:
    author = review.get("author") if isinstance(review.get("author"), dict) else {}
    steamid = str(author.get("steamid", "") or "")
    return {
        "来源类型": item.source_type,
        "搜索词": item.source,
        "AppID": item.appid,
        "游戏名": app_data.get("name") or item.seed_name,
        "评论ID": review.get("recommendationid", ""),
        "SteamID": steamid,
        "是否推荐": _yes_no(review.get("voted_up")),
        "语言": review.get("language", ""),
        "Steam购买": _yes_no(review.get("steam_purchase")),
        "免费获得": _yes_no(review.get("received_for_free")),
        "抢先体验期间": _yes_no(review.get("written_during_early_access")),
        "主要在SteamDeck": _yes_no(review.get("primarily_steam_deck")),
        "拥有游戏数": author.get("num_games_owned", ""),
        "作者评论数": author.get("num_reviews", ""),
        "总游玩小时": _minutes_to_hours(author.get("playtime_forever")),
        "近两周游玩小时": _minutes_to_hours(author.get("playtime_last_two_weeks")),
        "评价时游玩小时": _minutes_to_hours(author.get("playtime_at_review")),
        "最后游玩时间": _format_timestamp(author.get("last_played")),
        "有价值票数": review.get("votes_up", ""),
        "欢乐票数": review.get("votes_funny", ""),
        "加权评分": review.get("weighted_vote_score", ""),
        "开发者回复": _clean_text(review.get("developer_response"), max_length=3000),
        "开发者回复时间": _format_timestamp(review.get("timestamp_dev_responded")),
        "发布时间": _format_timestamp(review.get("timestamp_created")),
        "最后更新时间": _format_timestamp(review.get("timestamp_updated")),
        "评论链接": f"https://steamcommunity.com/profiles/{steamid}/recommended/{item.appid}/" if steamid else "",
        "评论内容": _clean_text(review.get("review"), max_length=5000),
        "查询时间": query_time,
    }


def _build_news_row(item: SteamWorkItem, app_data: dict[str, Any], news: dict[str, Any], query_time: str) -> dict[str, Any]:
    return {
        "来源类型": item.source_type,
        "搜索词": item.source,
        "AppID": item.appid,
        "游戏名": app_data.get("name") or item.seed_name,
        "新闻ID": news.get("gid", ""),
        "标题": _clean_text(news.get("title")),
        "作者": news.get("author", ""),
        "发布时间": _format_timestamp(news.get("date")),
        "来源名称": news.get("feedlabel", ""),
        "来源标签": news.get("feedname", ""),
        "Feed名称": news.get("feedname", ""),
        "Feed类型": news.get("feed_type", ""),
        "外部链接": _yes_no(news.get("is_external_url")),
        "链接": news.get("url", ""),
        "内容摘要": _clean_text(news.get("contents"), max_length=2000),
        "查询时间": query_time,
    }


def _movie_url(movie: dict[str, Any]) -> str:
    webm = movie.get("webm") if isinstance(movie.get("webm"), dict) else {}
    mp4 = movie.get("mp4") if isinstance(movie.get("mp4"), dict) else {}
    return webm.get("max") or webm.get("480") or mp4.get("max") or mp4.get("480") or ""


def _store_url(appid: int) -> str:
    return f"https://store.steampowered.com/app/{appid}/"


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def run_steam_api_spider(
    api_key: str,
    app_ids_or_urls: str | list[str],
    keywords: str | list[str],
    language: str,
    country: str,
    collect_reviews_choice: str,
    max_reviews: int,
    collect_news_choice: str,
    max_news: int,
    log_callback,
    finish_callback,
    stop_event,
    *,
    pause_event=None,
    config: dict[str, Any] | None = None,
):
    config = config or {}
    api_key = str(api_key or "").strip()
    language = str(language or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE
    country = str(country or DEFAULT_COUNTRY).strip().upper() or DEFAULT_COUNTRY
    app_ids = parse_app_ids(app_ids_or_urls)
    keyword_list = normalize_keywords(keywords)
    if not app_ids and not keyword_list:
        raise ValueError("至少需要输入一个 Steam AppID/商店链接，或一个关键词。")

    max_apps_per_keyword = max(1, int(config.get("max_apps_per_keyword", 100) or 100))
    collect_reviews = str(collect_reviews_choice or "否") == "是"
    collect_news = str(collect_news_choice or "否") == "是"
    max_reviews = max(0, int(max_reviews or 0))
    max_news = max(0, int(max_news or 0))
    parallel_workers = max(1, min(8, int(config.get("parallel_workers", 1) or 1)))
    save_batch_size = max(1, int(config.get("save_batch_size", 10) or 10))
    collect_current_players = _config_bool(config, "collect_current_players", "是")
    collect_achievements = _config_bool(config, "collect_achievements", "否")
    include_non_games = str(config.get("include_non_games", "否") or "否")

    scope = {
        "app_ids": app_ids,
        "keywords": keyword_list,
        "language": language,
        "country": country,
        "collect_reviews": collect_reviews,
        "max_reviews": max_reviews,
        "collect_news": collect_news,
        "max_news": max_news,
        "max_apps_per_keyword": max_apps_per_keyword,
        "collect_current_players": collect_current_players,
        "collect_achievements": collect_achievements,
        "include_non_games": include_non_games,
    }
    checkpoint = open_task_checkpoint(
        "steam_api_research",
        scope,
        log_callback,
        merge_on_keys=("app_ids", "keywords"),
        merge_keep_keys=(
            "language",
            "country",
            "collect_reviews",
            "max_reviews",
            "collect_news",
            "max_news",
            "collect_current_players",
            "collect_achievements",
            "include_non_games",
        ),
    )
    default_output_path = build_output_path(
        "steam",
        f"steam_api_research_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
        channel="api_research",
    )
    output_path, writer = open_checkpointed_multi_sheet_writer(
        checkpoint,
        default_output_path,
        {"游戏信息": APP_FIELDS, "玩家评论": REVIEW_FIELDS, "新闻": NEWS_FIELDS},
        log_callback,
        autosave_every=save_batch_size,
    )
    checkpoint.add_output_path(output_path)

    items: list[SteamWorkItem] = []
    for appid in app_ids:
        items.append(SteamWorkItem(appid=appid, source_type="直接输入"))
    for keyword in keyword_list:
        if should_stop(stop_event):
            break
        log_line(log_callback, f"发现关键词候选游戏：{keyword}，上限 {max_apps_per_keyword} 个。")
        try:
            items.extend(
                discover_apps_for_keyword(
                    keyword,
                    api_key=api_key,
                    language=language,
                    country=country,
                    max_apps=max_apps_per_keyword,
                    config=config,
                    log_callback=log_callback,
                    stop_event=stop_event,
                    pause_event=pause_event,
                )
            )
        except Exception as exc:
            log_error(log_callback, f"关键词发现失败：{keyword}: {exc}")

    deduped: list[SteamWorkItem] = []
    seen_keys: set[str] = set()
    for item in items:
        if item.checkpoint_key not in seen_keys:
            seen_keys.add(item.checkpoint_key)
            deduped.append(item)
    log_line(log_callback, f"本次 Steam API 任务候选 {len(deduped)} 个采集项，并发数 {parallel_workers}。")

    submitted: dict[Any, SteamWorkItem] = {}
    completed_this_run = 0
    try:
        with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
            for item in deduped:
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                claimed, claim_status = checkpoint.claim_item(item.checkpoint_key)
                if not claimed:
                    if claim_status == "completed":
                        log_line(log_callback, f"断点跳过已完成游戏：{item.checkpoint_key}")
                    elif claim_status == "active":
                        log_line(log_callback, f"分流跳过正在采集的游戏：{item.checkpoint_key}")
                    continue
                future = executor.submit(
                    collect_steam_app_bundle,
                    item,
                    api_key=api_key,
                    language=language,
                    country=country,
                    collect_reviews=collect_reviews,
                    max_reviews=max_reviews,
                    collect_news=collect_news,
                    max_news=max_news,
                    config=config,
                    log_callback=log_callback,
                    stop_event=stop_event,
                    pause_event=pause_event,
                )
                submitted[future] = item

            total = len(submitted)
            for index, future in enumerate(as_completed(submitted), start=1):
                item = submitted[future]
                if should_stop(stop_event):
                    checkpoint.release_item(item.checkpoint_key)
                    continue
                try:
                    bundle = future.result()
                except InterruptedError:
                    checkpoint.release_item(item.checkpoint_key)
                    continue
                except Exception as exc:
                    checkpoint.release_item(item.checkpoint_key)
                    log_error(log_callback, f"[{index}/{total}] Steam 采集失败 AppID={item.appid}: {exc}")
                    continue
                writer.writerow("游戏信息", bundle.app_row)
                for row in bundle.review_rows:
                    writer.writerow("玩家评论", row)
                for row in bundle.news_rows:
                    writer.writerow("新闻", row)
                checkpoint.mark_completed(item.checkpoint_key, bundle.meta)
                completed_this_run += 1
                log_line(
                    log_callback,
                    f"[{index}/{total}] 完成 {bundle.app_row.get('游戏名') or item.appid}，"
                    f"评论 {len(bundle.review_rows)}，新闻 {len(bundle.news_rows)}。",
                )
    finally:
        try:
            writer.save()
        except Exception:
            pass
        checkpoint.close_run()

    if should_stop(stop_event):
        log_warn(log_callback, f"Steam API 任务已停止，本轮完成 {completed_this_run} 个采集项。")
    else:
        log_line(log_callback, f"Steam API 任务完成，本轮完成 {completed_this_run} 个采集项。")
    finish_callback(output_path)
    return output_path
