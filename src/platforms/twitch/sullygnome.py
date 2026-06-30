# -*- coding: utf-8 -*-
"""Low-frequency SullyGnome page supplements for Twitch game collection."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from src.core import cdp_url_for_browser, connect_existing_chromium, interruptible_sleep, log_line, log_warn, should_stop, wait_if_paused

BASE_URL = "https://sullygnome.com"

SULLYGNOME_GAME_SUMMARY_FIELDS = [
    "来源类型",
    "搜索词",
    "Game ID",
    "游戏名",
    "SullyGnome Slug",
    "SullyGnome URL",
    "统计范围",
    "时区",
    "Twitch目录链接",
    "Average viewers rank",
    "Peak viewers rank",
    "Average channels rank",
    "Peak channels rank",
    "Hours watched",
    "Hours watched变化",
    "Hours streamed",
    "Hours streamed变化",
    "Average viewers",
    "Average viewers变化",
    "Average channels",
    "Average channels变化",
    "Viewer ratio",
    "Viewer ratio变化",
    "Max viewers",
    "Max viewers变化",
    "Streamers",
    "Streamers变化",
    "图表链接",
    "状态",
    "备注",
    "查询时间",
]

SULLYGNOME_VISIBLE_TABLE_FIELDS = [
    "来源类型",
    "搜索词",
    "Game ID",
    "游戏名",
    "SullyGnome Slug",
    "表格类型",
    "行号",
    "实体名称",
    "实体链接",
    "可见指标文本",
    "状态",
    "备注",
    "查询时间",
]


@dataclass
class SullyGnomeGameRef:
    game_id: str = ""
    name: str = ""
    source: str = ""
    source_type: str = ""


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def normalize_sullygnome_game_slug(value: str) -> str:
    """Convert a Twitch game name or SullyGnome path fragment to a conservative game slug."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^https?://(?:www\.)?sullygnome\.com/game/", "", text, flags=re.I)
    text = text.split("?")[0].split("#")[0].strip("/")
    if "/" in text:
        text = text.split("/")[0]
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return quote(text.strip("_"), safe="_-().!~*'")


def build_sullygnome_game_url(game_name_or_slug: str, summary_range: str | int = "30") -> str:
    slug = normalize_sullygnome_game_slug(game_name_or_slug)
    if not slug:
        return ""
    range_text = str(summary_range or "30").strip().lower()
    if range_text in {"", "30", "default", "month"}:
        return f"{BASE_URL}/game/{slug}"
    return f"{BASE_URL}/game/{slug}/{range_text}/summary"


def build_sullygnome_game_table_url(game_name_or_slug: str, table_type: str = "watched", summary_range: str | int = "30") -> str:
    slug = normalize_sullygnome_game_slug(game_name_or_slug)
    if not slug:
        return ""
    table = re.sub(r"[^a-z0-9_-]+", "", str(table_type or "watched").strip().lower()) or "watched"
    range_text = str(summary_range or "30").strip().lower()
    if range_text in {"", "30", "default", "month"}:
        return f"{BASE_URL}/game/{slug}/{table}"
    return f"{BASE_URL}/game/{slug}/{range_text}/{table}"


def empty_sullygnome_summary_row(game: SullyGnomeGameRef | Any, *, slug: str = "", url: str = "", status: str = "", note: str = "") -> dict[str, Any]:
    return {
        "来源类型": getattr(game, "source_type", ""),
        "搜索词": getattr(game, "source", ""),
        "Game ID": getattr(game, "game_id", ""),
        "游戏名": getattr(game, "name", ""),
        "SullyGnome Slug": slug,
        "SullyGnome URL": url,
        "统计范围": "",
        "时区": "",
        "Twitch目录链接": "",
        "Average viewers rank": "",
        "Peak viewers rank": "",
        "Average channels rank": "",
        "Peak channels rank": "",
        "Hours watched": "",
        "Hours watched变化": "",
        "Hours streamed": "",
        "Hours streamed变化": "",
        "Average viewers": "",
        "Average viewers变化": "",
        "Average channels": "",
        "Average channels变化": "",
        "Viewer ratio": "",
        "Viewer ratio变化": "",
        "Max viewers": "",
        "Max viewers变化": "",
        "Streamers": "",
        "Streamers变化": "",
        "图表链接": "",
        "状态": status,
        "备注": note,
        "查询时间": now_text(),
    }


def build_sullygnome_visible_table_row(
    game: SullyGnomeGameRef | Any,
    *,
    slug: str,
    table_type: str,
    rank: int,
    entity_name: str = "",
    entity_url: str = "",
    metrics_text: str = "",
    status: str = "ok",
    note: str = "",
) -> dict[str, Any]:
    return {
        "来源类型": getattr(game, "source_type", ""),
        "搜索词": getattr(game, "source", ""),
        "Game ID": getattr(game, "game_id", ""),
        "游戏名": getattr(game, "name", ""),
        "SullyGnome Slug": slug,
        "表格类型": table_type,
        "行号": rank,
        "实体名称": entity_name,
        "实体链接": entity_url,
        "可见指标文本": metrics_text,
        "状态": status,
        "备注": note,
        "查询时间": now_text(),
    }


def _first_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            return _clean_text(match.group(1))
    return ""


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _metric_value(text: str, label: str) -> str:
    escaped = re.escape(label)
    patterns = [
        rf"{escaped}\s+([0-9][0-9,\.]*\s*[KMBkmb]?|[0-9][0-9,\.]*\s*%)",
        rf"{escaped}[^\n\r]{{0,80}}?([0-9][0-9,\.]*\s*[KMBkmb]?|[0-9][0-9,\.]*\s*%)",
    ]
    return _first_match(text, patterns)


def _metric_change(text: str, label: str) -> str:
    escaped = re.escape(label)
    return _first_match(
        text,
        [
            rf"{escaped}[^\n\r]{{0,160}}?((?:\+|-)?[0-9][0-9,\.]*\s*%[^\n\r]{{0,50}})",
            rf"{escaped}[^\n\r]{{0,160}}?((?:\+|-)[0-9][0-9,\.]*\s*[KMBkmb]?[^\n\r]{{0,50}})",
        ],
    )


def _rank_value(text: str, label: str) -> str:
    escaped = re.escape(label)
    return _first_match(text, [rf"{escaped}\s*#?\s*([0-9][0-9,]*)", rf"#\s*([0-9][0-9,]*)\s*{escaped}"])


def parse_sullygnome_summary_from_text(text: str, game: SullyGnomeGameRef | Any, *, slug: str, url: str, page_url: str = "") -> dict[str, Any]:
    body = str(text or "")
    row = empty_sullygnome_summary_row(game, slug=slug, url=page_url or url, status="ok")
    row["统计范围"] = _first_match(body, [r"past\s+([0-9]+\s+days)", r"Currently showing stats for\s+([^\n\r]+)"])
    row["时区"] = _first_match(body, [r"Times displayed are shown in\s+([A-Z]+)", r"timezone\s*[:：]?\s*([A-Z]+)"])
    row["Average viewers rank"] = _rank_value(body, "Average viewers rank")
    row["Peak viewers rank"] = _rank_value(body, "Peak viewers rank")
    row["Average channels rank"] = _rank_value(body, "Average channels rank")
    row["Peak channels rank"] = _rank_value(body, "Peak channels rank")
    for label, field in [
        ("Hours watched", "Hours watched"),
        ("Hours streamed", "Hours streamed"),
        ("Average viewers", "Average viewers"),
        ("Average channels", "Average channels"),
        ("Viewer ratio", "Viewer ratio"),
        ("Max viewers", "Max viewers"),
        ("Streamers", "Streamers"),
    ]:
        row[field] = _metric_value(body, label)
        row[f"{field}变化"] = _metric_change(body, label)
    return row


def _extract_links(page) -> tuple[str, str]:
    try:
        links = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href || '', text: (a.innerText || a.textContent || '').trim()
            })).filter(x => x.href)
            """
        )
    except Exception:
        return "", ""
    twitch_url = ""
    chart_links: list[str] = []
    for item in links or []:
        href = str((item or {}).get("href") or "")
        text = str((item or {}).get("text") or "")
        if not twitch_url and "twitch.tv/directory/game" in href:
            twitch_url = href
        if "/images/charts/" in href or "chart" in text.lower():
            chart_links.append(href)
    seen: set[str] = set()
    unique_charts = []
    for href in chart_links:
        if href and href not in seen:
            seen.add(href)
            unique_charts.append(href)
    return twitch_url, "\n".join(unique_charts[:20])


def collect_sullygnome_game_summary(page, game: SullyGnomeGameRef | Any, *, summary_range: str = "30", page_timeout: int = 30000) -> dict[str, Any]:
    slug = normalize_sullygnome_game_slug(getattr(game, "name", ""))
    url = build_sullygnome_game_url(slug, summary_range)
    if not slug or not url:
        return empty_sullygnome_summary_row(game, slug=slug, url=url, status="skipped", note="缺少可用于 SullyGnome 的游戏名。")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=page_timeout)
        try:
            page.wait_for_load_state("networkidle", timeout=min(10000, max(1000, int(page_timeout / 2))))
        except PlaywrightTimeoutError:
            pass
        text = page.locator("body").inner_text(timeout=5000)
        if "Page not found" in text or "404" in text[:500]:
            return empty_sullygnome_summary_row(game, slug=slug, url=url, status="not_found", note="SullyGnome 页面未找到。")
        row = parse_sullygnome_summary_from_text(text, game, slug=slug, url=url, page_url=page.url)
        twitch_url, chart_links = _extract_links(page)
        row["Twitch目录链接"] = twitch_url
        row["图表链接"] = chart_links
        if "Please do not scrape this site" in text:
            row["备注"] = "SullyGnome 第三方公开页面，已按低频可见页面方式补采。"
        return row
    except Exception as exc:
        return empty_sullygnome_summary_row(game, slug=slug, url=url, status="error", note=str(exc)[:500])


def _absolute_url(href: str) -> str:
    text = str(href or "")
    if not text:
        return ""
    if text.startswith("http"):
        return text
    if text.startswith("/"):
        return f"{BASE_URL}{text}"
    return f"{BASE_URL}/{text}"


def _visible_table_rows(page) -> list[dict[str, str]]:
    try:
        return page.evaluate(
            """
            () => {
                const tables = Array.from(document.querySelectorAll('table'));
                const out = [];
                for (const table of tables) {
                    const rows = Array.from(table.querySelectorAll('tbody tr'));
                    for (const tr of rows) {
                        const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || td.textContent || '').trim()).filter(Boolean);
                        if (!cells.length) continue;
                        const link = tr.querySelector('a[href]');
                        out.push({
                            name: link ? (link.innerText || link.textContent || '').trim() : (cells[1] || cells[0] || ''),
                            href: link ? (link.getAttribute('href') || '') : '',
                            text: cells.join(' | ')
                        });
                    }
                }
                return out;
            }
            """
        ) or []
    except Exception:
        return []


def collect_sullygnome_visible_table_rows(
    page,
    game: SullyGnomeGameRef | Any,
    *,
    table_type: str = "watched",
    summary_range: str = "30",
    limit: int = 25,
    max_scrolls: int = 2,
    page_timeout: int = 30000,
    stop_event=None,
    pause_event=None,
) -> list[dict[str, Any]]:
    slug = normalize_sullygnome_game_slug(getattr(game, "name", ""))
    url = build_sullygnome_game_table_url(slug, table_type, summary_range)
    if not slug or not url:
        return [
            build_sullygnome_visible_table_row(
                game,
                slug=slug,
                table_type=table_type,
                rank=1,
                status="skipped",
                note="缺少可用于 SullyGnome 的游戏名。",
            )
        ]
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=page_timeout)
        try:
            page.wait_for_load_state("networkidle", timeout=min(10000, max(1000, int(page_timeout / 2))))
        except PlaywrightTimeoutError:
            pass
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for scroll_index in range(max(0, int(max_scrolls or 0)) + 1):
            if wait_if_paused(pause_event, stop_event) or should_stop(stop_event):
                break
            try:
                page.wait_for_selector("table tbody tr", timeout=5000 if scroll_index == 0 else 1500)
            except PlaywrightTimeoutError:
                pass
            for item in _visible_table_rows(page):
                key = _clean_text(item.get("text"))
                if not key or key in seen:
                    continue
                seen.add(key)
                rows.append(item)
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
            if scroll_index < max_scrolls:
                try:
                    page.mouse.wheel(0, 900)
                except Exception:
                    try:
                        page.evaluate("window.scrollBy(0, 900)")
                    except Exception:
                        pass
                if interruptible_sleep(1.0, stop_event, pause_event=pause_event):
                    break
        if not rows:
            return [
                build_sullygnome_visible_table_row(
                    game,
                    slug=slug,
                    table_type=table_type,
                    rank=1,
                    status="empty",
                    note="未在当前动态窗口中识别到可见表格行。",
                )
            ]
        result: list[dict[str, Any]] = []
        for index, item in enumerate(rows[:limit], start=1):
            result.append(
                build_sullygnome_visible_table_row(
                    game,
                    slug=slug,
                    table_type=table_type,
                    rank=index,
                    entity_name=_clean_text(item.get("name")),
                    entity_url=_absolute_url(item.get("href", "")),
                    metrics_text=_clean_text(item.get("text")),
                    status="ok",
                    note="仅采当前动态窗口可见表格，未做全量翻页。",
                )
            )
        return result
    except Exception as exc:
        return [
            build_sullygnome_visible_table_row(
                game,
                slug=slug,
                table_type=table_type,
                rank=1,
                status="error",
                note=str(exc)[:500],
            )
        ]


def collect_sullygnome_for_games(
    games: list[Any],
    *,
    cdp_url: str | None = None,
    browser: str | None = "Chrome",
    summary_range: str = "30",
    collect_visible_tables: bool = True,
    visible_table_limit: int = 25,
    max_scrolls: int = 2,
    request_delay: float = 5.0,
    page_timeout: int = 30000,
    log_callback=None,
    stop_event=None,
    pause_event=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summary_rows: list[dict[str, Any]] = []
    table_rows: list[dict[str, Any]] = []
    if not games:
        return summary_rows, table_rows
    try:
        with sync_playwright() as playwright:
            resolved_cdp_url = cdp_url or cdp_url_for_browser(browser)
            _, context = connect_existing_chromium(playwright, resolved_cdp_url, log_callback=log_callback, browser=browser)
            page = context.new_page()
            try:
                for index, game in enumerate(games, start=1):
                    if wait_if_paused(pause_event, stop_event) or should_stop(stop_event):
                        break
                    if getattr(game, "status", "ok") != "ok" or not getattr(game, "name", ""):
                        continue
                    log_line(log_callback, f"[{index}/{len(games)}] SullyGnome 补采：{getattr(game, 'name', '')}")
                    summary_rows.append(
                        collect_sullygnome_game_summary(page, game, summary_range=summary_range, page_timeout=page_timeout)
                    )
                    if collect_visible_tables:
                        table_rows.extend(
                            collect_sullygnome_visible_table_rows(
                                page,
                                game,
                                table_type="watched",
                                summary_range=summary_range,
                                limit=max(1, int(visible_table_limit or 1)),
                                max_scrolls=max(0, int(max_scrolls or 0)),
                                page_timeout=page_timeout,
                                stop_event=stop_event,
                                pause_event=pause_event,
                            )
                        )
                    if index < len(games) and request_delay > 0:
                        if interruptible_sleep(float(request_delay), stop_event, pause_event=pause_event):
                            break
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    except Exception as exc:
        log_warn(log_callback, f"SullyGnome 动态窗口补采失败：{exc}")
    return summary_rows, table_rows
