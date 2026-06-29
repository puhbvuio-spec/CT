from __future__ import annotations

import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # pragma: no cover
    sync_playwright = None

from src.core import (
    build_output_path,
    connect_existing_chromium,
    ensure_chrome_for_cdp,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    should_stop,
    wait_if_paused,
)
from src.core.task_checkpoint import open_checkpointed_row_writer, open_task_checkpoint
from src.platforms.tiktok.keyword import (
    MAX_SEARCH_SCROLLS,
    SEARCH_SCROLL_PAUSE,
    _click_tiktok_retry,
    _detect_tiktok_search_error,
    _retry_backoff_seconds,
    collect_visible_video_items,
    derive_publish_time_from_video_url,
    dynamic_search_scroll_limit,
    extract_video_row,
    in_date_range,
    parse_date_range,
    trigger_search_lazy_load,
)
from src.platforms.tiktok.keyword_author_works import (
    QUICK_PROFILE_WORK_LIMIT,
    TikTokAuthorSeed,
    _author_key,
    build_author_row,
    merge_seed_author,
    quick_mode_enabled,
    resolve_profile_work_limit,
)
from src.platforms.tiktok.profile_videos import (
    DEFAULT_MAX_SCROLLS,
    DETAIL_DELAY_MAX_SECONDS,
    DETAIL_DELAY_MIN_SECONDS,
    DETAIL_LOAD_TIMEOUT,
    NO_NEW_SCROLL_LIMIT,
    PAGE_LOAD_TIMEOUT,
    SCROLL_INTERVAL_SECONDS,
    SCROLL_PX,
    collect_profile_video_details,
)
from src.platforms.tiktok.profiles import extract_profile_row


CSV_FIELDS = [
    "话题",
    "命中作品数",
    "命中作品链接列表",
    "作者主页链接",
    "作者名称",
    "作者ID",
    "粉丝数",
    "作者简介",
    "采集作品数",
    "时间窗口内作品数",
    "作品标题列表",
    "作品链接列表",
    "作品发布时间列表",
]


@dataclass(frozen=True)
class HashtagSource:
    label: str
    url: str


def ensure_playwright_available() -> None:
    if sync_playwright is None:
        raise ModuleNotFoundError("playwright is required for TikTok hashtag author works scraping")


def normalize_hashtag_input(value: str) -> HashtagSource:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("话题不能为空。")

    if re.match(r"^https?://", raw, re.I):
        parsed = urllib.parse.urlparse(raw)
        match = re.search(r"/tag/([^/?#]+)", parsed.path or "", re.I)
        if not match:
            raise ValueError(f"不是有效的 TikTok 话题页链接：{raw}")
        tag = urllib.parse.unquote(match.group(1)).strip()
    else:
        tag = raw.lstrip("#").strip()
        if tag.startswith("tag/"):
            tag = tag[4:].strip()
    if not tag:
        raise ValueError(f"无法识别话题：{value}")

    encoded = urllib.parse.quote(tag, safe="")
    return HashtagSource(label=f"#{tag}", url=f"https://www.tiktok.com/tag/{encoded}")


def parse_hashtag_sources(
    values: list[str] | tuple[str, ...] | str,
    *,
    skip_invalid: bool = False,
    log_callback=None,
) -> list[HashtagSource]:
    lines = values.splitlines() if isinstance(values, str) else list(values or [])
    sources: list[HashtagSource] = []
    seen: set[str] = set()
    for line in lines:
        if not str(line).strip():
            continue
        try:
            source = normalize_hashtag_input(str(line))
        except ValueError as exc:
            if skip_invalid:
                log_warn(log_callback, f"跳过无效话题输入：{line}（{exc}）")
                continue
            raise
        key = source.url.lower()
        if key in seen:
            continue
        seen.add(key)
        sources.append(source)
    return sources


def _author_row_for_hashtag(
    seed: TikTokAuthorSeed,
    profile_record: dict[str, str],
    works: list[dict[str, str]],
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> dict[str, str]:
    row = build_author_row(seed, profile_record, works, limit_time_bool, start_dt, end_dt)
    row["话题"] = row.pop("搜索词", "")
    return {field: row.get(field, "") for field in CSV_FIELDS}


def _distributed_limit(total: int, bucket_count: int, bucket_index: int) -> int:
    total = max(1, int(total or 1))
    bucket_count = max(1, int(bucket_count or 1))
    bucket_index = max(1, int(bucket_index or 1))
    base = total // bucket_count
    remainder = total % bucket_count
    return max(1, base + (1 if bucket_index <= remainder else 0))


def open_hashtag_page(page, source: HashtagSource, stop_event=None, log_callback=None, page_timeout: int = PAGE_LOAD_TIMEOUT, max_attempts: int = 5) -> bool:
    for attempt in range(1, max_attempts + 1):
        if should_stop(stop_event):
            return False
        try:
            if attempt >= 3:
                page.reload(wait_until="domcontentloaded", timeout=page_timeout)
            else:
                page.goto(source.url, wait_until="domcontentloaded", timeout=page_timeout)
        except Exception as exc:
            log_line(log_callback, f"  话题页导航失败（第 {attempt}/{max_attempts} 次）：{exc}")
            interruptible_sleep(_retry_backoff_seconds(attempt), stop_event)
            continue

        interruptible_sleep(random.uniform(1.8, 2.8), stop_event)
        try:
            page.wait_for_selector("a[href*='/video/'], a[href*='video/']", timeout=8000)
        except Exception:
            pass

        error_text = _detect_tiktok_search_error(page)
        if error_text is None:
            return True

        log_line(log_callback, f"  话题页出现错误态「{error_text}」（第 {attempt}/{max_attempts} 次），尝试重试...")
        cooldown = random.uniform(5.0, 9.0) if attempt <= 2 else _retry_backoff_seconds(attempt)
        interruptible_sleep(cooldown, stop_event)
        if _click_tiktok_retry(page):
            interruptible_sleep(random.uniform(4.0, 6.0), stop_event)
            if _detect_tiktok_search_error(page) is None:
                log_line(log_callback, "  点击重试后话题页已恢复。")
                return True
        interruptible_sleep(_retry_backoff_seconds(attempt), stop_event)

    log_line(log_callback, f"  话题页重试 {max_attempts} 次仍处于错误态，继续尝试采集：{source.url}")
    return False


def collect_hashtag_seed_authors(
    topic_page,
    detail_page,
    sources: list[HashtagSource],
    start_dt: datetime | None,
    end_dt: datetime | None,
    limit_time_bool: bool,
    log_callback,
    stop_event=None,
    pause_event=None,
    max_seed_works: int = 300,
    max_authors: int = 100,
    max_topic_scrolls: int = MAX_SEARCH_SCROLLS,
    topic_scroll_pause: float = SEARCH_SCROLL_PAUSE,
    no_new_scroll_limit: int = 12,
    page_timeout: int = PAGE_LOAD_TIMEOUT,
) -> dict[str, TikTokAuthorSeed]:
    authors: dict[str, TikTokAuthorSeed] = {}
    seen_links: set[str] = set()
    inspected_count = 0

    for source_index, source in enumerate(sources, 1):
        if inspected_count >= max_seed_works or should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        log_line(log_callback, f"[{source_index}/{len(sources)}] 打开话题页：{source.label} {source.url}")
        if not open_hashtag_page(topic_page, source, stop_event=stop_event, log_callback=log_callback, page_timeout=page_timeout):
            log_warn(log_callback, f"跳过话题：页面无法正常打开或持续错误：{source.label}")
            continue
        source_seed_limit = _distributed_limit(max_seed_works, len(sources), source_index)
        source_author_limit = _distributed_limit(max_authors, len(sources), source_index)
        scroll_limit = dynamic_search_scroll_limit(source_seed_limit, max_topic_scrolls)
        source_inspected_count = 0
        source_new_author_count = 0
        log_line(
            log_callback,
            f"  本话题采样配额：最多检查 {source_seed_limit} 个命中作品，最多新增 {source_author_limit} 个作者。",
        )
        no_new_rounds = 0
        source_seen_count = 0

        for scroll_index in range(scroll_limit):
            if (
                inspected_count >= max_seed_works
                or len(authors) >= max_authors
                or source_inspected_count >= source_seed_limit
                or source_new_author_count >= source_author_limit
                or should_stop(stop_event)
            ):
                break
            if wait_if_paused(pause_event, stop_event):
                break

            new_items = collect_visible_video_items(topic_page, seen_links)
            source_seen_count += len(new_items)
            if not new_items:
                no_new_rounds += 1
            else:
                no_new_rounds = 0

            for item in new_items:
                if (
                    inspected_count >= max_seed_works
                    or len(authors) >= max_authors
                    or source_inspected_count >= source_seed_limit
                    or source_new_author_count >= source_author_limit
                    or should_stop(stop_event)
                ):
                    break
                video_url = item.get("视频链接", "")
                if not video_url:
                    continue
                try:
                    derived_publish_time = derive_publish_time_from_video_url(video_url)
                    if limit_time_bool and start_dt and end_dt and derived_publish_time and not in_date_range(derived_publish_time, start_dt, end_dt):
                        inspected_count += 1
                        source_inspected_count += 1
                        log_line(log_callback, f"  跳过种子：视频 ID 时间不在范围内（{derived_publish_time}）")
                        continue

                    row = extract_video_row(
                        detail_page,
                        source.label,
                        video_url,
                        item.get("播放量", ""),
                        profile_url=item.get("博主主页链接", ""),
                        stop_event=stop_event,
                    )
                    inspected_count += 1
                    source_inspected_count += 1
                    if limit_time_bool and start_dt and end_dt and not in_date_range(row.get("发布时间", ""), start_dt, end_dt):
                        log_line(log_callback, f"  跳过种子：发布时间不在范围内（{row.get('发布时间') or '未解析'}）")
                        continue
                    author_key = _author_key(row.get("博主主页链接", ""), row.get("博主ID", ""))
                    is_new_author = author_key not in authors
                    if is_new_author and (len(authors) >= max_authors or source_new_author_count >= source_author_limit):
                        continue
                    seed = merge_seed_author(authors, source.label, video_url, row)
                    if seed:
                        if is_new_author:
                            source_new_author_count += 1
                        log_line(log_callback, f"  发现作者种子 {inspected_count}/{max_seed_works}: {video_url}")
                except Exception as exc:
                    inspected_count += 1
                    source_inspected_count += 1
                    log_warn(log_callback, f"  跳过一个话题候选视频：{exc}")

            if no_new_rounds >= no_new_scroll_limit and scroll_index >= 5:
                break
            if (
                len(authors) >= max_authors
                or source_inspected_count >= source_seed_limit
                or source_new_author_count >= source_author_limit
            ):
                break
            trigger_search_lazy_load(topic_page)
            if interruptible_sleep(topic_scroll_pause, stop_event):
                break
        if source_seen_count == 0:
            log_warn(log_callback, f"跳过话题：未发现可采集视频，可能是话题不存在、无公开内容或页面未加载成功：{source.label}")

    return authors


def run_tiktok_hashtag_author_works_spider(
    hashtag_inputs,
    limit_time_str,
    start_date,
    end_date,
    cdp_port_or_url,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    ensure_playwright_available()
    if config is None:
        config = {}

    max_seed_works = int(config.get("max_seed_works", 300))
    max_authors = int(config.get("max_authors", 100))
    quick_mode_value = config.get("quick_mode", "是")
    max_profile_works = resolve_profile_work_limit(config, quick_mode_value)
    max_topic_scrolls = int(config.get("max_topic_scrolls", config.get("max_search_scrolls", MAX_SEARCH_SCROLLS)))
    max_profile_scrolls = int(config.get("max_profile_scrolls", config.get("max_scrolls", DEFAULT_MAX_SCROLLS)))
    page_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    topic_scroll_pause = float(config.get("scroll_interval", SEARCH_SCROLL_PAUSE))
    profile_scroll_interval = float(config.get("profile_scroll_interval", SCROLL_INTERVAL_SECONDS))
    no_new_scroll_limit = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    scroll_px = int(config.get("scroll_px", SCROLL_PX))
    detail_load_timeout = int(config.get("detail_load_timeout", DETAIL_LOAD_TIMEOUT))
    detail_delay_min = float(config.get("detail_delay_min", DETAIL_DELAY_MIN_SECONDS))
    detail_delay_max = float(config.get("detail_delay_max", DETAIL_DELAY_MAX_SECONDS))

    completed_path = None
    topic_page = seed_detail_page = profile_info_page = profile_page = works_detail_page = None
    try:
        sources = parse_hashtag_sources(hashtag_inputs, skip_invalid=True, log_callback=log_callback)
        if not sources:
            raise ValueError("至少需要输入一个有效的 TikTok 话题关键词。")

        limit_time_bool = limit_time_str == "是"
        start_dt = end_dt = None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)

        if quick_mode_enabled(quick_mode_value):
            log_line(log_callback, f"快速模式已开启：作者主页作品最多取最新 {QUICK_PROFILE_WORK_LIMIT} 条，不足则采完即停。")
        checkpoint = open_task_checkpoint(
            "tiktok_hashtag_author_works",
            {
                "hashtags": [source.url for source in sources],
                "limit_time": limit_time_bool,
                "start_date": start_date if limit_time_bool else "",
                "end_date": end_date if limit_time_bool else "",
                "quick_mode": quick_mode_value,
                "max_profile_works": max_profile_works,
            },
            log_callback=log_callback,
            merge_on_keys=("hashtags",),
        )
        ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)
        with sync_playwright() as playwright:
            _, context = connect_existing_chromium(playwright, cdp_port_or_url)
            topic_page = context.new_page()
            seed_detail_page = context.new_page()
            profile_info_page = context.new_page()
            profile_page = context.new_page()
            works_detail_page = context.new_page()

            authors = collect_hashtag_seed_authors(
                topic_page,
                seed_detail_page,
                sources,
                start_dt,
                end_dt,
                limit_time_bool,
                log_callback,
                stop_event=stop_event,
                pause_event=pause_event,
                max_seed_works=max_seed_works,
                max_authors=max_authors,
                max_topic_scrolls=max_topic_scrolls,
                topic_scroll_pause=topic_scroll_pause,
                no_new_scroll_limit=no_new_scroll_limit,
                page_timeout=page_timeout,
            )
            if not authors:
                log_warn(log_callback, "没有从话题页中发现有效作者。")
                return

            default_output_path = build_output_path(
                "tiktok",
                f"tiktok_hashtag_author_works_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
                channel="hashtag_author_works",
            )
            output_path, writer = open_checkpointed_row_writer(
                checkpoint,
                default_output_path,
                CSV_FIELDS,
                log_callback=log_callback,
                autosave_every=5,
            )
            checkpoint.add_output_path(output_path)

            total_authors = min(len(authors), max_authors)
            for index, seed in enumerate(list(authors.values())[:max_authors], 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                claimed, claim_status = checkpoint.claim_item(seed.profile_url, positive_count_fields=("works_count",))
                if not claimed:
                    if claim_status == "active":
                        log_line(log_callback, f"[{index}/{total_authors}] 双开分流跳过正在处理的作者：{seed.profile_url}")
                    else:
                        log_line(log_callback, f"[{index}/{total_authors}] 断点续跑跳过已完成作者：{seed.profile_url}")
                    continue
                log_line(log_callback, f"[{index}/{total_authors}] 进入博主主页：{seed.profile_url}")
                profile_record_ok = False
                try:
                    profile_record = extract_profile_row(
                        profile_info_page,
                        seed.profile_url,
                        page_load_timeout=page_timeout,
                        captcha_wait=8,
                        stop_event=stop_event,
                    )
                    profile_record_ok = True
                except Exception as exc:
                    log_warn(log_callback, f"  博主信息补充失败：{exc}")
                    profile_record = {
                        "博主主页链接": seed.profile_url,
                        "博主名称": seed.author_name,
                        "博主ID": seed.author_id,
                        "粉丝量": seed.followers,
                        "作者简介": seed.bio,
                    }

                works_collected_ok = False
                try:
                    works = collect_profile_video_details(
                        profile_page,
                        works_detail_page,
                        seed.profile_url,
                        None,
                        None,
                        False,
                        log_callback,
                        stop_event=stop_event,
                        pause_event=pause_event,
                        max_scrolls=max_profile_scrolls,
                        max_collect=max_profile_works,
                        page_load_timeout=page_timeout,
                        scroll_interval=profile_scroll_interval,
                        no_new_scroll_limit=no_new_scroll_limit,
                        scroll_px=scroll_px,
                        detail_load_timeout=detail_load_timeout,
                        detail_delay_min=detail_delay_min,
                        detail_delay_max=detail_delay_max,
                    )
                    works_collected_ok = True
                except Exception as exc:
                    log_warn(log_callback, f"  博主作品采集失败：{exc}")
                    works = []

                writer.writerow(_author_row_for_hashtag(seed, profile_record, works, limit_time_bool, start_dt, end_dt))
                if profile_record_ok and works_collected_ok and len(works) > 0:
                    checkpoint.mark_completed(
                        seed.profile_url,
                        {"output_path": output_path, "index": index, "works_count": len(works)},
                    )
                else:
                    checkpoint.release_item(seed.profile_url)
                    if works_collected_ok and len(works) == 0:
                        log_warn(log_callback, "  未采到主页作品，未写入断点完成标记，下次会继续重试。")
                    else:
                        log_warn(log_callback, "  本轮未完整采集成功，未写入断点完成标记，下次会继续重试。")
                log_line(log_callback, f"  写入博主：{profile_record.get('博主ID') or seed.author_id or seed.profile_url}，作品 {len(works)} 条。")

            writer.save()
            completed_path = output_path
            log_line(log_callback, f"完成，已保存：{output_path}")
    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
    finally:
        for page in (topic_page, seed_detail_page, profile_info_page, profile_page, works_detail_page):
            try:
                if page is not None and not page.is_closed():
                    page.close()
            except Exception:
                pass
        finish_callback(completed_path)
