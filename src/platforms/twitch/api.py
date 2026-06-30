# -*- coding: utf-8 -*-
"""Twitch Helix API collectors for game content and KOL discovery."""

from __future__ import annotations

import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
from src.core.task_checkpoint import open_checkpointed_multi_sheet_writer, open_task_checkpoint
from src.core.xlsx import MultiSheetXlsxWriter
from src.platforms.twitch.sullygnome import (
    SULLYGNOME_GAME_SUMMARY_FIELDS,
    SULLYGNOME_VISIBLE_TABLE_FIELDS,
    collect_sullygnome_for_games,
)


AUTH_URL = "https://id.twitch.tv/oauth2/token"
HELIX_URL = "https://api.twitch.tv/helix"
DEFAULT_TIMEOUT = 30


GAME_FIELDS = [
    "来源类型",
    "搜索词",
    "Game ID",
    "游戏名",
    "封面图",
    "状态",
    "查询时间",
]

STREAM_FIELDS = [
    "来源类型",
    "搜索词",
    "Game ID",
    "游戏名",
    "直播ID",
    "主播ID",
    "主播Login",
    "主播名",
    "主页链接",
    "直播标题",
    "观众数",
    "语言",
    "开播时间",
    "直播类型",
    "标签",
    "是否成人内容",
    "缩略图URL",
    "查询时间",
]

VIDEO_FIELDS = [
    "来源类型",
    "搜索词",
    "Game ID",
    "游戏名",
    "视频ID",
    "Stream ID",
    "主播ID",
    "主播Login",
    "主播名",
    "主页链接",
    "视频标题",
    "视频简介",
    "播放量",
    "时长",
    "发布时间",
    "创建时间",
    "类型",
    "语言",
    "可见性",
    "视频链接",
    "缩略图URL",
    "静音片段",
    "查询时间",
]

CLIP_FIELDS = [
    "来源类型",
    "搜索词",
    "Game ID",
    "游戏名",
    "Clip ID",
    "频道ID",
    "频道名",
    "剪辑者ID",
    "剪辑者",
    "视频ID",
    "Clip标题",
    "播放量",
    "时长(秒)",
    "语言",
    "创建时间",
    "Clip链接",
    "嵌入链接",
    "缩略图URL",
    "是否精选",
    "查询时间",
]

TOP_GAME_FIELDS = [
    "排名",
    "Game ID",
    "游戏名",
    "封面图",
    "查询时间",
]

CHANNEL_PROFILE_FIELDS = [
    "频道ID",
    "频道Login",
    "主播名",
    "主页链接",
    "语言/市场",
    "当前游戏",
    "是否直播",
    "直播标题",
    "标签",
    "是否成人内容",
    "关注者",
    "创作者等级",
    "注册时间",
    "简介",
    "头像",
    "离线图",
    "命中关键词",
    "命中优先级",
    "命中市场",
    "来源",
    "最高VOD播放量",
    "查询时间",
]

KOL_FIELDS = [
    "排名",
    "主页链接",
    "主播名",
    "频道Login",
    "频道ID",
    "关注者",
    "语言/市场",
    "创作者等级",
    "级",
    "触达分",
    "相关分",
    "热度分",
    "安全分",
    "总分",
    "命中关键词",
    "来源",
    "当前游戏",
    "是否直播",
    "简介",
    "注册时间",
]

KOL_VOD_FIELDS = [
    "游戏名",
    "Game ID",
    "视频ID",
    "主播ID",
    "主播Login",
    "主播名",
    "视频标题",
    "播放量",
    "时长",
    "发布时间",
    "视频链接",
    "查询时间",
]

NOTE_FIELDS = ["项目", "说明"]


@dataclass
class TwitchGameItem:
    game_id: str
    name: str = ""
    box_art_url: str = ""
    source: str = ""
    source_type: str = "直接输入"
    status: str = "ok"

    @property
    def checkpoint_key(self) -> str:
        return (self.game_id or self.name or self.source).strip().lower()


@dataclass
class TwitchChannelCandidate:
    login: str
    channel_id: str = ""
    name: str = ""
    language: str = ""
    game_id: str = ""
    game_name: str = ""
    is_live: bool = False
    title: str = ""
    tags: list[str] = field(default_factory=list)
    is_mature: bool | str = ""
    hit_keywords: list[str] = field(default_factory=list)
    hit_priorities: list[str] = field(default_factory=list)
    hit_markets: list[str] = field(default_factory=list)
    source: str = "Search"
    vod_views: int = 0
    followers: int | None = None
    broadcaster_type: str = ""
    created_at: str = ""
    description: str = ""
    profile_image_url: str = ""
    offline_image_url: str = ""

    @property
    def url(self) -> str:
        return f"https://twitch.tv/{self.login}" if self.login else ""


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _yes_no(value: Any) -> str:
    if isinstance(value, str):
        if not value:
            return ""
        return "是" if value.lower() in {"true", "1", "yes", "是"} else "否"
    return "是" if bool(value) else "否"


def _config_bool(config: dict[str, Any], key: str, default: str = "否") -> bool:
    return str(config.get(key, default)).strip() == "是"


def normalize_lines(value: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(value, str):
        raw = value.splitlines()
    else:
        raw = list(value or [])
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", text).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def parse_game_inputs(value: str | list[str] | tuple[str, ...]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for text in normalize_lines(value):
        if re.fullmatch(r"\d+", text):
            items.append({"type": "id", "value": text})
        else:
            items.append({"type": "name", "value": text})
    return items


def parse_keyword_specs(value: str | list[str] | tuple[str, ...]) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    for text in normalize_lines(value):
        parts = [part.strip() for part in re.split(r"[|\t,，]", text) if part.strip()]
        keyword = parts[0] if parts else text
        priority = "P1"
        market = ""
        for part in parts[1:]:
            upper = part.upper()
            if upper in {"P0", "P1", "P2"}:
                priority = upper
            elif upper in {"EN", "JP", "JA", "ZH", "CN", "KR", "KO", "DE", "FR", "ES", "PT"}:
                market = "JP" if upper == "JA" else ("KR" if upper == "KO" else ("ZH" if upper == "CN" else upper))
        if not market:
            market = _infer_market(keyword)
        specs.append({"keyword": keyword, "priority": priority, "market": market})
    return specs


def _infer_market(text: str) -> str:
    if re.search(r"[\u3040-\u30ff]", text):
        return "JP"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "ZH"
    return "EN"


def _redact_sensitive_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(r"(?i)(client_secret=)[^&\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1<redacted>", text)
    return text


def _join(value: Any, sep: str = "\n") -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return sep.join(str(item) for item in value if item not in (None, ""))
    return str(value)


def _box_art_url(value: str, width: int = 285, height: int = 380) -> str:
    return str(value or "").replace("{width}", str(width)).replace("{height}", str(height))


def _thumb_url(value: str, width: int = 640, height: int = 360) -> str:
    return str(value or "").replace("{width}", str(width)).replace("{height}", str(height))


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _market_label(language: str) -> str:
    lang = str(language or "").lower()
    mapping = {"en": "EN", "ja": "JP", "zh": "ZH", "ko": "KR", "de": "DE", "fr": "FR", "pt": "PT", "es": "ES"}
    return mapping.get(lang, "Other") if lang else "Unknown"


def _duration_to_seconds(value: str) -> int:
    text = str(value or "")
    if not text:
        return 0
    total = 0
    for number, unit in re.findall(r"(\d+)([hms])", text):
        factor = {"h": 3600, "m": 60, "s": 1}.get(unit, 1)
        total += int(number) * factor
    return total


class TwitchClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        request_delay: float = 0.1,
        log_callback=None,
        stop_event=None,
        pause_event=None,
    ) -> None:
        self.client_id = str(client_id or os.environ.get("TWITCH_CLIENT_ID", "")).strip()
        self.client_secret = str(client_secret or os.environ.get("TWITCH_CLIENT_SECRET", "")).strip()
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.request_delay = float(request_delay or 0.0)
        self.log_callback = log_callback
        self.stop_event = stop_event
        self.pause_event = pause_event
        self._token = ""

    def token(self, *, force_refresh: bool = False) -> str:
        if self._token and not force_refresh:
            return self._token
        if not self.client_id or not self.client_secret:
            raise ValueError("Twitch Client ID 和 Client Secret 不能为空。")
        response = requests.post(
            AUTH_URL,
            data={"client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        self._token = str(payload.get("access_token") or "")
        if not self._token:
            raise RuntimeError("Twitch OAuth 未返回 access_token。")
        return self._token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Client-Id": self.client_id}

    def get(self, endpoint: str, params: dict[str, Any] | None = None, *, max_retries: int = 3) -> dict[str, Any]:
        url = endpoint if endpoint.startswith("http") else f"{HELIX_URL}{endpoint}"
        last_exc: Exception | None = None
        for attempt in range(max(1, int(max_retries or 1))):
            if wait_if_paused(self.pause_event, self.stop_event) or should_stop(self.stop_event):
                raise InterruptedError("任务已停止")
            try:
                response = requests.get(url, headers=self.headers(), params=params, timeout=self.timeout)
                if response.status_code in {401, 403} and attempt < max_retries - 1:
                    self.token(force_refresh=True)
                    interruptible_sleep(0.5, self.stop_event)
                    continue
                if response.status_code == 429 and attempt < max_retries - 1:
                    wait_seconds = self._rate_limit_wait(response, attempt)
                    log_warn(self.log_callback, f"Twitch API 触发限流，{wait_seconds:.1f} 秒后重试。")
                    interruptible_sleep(wait_seconds, self.stop_event)
                    continue
                if response.status_code in {500, 502, 503, 504} and attempt < max_retries - 1:
                    wait_seconds = min(30.0, 2.0 ** attempt + 1.0)
                    log_warn(self.log_callback, f"Twitch API HTTP {response.status_code}，{wait_seconds:.1f} 秒后重试。")
                    interruptible_sleep(wait_seconds, self.stop_event)
                    continue
                response.raise_for_status()
                payload = response.json()
                if self.request_delay > 0:
                    interruptible_sleep(self.request_delay, self.stop_event)
                return payload if isinstance(payload, dict) else {}
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    wait_seconds = min(30.0, 2.0 ** attempt + 1.0)
                    log_warn(self.log_callback, f"Twitch API 请求失败，{wait_seconds:.1f} 秒后重试：{_redact_sensitive_text(exc)}")
                    interruptible_sleep(wait_seconds, self.stop_event)
                    continue
                break
        if last_exc:
            raise RuntimeError(_redact_sensitive_text(last_exc)) from last_exc
        raise RuntimeError("Twitch API 请求失败")

    @staticmethod
    def _rate_limit_wait(response: requests.Response, attempt: int) -> float:
        reset = response.headers.get("Ratelimit-Reset")
        try:
            if reset:
                return max(1.0, float(reset) - time.time() + 0.5)
        except (TypeError, ValueError):
            pass
        return min(60.0, 2.0 ** attempt + 2.0)

    def paginate(self, endpoint: str, params: dict[str, Any], max_items: int, *, delay: float | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = ""
        page_delay = self.request_delay if delay is None else float(delay or 0.0)
        while len(items) < max_items:
            if wait_if_paused(self.pause_event, self.stop_event) or should_stop(self.stop_event):
                break
            page_params = dict(params)
            page_params["first"] = min(100, max_items - len(items))
            if cursor:
                page_params["after"] = cursor
            payload = self.get(endpoint, page_params)
            batch = payload.get("data", []) if isinstance(payload, dict) else []
            if not isinstance(batch, list) or not batch:
                break
            items.extend(item for item in batch if isinstance(item, dict))
            cursor = str((payload.get("pagination") or {}).get("cursor") or "")
            if not cursor:
                break
            if page_delay > 0:
                interruptible_sleep(page_delay, self.stop_event)
        return items[:max_items]


def resolve_games(client: TwitchClient, game_inputs: list[dict[str, str]], log_callback=None) -> list[TwitchGameItem]:
    games: list[TwitchGameItem] = []
    seen: set[str] = set()
    for raw in game_inputs:
        kind = raw.get("type", "name")
        value = raw.get("value", "").strip()
        if not value:
            continue
        try:
            if kind == "id":
                payload = client.get("/games", {"id": value})
            else:
                payload = client.get("/games", {"name": value})
        except Exception as exc:
            log_warn(log_callback, f"Twitch 游戏解析失败：{value}: {exc}")
            games.append(TwitchGameItem(game_id=value if kind == "id" else "", name=value, source=value, status="not_found"))
            continue
        data = payload.get("data", []) if isinstance(payload, dict) else []
        if data:
            item = data[0]
            game_id = str(item.get("id") or "")
            if not game_id or game_id in seen:
                continue
            seen.add(game_id)
            games.append(
                TwitchGameItem(
                    game_id=game_id,
                    name=str(item.get("name") or value),
                    box_art_url=_box_art_url(item.get("box_art_url", "")),
                    source=value,
                    source_type="Game ID" if kind == "id" else "游戏名",
                    status="ok",
                )
            )
        else:
            games.append(TwitchGameItem(game_id=value if kind == "id" else "", name=value, source=value, status="not_found"))
    return games


def collect_stream_rows(client: TwitchClient, game: TwitchGameItem, *, count: int, language: str = "") -> list[dict[str, Any]]:
    params: dict[str, Any] = {"game_id": game.game_id}
    if language:
        params["language"] = language.lower()
    streams = client.paginate("/streams", params, max(0, int(count or 0)))
    query_time = _now_text()
    return [_build_stream_row(game, stream, query_time) for stream in streams]


def collect_video_rows(
    client: TwitchClient,
    game: TwitchGameItem,
    *,
    count: int,
    period: str = "month",
    sort: str = "views",
    video_type: str = "archive",
    language: str = "",
    months_back: int = 0,
    min_views: int = 0,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "game_id": game.game_id,
        "period": period,
        "sort": sort,
        "type": video_type,
    }
    if language:
        params["language"] = language.lower()
    videos = client.paginate("/videos", params, max(0, int(count or 0)) * 3 if (months_back or min_views) else max(0, int(count or 0)))
    filtered: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(months_back or 0)) * 30) if months_back else None
    for video in videos:
        if cutoff:
            timestamp = str(video.get("published_at") or video.get("created_at") or "")
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if dt < cutoff:
                    continue
            except ValueError:
                pass
        if min_views and _to_int(video.get("view_count")) < min_views:
            continue
        filtered.append(video)
        if len(filtered) >= count:
            break
    query_time = _now_text()
    return [_build_video_row(game, video, query_time) for video in filtered[:count]]


def collect_clip_rows(
    client: TwitchClient,
    game: TwitchGameItem,
    *,
    count: int,
    days_back: int = 7,
    min_views: int = 0,
) -> list[dict[str, Any]]:
    ended_at = datetime.now(timezone.utc)
    started_at = ended_at - timedelta(days=max(1, int(days_back or 1)))
    params = {
        "game_id": game.game_id,
        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ended_at": ended_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    clips = client.paginate("/clips", params, max(0, int(count or 0)) * 3 if min_views else max(0, int(count or 0)))
    filtered = [clip for clip in clips if not min_views or _to_int(clip.get("view_count")) >= min_views]
    filtered.sort(key=lambda item: _to_int(item.get("view_count")), reverse=True)
    query_time = _now_text()
    return [_build_clip_row(game, clip, query_time) for clip in filtered[:count]]


def collect_top_games(client: TwitchClient, *, count: int) -> list[dict[str, Any]]:
    games = client.paginate("/games/top", {}, max(0, int(count or 0)))
    query_time = _now_text()
    rows: list[dict[str, Any]] = []
    for index, game in enumerate(games, start=1):
        rows.append(
            {
                "排名": index,
                "Game ID": game.get("id", ""),
                "游戏名": game.get("name", ""),
                "封面图": _box_art_url(game.get("box_art_url", "")),
                "查询时间": query_time,
            }
        )
    return rows


def _base_game_row(game: TwitchGameItem, query_time: str | None = None) -> dict[str, Any]:
    return {
        "来源类型": game.source_type,
        "搜索词": game.source,
        "Game ID": game.game_id,
        "游戏名": game.name,
        "封面图": game.box_art_url,
        "状态": game.status,
        "查询时间": query_time or _now_text(),
    }


def _build_stream_row(game: TwitchGameItem, stream: dict[str, Any], query_time: str) -> dict[str, Any]:
    login = str(stream.get("user_login") or "").lower()
    return {
        "来源类型": game.source_type,
        "搜索词": game.source,
        "Game ID": game.game_id,
        "游戏名": stream.get("game_name") or game.name,
        "直播ID": stream.get("id", ""),
        "主播ID": stream.get("user_id", ""),
        "主播Login": login,
        "主播名": stream.get("user_name", ""),
        "主页链接": f"https://twitch.tv/{login}" if login else "",
        "直播标题": stream.get("title", ""),
        "观众数": stream.get("viewer_count", ""),
        "语言": stream.get("language", ""),
        "开播时间": stream.get("started_at", ""),
        "直播类型": stream.get("type", ""),
        "标签": _join(stream.get("tags", []), ", "),
        "是否成人内容": _yes_no(stream.get("is_mature")),
        "缩略图URL": _thumb_url(stream.get("thumbnail_url", "")),
        "查询时间": query_time,
    }


def _build_video_row(game: TwitchGameItem, video: dict[str, Any], query_time: str) -> dict[str, Any]:
    login = str(video.get("user_login") or "").lower()
    return {
        "来源类型": game.source_type,
        "搜索词": game.source,
        "Game ID": game.game_id,
        "游戏名": game.name,
        "视频ID": video.get("id", ""),
        "Stream ID": video.get("stream_id", ""),
        "主播ID": video.get("user_id", ""),
        "主播Login": login,
        "主播名": video.get("user_name", ""),
        "主页链接": f"https://twitch.tv/{login}" if login else "",
        "视频标题": video.get("title", ""),
        "视频简介": video.get("description", ""),
        "播放量": video.get("view_count", ""),
        "时长": video.get("duration", ""),
        "发布时间": video.get("published_at", ""),
        "创建时间": video.get("created_at", ""),
        "类型": video.get("type", ""),
        "语言": video.get("language", ""),
        "可见性": video.get("viewable", ""),
        "视频链接": video.get("url", ""),
        "缩略图URL": _thumb_url(video.get("thumbnail_url", "")),
        "静音片段": _join(video.get("muted_segments", [])),
        "查询时间": query_time,
    }


def _build_clip_row(game: TwitchGameItem, clip: dict[str, Any], query_time: str) -> dict[str, Any]:
    return {
        "来源类型": game.source_type,
        "搜索词": game.source,
        "Game ID": clip.get("game_id") or game.game_id,
        "游戏名": game.name,
        "Clip ID": clip.get("id", ""),
        "频道ID": clip.get("broadcaster_id", ""),
        "频道名": clip.get("broadcaster_name", ""),
        "剪辑者ID": clip.get("creator_id", ""),
        "剪辑者": clip.get("creator_name", ""),
        "视频ID": clip.get("video_id", ""),
        "Clip标题": clip.get("title", ""),
        "播放量": clip.get("view_count", ""),
        "时长(秒)": clip.get("duration", ""),
        "语言": clip.get("language", ""),
        "创建时间": clip.get("created_at", ""),
        "Clip链接": clip.get("url", ""),
        "嵌入链接": clip.get("embed_url", ""),
        "缩略图URL": clip.get("thumbnail_url", ""),
        "是否精选": _yes_no(clip.get("is_featured")) if "is_featured" in clip else "",
        "查询时间": query_time,
    }


def run_twitch_game_content_spider(
    client_id: str,
    client_secret: str,
    games_input: str | list[str],
    collect_streams_choice: str,
    stream_count: int,
    collect_videos_choice: str,
    video_count: int,
    collect_clips_choice: str,
    clip_count: int,
    collect_top_games_choice: str,
    top_games_count: int,
    log_callback,
    finish_callback,
    stop_event,
    *,
    pause_event=None,
    config: dict[str, Any] | None = None,
):
    config = config or {}
    game_inputs = parse_game_inputs(games_input)
    if not game_inputs and str(collect_top_games_choice or "否") != "是":
        raise ValueError("请至少输入一个 Twitch 游戏名/Game ID，或开启 Top Games 采集。")

    timeout = float(config.get("request_timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    request_delay = float(config.get("request_delay", 0.1) or 0.0)
    save_batch_size = max(1, int(config.get("save_batch_size", 10) or 10))
    language = str(config.get("language", "") or "").strip()
    video_period = str(config.get("video_period", "month") or "month")
    video_sort = str(config.get("video_sort", "views") or "views")
    video_type = str(config.get("video_type", "archive") or "archive")
    video_months_back = max(0, int(config.get("video_months_back", 0) or 0))
    video_min_views = max(0, int(config.get("video_min_views", 0) or 0))
    clip_days_back = max(1, int(config.get("clip_days_back", 7) or 7))
    clip_min_views = max(0, int(config.get("clip_min_views", 0) or 0))
    collect_sullygnome = _config_bool(config, "collect_sullygnome", "否")
    sullygnome_summary_range = str(config.get("sullygnome_summary_range", "30") or "30").strip()
    sullygnome_collect_visible_tables = _config_bool(config, "sullygnome_collect_visible_tables", "是")
    sullygnome_visible_table_limit = max(1, min(100, int(config.get("sullygnome_visible_table_limit", 25) or 25)))
    sullygnome_max_scrolls = max(0, min(10, int(config.get("sullygnome_max_scrolls", 2) or 0)))
    sullygnome_request_delay = max(0.0, float(config.get("sullygnome_request_delay", 5.0) or 0.0))
    sullygnome_page_timeout = max(5000, int(config.get("sullygnome_page_timeout", 30000) or 30000))
    sullygnome_browser = str(config.get("sullygnome_browser", "Chrome") or "Chrome").strip() or "Chrome"
    collect_streams = str(collect_streams_choice or "否") == "是"
    collect_videos = str(collect_videos_choice or "否") == "是"
    collect_clips = str(collect_clips_choice or "否") == "是"
    collect_top = str(collect_top_games_choice or "否") == "是"

    scope = {
        "games": game_inputs,
        "collect_streams": collect_streams,
        "stream_count": int(stream_count or 0),
        "collect_videos": collect_videos,
        "video_count": int(video_count or 0),
        "collect_clips": collect_clips,
        "clip_count": int(clip_count or 0),
        "collect_top_games": collect_top,
        "top_games_count": int(top_games_count or 0),
        "language": language,
        "video_period": video_period,
        "video_sort": video_sort,
        "video_type": video_type,
        "video_months_back": video_months_back,
        "video_min_views": video_min_views,
        "clip_days_back": clip_days_back,
        "clip_min_views": clip_min_views,
        "collect_sullygnome": collect_sullygnome,
        "sullygnome_summary_range": sullygnome_summary_range,
        "sullygnome_collect_visible_tables": sullygnome_collect_visible_tables,
        "sullygnome_visible_table_limit": sullygnome_visible_table_limit,
        "sullygnome_max_scrolls": sullygnome_max_scrolls,
    }
    checkpoint = open_task_checkpoint(
        "twitch_game_content",
        scope,
        log_callback,
        merge_on_keys=("games",),
        merge_keep_keys=(
            "collect_streams",
            "stream_count",
            "collect_videos",
            "video_count",
            "collect_clips",
            "clip_count",
            "language",
            "video_period",
            "video_sort",
            "video_type",
            "video_months_back",
            "video_min_views",
            "clip_days_back",
            "clip_min_views",
            "collect_sullygnome",
            "sullygnome_summary_range",
            "sullygnome_collect_visible_tables",
            "sullygnome_visible_table_limit",
            "sullygnome_max_scrolls",
        ),
    )
    default_output_path = build_output_path(
        "twitch",
        f"twitch_game_content_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
        channel="game_content",
    )
    output_path, writer = open_checkpointed_multi_sheet_writer(
        checkpoint,
        default_output_path,
        {
            "游戏信息": GAME_FIELDS,
            "当前直播": STREAM_FIELDS,
            "VOD回放": VIDEO_FIELDS,
            "Clips片段": CLIP_FIELDS,
            "TopGames": TOP_GAME_FIELDS,
            "SullyGnome摘要": SULLYGNOME_GAME_SUMMARY_FIELDS,
            "SullyGnome可见表": SULLYGNOME_VISIBLE_TABLE_FIELDS,
        },
        log_callback,
        autosave_every=save_batch_size,
    )
    checkpoint.add_output_path(output_path)

    client = TwitchClient(
        client_id,
        client_secret,
        timeout=timeout,
        request_delay=request_delay,
        log_callback=log_callback,
        stop_event=stop_event,
        pause_event=pause_event,
    )
    completed = 0
    try:
        if collect_top:
            top_key = f"top_games:{int(top_games_count or 0)}"
            claimed, claim_status = checkpoint.claim_item(top_key)
            if claimed:
                log_line(log_callback, f"采集 Twitch Top Games：{top_games_count} 个。")
                rows = collect_top_games(client, count=top_games_count)
                for row in rows:
                    writer.writerow("TopGames", row)
                checkpoint.mark_completed(top_key, {"top_game_rows": len(rows)})
            elif claim_status == "completed":
                log_line(log_callback, "断点跳过已完成 Top Games。")

        games = resolve_games(client, game_inputs, log_callback)
        log_line(log_callback, f"本次 Twitch 游戏内容任务候选 {len(games)} 个游戏。")
        sullygnome_games: list[TwitchGameItem] = []
        for index, game in enumerate(games, start=1):
            if wait_if_paused(pause_event, stop_event) or should_stop(stop_event):
                break
            key = game.checkpoint_key
            claimed, claim_status = checkpoint.claim_item(key)
            if not claimed:
                if claim_status == "completed":
                    log_line(log_callback, f"断点跳过已完成游戏：{key}")
                elif claim_status == "active":
                    log_line(log_callback, f"分流跳过正在采集的游戏：{key}")
                continue
            try:
                writer.writerow("游戏信息", _base_game_row(game))
                if game.status != "ok" or not game.game_id:
                    checkpoint.mark_completed(key, {"status": game.status, "stream_rows": 0, "video_rows": 0, "clip_rows": 0})
                    continue
                stream_rows = collect_stream_rows(client, game, count=stream_count, language=language) if collect_streams else []
                for row in stream_rows:
                    writer.writerow("当前直播", row)
                video_rows = (
                    collect_video_rows(
                        client,
                        game,
                        count=video_count,
                        period=video_period,
                        sort=video_sort,
                        video_type=video_type,
                        language=language,
                        months_back=video_months_back,
                        min_views=video_min_views,
                    )
                    if collect_videos
                    else []
                )
                for row in video_rows:
                    writer.writerow("VOD回放", row)
                clip_rows = collect_clip_rows(client, game, count=clip_count, days_back=clip_days_back, min_views=clip_min_views) if collect_clips else []
                for row in clip_rows:
                    writer.writerow("Clips片段", row)
                checkpoint.mark_completed(
                    key,
                    {"status": "ok", "stream_rows": len(stream_rows), "video_rows": len(video_rows), "clip_rows": len(clip_rows)},
                )
                if collect_sullygnome:
                    sullygnome_games.append(game)
                completed += 1
                log_line(log_callback, f"[{index}/{len(games)}] 完成 {game.name}：直播 {len(stream_rows)}，VOD {len(video_rows)}，Clips {len(clip_rows)}。")
            except InterruptedError:
                checkpoint.release_item(key)
                break
            except Exception as exc:
                checkpoint.release_item(key)
                log_error(log_callback, f"[{index}/{len(games)}] Twitch 游戏采集失败 {game.name or game.game_id}: {exc}")
                continue

        if collect_sullygnome and sullygnome_games and not should_stop(stop_event):
            log_line(log_callback, f"开始 SullyGnome 低频补采：{len(sullygnome_games)} 个游戏。")
            summary_rows, visible_table_rows = collect_sullygnome_for_games(
                sullygnome_games,
                browser=sullygnome_browser,
                summary_range=sullygnome_summary_range,
                collect_visible_tables=sullygnome_collect_visible_tables,
                visible_table_limit=sullygnome_visible_table_limit,
                max_scrolls=sullygnome_max_scrolls,
                request_delay=sullygnome_request_delay,
                page_timeout=sullygnome_page_timeout,
                log_callback=log_callback,
                stop_event=stop_event,
                pause_event=pause_event,
            )
            for row in summary_rows:
                writer.writerow("SullyGnome摘要", row)
            for row in visible_table_rows:
                writer.writerow("SullyGnome可见表", row)
            log_line(log_callback, f"SullyGnome 补采完成：摘要 {len(summary_rows)} 行，可见表 {len(visible_table_rows)} 行。")
    finally:
        try:
            writer.save()
        except Exception:
            pass
        checkpoint.close_run()

    log_line(log_callback, f"Twitch 游戏内容任务完成，本轮完成 {completed} 个游戏。")
    finish_callback(output_path)
    return output_path


def search_channel_candidates(
    client: TwitchClient,
    keyword_specs: list[dict[str, str]],
    *,
    per_keyword: int,
    live_only: bool,
    log_callback=None,
) -> dict[str, TwitchChannelCandidate]:
    channels: dict[str, TwitchChannelCandidate] = {}
    total = len(keyword_specs)
    for index, spec in enumerate(keyword_specs, start=1):
        keyword = spec["keyword"]
        log_line(log_callback, f"[{index}/{total}] 搜索 Twitch 频道：{keyword}")
        rows = client.paginate(
            "/search/channels",
            {"query": keyword, "live_only": str(bool(live_only)).lower()},
            max(0, int(per_keyword or 0)),
        )
        for row in rows:
            login = str(row.get("broadcaster_login") or row.get("login") or "").lower()
            if not login:
                continue
            channel = channels.get(login)
            if channel is None:
                channel = TwitchChannelCandidate(
                    login=login,
                    channel_id=str(row.get("broadcaster_id") or row.get("id") or ""),
                    name=str(row.get("display_name") or row.get("broadcaster_name") or login),
                    language=str(row.get("broadcaster_language") or ""),
                    game_id=str(row.get("game_id") or ""),
                    game_name=str(row.get("game_name") or ""),
                    is_live=bool(row.get("is_live")),
                    title=str(row.get("title") or ""),
                    tags=[str(item) for item in (row.get("tags") or []) if item],
                    is_mature=row.get("is_mature", ""),
                    source="Search",
                )
                channels[login] = channel
            _append_hit(channel, spec)
    return channels


def mine_vod_candidates(
    client: TwitchClient,
    game_inputs: list[dict[str, str]],
    channels: dict[str, TwitchChannelCandidate],
    *,
    max_vods_per_game: int,
    log_callback=None,
) -> list[dict[str, Any]]:
    vod_rows: list[dict[str, Any]] = []
    games = resolve_games(client, game_inputs, log_callback)
    for game in games:
        if game.status != "ok" or not game.game_id:
            continue
        log_line(log_callback, f"VOD 挖掘：{game.name}，上限 {max_vods_per_game} 条。")
        videos = client.paginate(
            "/videos",
            {"game_id": game.game_id, "type": "archive", "period": "all", "sort": "views"},
            max(0, int(max_vods_per_game or 0)),
        )
        query_time = _now_text()
        for video in videos:
            login = str(video.get("user_login") or "").lower()
            if not login:
                continue
            views = _to_int(video.get("view_count"))
            channel = channels.get(login)
            if channel is None:
                channel = TwitchChannelCandidate(
                    login=login,
                    channel_id=str(video.get("user_id") or ""),
                    name=str(video.get("user_name") or login),
                    game_id=game.game_id,
                    game_name=game.name,
                    source="VOD",
                    vod_views=views,
                )
                channels[login] = channel
            else:
                channel.vod_views = max(channel.vod_views, views)
                if channel.source == "Search":
                    channel.source = "Search+VOD"
            _append_hit(channel, {"keyword": game.name, "priority": "P0", "market": _infer_market(game.name)})
            vod_rows.append(
                {
                    "游戏名": game.name,
                    "Game ID": game.game_id,
                    "视频ID": video.get("id", ""),
                    "主播ID": video.get("user_id", ""),
                    "主播Login": login,
                    "主播名": video.get("user_name", ""),
                    "视频标题": video.get("title", ""),
                    "播放量": video.get("view_count", ""),
                    "时长": video.get("duration", ""),
                    "发布时间": video.get("published_at", ""),
                    "视频链接": video.get("url", ""),
                    "查询时间": query_time,
                }
            )
    return vod_rows


def _append_hit(channel: TwitchChannelCandidate, spec: dict[str, str]) -> None:
    keyword = spec.get("keyword", "").strip()
    if keyword and keyword not in channel.hit_keywords:
        channel.hit_keywords.append(keyword)
        channel.hit_priorities.append(spec.get("priority", "P1") or "P1")
        channel.hit_markets.append(spec.get("market", "") or _infer_market(keyword))


def enrich_channels(
    client: TwitchClient,
    channels: list[TwitchChannelCandidate],
    *,
    workers: int,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> None:
    workers = max(1, min(8, int(workers or 1)))
    log_line(log_callback, f"开始富化 Twitch 主播画像：{len(channels)} 人，并发 {workers}。")

    def enrich_one(channel: TwitchChannelCandidate) -> dict[str, Any]:
        result: dict[str, Any] = {"login": channel.login}
        if channel.channel_id:
            try:
                followers = client.get("/channels/followers", {"broadcaster_id": channel.channel_id})
                result["followers"] = followers.get("total")
            except Exception as exc:
                result["followers_error"] = str(exc)
            try:
                users = client.get("/users", {"id": channel.channel_id})
                data = users.get("data", []) if isinstance(users, dict) else []
                if data:
                    user = data[0]
                    result.update(
                        {
                            "broadcaster_type": user.get("broadcaster_type", ""),
                            "created_at": user.get("created_at", ""),
                            "description": user.get("description", ""),
                            "profile_image_url": user.get("profile_image_url", ""),
                            "offline_image_url": user.get("offline_image_url", ""),
                            "display_name": user.get("display_name", ""),
                        }
                    )
            except Exception as exc:
                result["user_error"] = str(exc)
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(enrich_one, channel): channel for channel in channels}
        for index, future in enumerate(as_completed(futures), start=1):
            if wait_if_paused(pause_event, stop_event) or should_stop(stop_event):
                break
            channel = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                log_warn(log_callback, f"Twitch 主播画像富化失败 {channel.login}: {exc}")
                continue
            if result.get("followers") is not None:
                channel.followers = _to_int(result.get("followers"))
            channel.broadcaster_type = str(result.get("broadcaster_type") or channel.broadcaster_type or "")
            channel.created_at = str(result.get("created_at") or channel.created_at or "")
            channel.description = str(result.get("description") or channel.description or "")
            channel.profile_image_url = str(result.get("profile_image_url") or channel.profile_image_url or "")
            channel.offline_image_url = str(result.get("offline_image_url") or channel.offline_image_url or "")
            if result.get("display_name"):
                channel.name = str(result["display_name"])
            if index % 40 == 0:
                log_line(log_callback, f"  已富化 {index}/{len(channels)} 人。")


def score_channel(channel: TwitchChannelCandidate) -> dict[str, Any]:
    followers = channel.followers
    reach_raw = _norm_log(followers, [(1_000_000, 100), (100_000, 80), (10_000, 55), (1_000, 30), (100, 15), (50, 5)])
    reach = (reach_raw or 0) * 0.35

    priority_map = {"P0": 1.0, "P1": 0.8, "P2": 0.6}
    priorities = channel.hit_priorities or ["P1"]
    priority_scores = [priority_map.get(str(item).upper(), 0.5) for item in priorities]
    avg_priority = sum(priority_scores) / max(len(priority_scores), 1)
    keyword_count = len(channel.hit_keywords)
    keyword_bonus = 10 if keyword_count >= 3 else (5 if keyword_count >= 2 else 0)
    relevance_raw = min(avg_priority * 70 + keyword_bonus, 100)
    relevance = relevance_raw * 0.30

    vod_views = max(0, int(channel.vod_views or 0))
    if vod_views > 0 and followers and followers > 0:
        content_raw = _norm_log(vod_views / followers, [(10, 100), (5, 85), (2, 65), (1, 50), (0.5, 35), (0.1, 15)])
    elif vod_views > 0:
        content_raw = _norm_log(vod_views, [(1_000_000, 100), (100_000, 80), (10_000, 55), (1_000, 30), (100, 15)])
    else:
        content_raw = 0
    content = (content_raw or 0) * 0.20

    safety_raw = 100
    if channel.is_mature is True or any("mature" in tag.lower() for tag in channel.tags):
        safety_raw -= 30
    lang = channel.language.lower()
    if lang in {"en", "ja", "zh", "ko"}:
        safety_raw += 5
    elif lang:
        safety_raw -= 10
    safety_raw = max(0, min(100, safety_raw))
    safety = safety_raw * 0.15

    total = reach + relevance + content + safety
    tier = "S" if total >= 65 else ("A" if total >= 50 else ("B" if total >= 35 else "C"))
    return {
        "reach": round(reach, 1),
        "relevance": round(relevance, 1),
        "content": round(content, 1),
        "safety": round(safety, 1),
        "total": round(total, 1),
        "tier": tier,
    }


def _norm_log(value: float | int | None, benches: list[tuple[float, int]]) -> int | None:
    if value is None:
        return None
    for threshold, score in benches:
        if value >= threshold:
            return score
    return 0


def _channel_profile_row(channel: TwitchChannelCandidate, query_time: str) -> dict[str, Any]:
    return {
        "频道ID": channel.channel_id,
        "频道Login": channel.login,
        "主播名": channel.name,
        "主页链接": channel.url,
        "语言/市场": _market_label(channel.language),
        "当前游戏": channel.game_name,
        "是否直播": _yes_no(channel.is_live),
        "直播标题": channel.title,
        "标签": _join(channel.tags, ", "),
        "是否成人内容": _yes_no(channel.is_mature),
        "关注者": "" if channel.followers is None else channel.followers,
        "创作者等级": channel.broadcaster_type or "",
        "注册时间": channel.created_at,
        "简介": channel.description,
        "头像": channel.profile_image_url,
        "离线图": channel.offline_image_url,
        "命中关键词": _join(channel.hit_keywords, ", "),
        "命中优先级": _join(channel.hit_priorities, ", "),
        "命中市场": _join(channel.hit_markets, ", "),
        "来源": channel.source,
        "最高VOD播放量": channel.vod_views or "",
        "查询时间": query_time,
    }


def _kol_row(rank: int, channel: TwitchChannelCandidate, scores: dict[str, Any]) -> dict[str, Any]:
    bt = str(channel.broadcaster_type or "").lower()
    bt_label = "Partner" if bt == "partner" else ("Affiliate" if bt == "affiliate" else "-")
    return {
        "排名": rank,
        "主页链接": channel.url,
        "主播名": channel.name,
        "频道Login": channel.login,
        "频道ID": channel.channel_id,
        "关注者": "" if channel.followers is None else channel.followers,
        "语言/市场": _market_label(channel.language),
        "创作者等级": bt_label,
        "级": scores["tier"],
        "触达分": scores["reach"],
        "相关分": scores["relevance"],
        "热度分": scores["content"],
        "安全分": scores["safety"],
        "总分": scores["total"],
        "命中关键词": _join(channel.hit_keywords[:8], ", "),
        "来源": channel.source,
        "当前游戏": channel.game_name,
        "是否直播": _yes_no(channel.is_live),
        "简介": channel.description,
        "注册时间": channel.created_at,
    }


def _note_rows() -> list[dict[str, str]]:
    return [
        {"项目": "核心指标", "说明": "关注者数来自 GET /helix/channels/followers 的 total；Twitch 付费订阅者只能查自己频道，不适合第三方 KOL 批量筛选。"},
        {"项目": "发现路径", "说明": "关键词 Search Channels 发现主播；可选用游戏名/Game ID 通过 Get Videos 挖掘热门 VOD 作者。"},
        {"项目": "评分公式", "说明": "总分 = Reach(35%) + Relevance(30%) + Content(20%) + Safety(15%)。"},
        {"项目": "Reach", "说明": "关注者数分段归一化：1M=100，100K=80，10K=55，1K=30，100=15，50=5。"},
        {"项目": "Relevance", "说明": "关键词优先级：P0=1.0，P1=0.8，P2=0.6；命中 >=3 词加 10 分，>=2 词加 5 分。"},
        {"项目": "Content", "说明": "优先使用最高 VOD 播放量/关注者比；无关注者时用 VOD 绝对播放量。"},
        {"项目": "Safety", "说明": "mature 标签扣分；目标语言 en/ja/zh/ko 加分，其他语言轻微扣分。"},
        {"项目": "分级", "说明": "S >= 65，A >= 50，B >= 35，C < 35。"},
    ]


def run_twitch_kol_discovery_spider(
    client_id: str,
    client_secret: str,
    keywords: str | list[str],
    vod_game_names: str | list[str],
    log_callback,
    finish_callback,
    stop_event,
    *,
    pause_event=None,
    config: dict[str, Any] | None = None,
):
    config = config or {}
    keyword_specs = parse_keyword_specs(keywords)
    game_inputs = parse_game_inputs(vod_game_names)
    if not keyword_specs and not game_inputs:
        raise ValueError("请至少输入关键词，或输入用于 VOD 挖掘的 Twitch 游戏名/Game ID。")

    timeout = float(config.get("request_timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    request_delay = float(config.get("request_delay", 0.1) or 0.0)
    per_keyword = max(1, int(config.get("search_count_per_keyword", 8) or 8))
    max_vods_per_game = max(0, int(config.get("max_vods_per_game", 10) or 0))
    live_only = _config_bool(config, "search_live_only", "否")
    enrich_workers = max(1, min(8, int(config.get("enrich_workers", 5) or 5)))
    min_followers = max(0, int(config.get("min_followers", 50) or 0))
    min_total_score = float(config.get("min_total_score", 20) or 0)
    save_batch_size = max(1, int(config.get("save_batch_size", 10) or 10))

    output_path = build_output_path(
        "twitch",
        f"twitch_kol_discovery_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
        channel="kol_discovery",
    )
    writer = MultiSheetXlsxWriter(
        output_path,
        {
            "KOL推荐": KOL_FIELDS,
            "主播画像": CHANNEL_PROFILE_FIELDS,
            "命中VOD": KOL_VOD_FIELDS,
            "数据说明": NOTE_FIELDS,
        },
        autosave_every=save_batch_size,
    )

    client = TwitchClient(
        client_id,
        client_secret,
        timeout=timeout,
        request_delay=request_delay,
        log_callback=log_callback,
        stop_event=stop_event,
        pause_event=pause_event,
    )

    try:
        channels_map: dict[str, TwitchChannelCandidate] = {}
        if keyword_specs:
            channels_map.update(search_channel_candidates(client, keyword_specs, per_keyword=per_keyword, live_only=live_only, log_callback=log_callback))
            log_line(log_callback, f"Search Channels 发现 {len(channels_map)} 个去重主播。")
        vod_rows: list[dict[str, Any]] = []
        if game_inputs and max_vods_per_game > 0:
            vod_rows = mine_vod_candidates(client, game_inputs, channels_map, max_vods_per_game=max_vods_per_game, log_callback=log_callback)
            log_line(log_callback, f"VOD 挖掘后候选池 {len(channels_map)} 个主播，命中 VOD {len(vod_rows)} 条。")
        channels = [channel for channel in channels_map.values() if channel.channel_id or channel.login]
        enrich_channels(client, channels, workers=enrich_workers, log_callback=log_callback, stop_event=stop_event, pause_event=pause_event)

        query_time = _now_text()
        scored: list[tuple[TwitchChannelCandidate, dict[str, Any]]] = []
        for channel in channels:
            scores = score_channel(channel)
            if channel.followers is None and scores["total"] < min_total_score:
                continue
            if channel.followers is not None and channel.followers < min_followers and scores["total"] < min_total_score:
                continue
            scored.append((channel, scores))
        scored.sort(key=lambda item: item[1]["total"], reverse=True)

        for rank, (channel, scores) in enumerate(scored, start=1):
            writer.writerow("KOL推荐", _kol_row(rank, channel, scores))
        for channel in channels:
            writer.writerow("主播画像", _channel_profile_row(channel, query_time))
        for row in vod_rows:
            writer.writerow("命中VOD", row)
        for row in _note_rows():
            writer.writerow("数据说明", row)
        writer.save()
    finally:
        try:
            writer.save()
        except Exception:
            pass

    tier_count: dict[str, int] = {"S": 0, "A": 0, "B": 0, "C": 0}
    for _, scores in scored:
        tier_count[str(scores["tier"])] = tier_count.get(str(scores["tier"]), 0) + 1
    log_line(
        log_callback,
        f"Twitch KOL 发现完成：输出 {len(scored)} 个推荐主播；S={tier_count.get('S', 0)} A={tier_count.get('A', 0)} "
        f"B={tier_count.get('B', 0)} C={tier_count.get('C', 0)}。",
    )
    finish_callback(output_path)
    return output_path
