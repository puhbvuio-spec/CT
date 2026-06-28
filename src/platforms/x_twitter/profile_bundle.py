from __future__ import annotations

import re
import time
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # pragma: no cover
    sync_playwright = None

from src.core import (
    DEFAULT_X_CDP_URL,
    build_output_path,
    connect_existing_chromium,
    log_error,
    log_line,
    log_warn,
    should_stop,
    wait_if_paused,
)
from src.core.task_checkpoint import open_checkpointed_multi_sheet_writer, open_task_checkpoint
from src.platforms.x_twitter.profile_tweets import (
    DEFAULT_MAX_SCROLLS,
    DEFAULT_PROFILE_TWEET_LIMIT,
    INITIAL_LOAD_DELAY,
    NO_NEW_SCROLL_LIMIT,
    PAGE_LOAD_TIMEOUT,
    SCROLL_DELAY,
    SCROLL_PX,
    collect_profile_tweets,
    extract_profile_username,
    navigate_to_profile_via_search,
    parse_profile_urls,
    row_from_tweet,
)
from src.platforms.x_twitter.profiles import extract_profile_record, normalize_x_url


PROFILE_FIELDS = ["作者主页链接", "作者名称", "作者ID", "粉丝量", "作者简介"]
TWEET_FIELDS = [
    "序号",
    "编号",
    "推文链接",
    "作品链接",
    "博主主页链接",
    "作者主页链接",
    "标题",
    "作品内容",
    "频道名称",
    "发布日期",
    "作品类型",
    "直播状态",
    "关联作品标题",
    "关联作品链接",
    "作品时长",
    "作品简介",
    "浏览量",
    "播放量",
    "点赞数",
    "评论数",
]


def _parse_date_range(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期。")
    return start_dt, end_dt


def _cell_text(value: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit is not None and len(text) > limit:
        return text[:limit].rstrip()
    return text


def _fallback_profile_record(profile_url: str) -> dict[str, str]:
    normalized_url = normalize_x_url(profile_url)
    username = extract_profile_username(normalized_url)
    return {
        "作者主页链接": normalized_url,
        "作者的名称": "",
        "账号ID": username,
        "粉丝数": "",
        "简介": "",
    }


def build_profile_row(profile_record: dict[str, str]) -> dict[str, str]:
    return {
        "作者主页链接": profile_record.get("作者主页链接", ""),
        "作者名称": profile_record.get("作者的名称", ""),
        "作者ID": profile_record.get("账号ID", ""),
        "粉丝量": profile_record.get("粉丝数", ""),
        "作者简介": profile_record.get("简介", ""),
    }


def build_tweet_row(index: int, profile_record: dict[str, str], tweet: dict[str, str]) -> dict[str, str]:
    base = row_from_tweet(index, tweet)
    tweet_url = base.get("帖子链接", "")
    content = _cell_text(base.get("帖子内容", ""))
    return {
        "序号": base.get("序号", str(index)),
        "编号": base.get("帖子ID", ""),
        "推文链接": tweet_url,
        "作品链接": tweet_url,
        "博主主页链接": profile_record.get("作者主页链接", ""),
        "作者主页链接": profile_record.get("作者主页链接", ""),
        "标题": _cell_text(content, limit=120),
        "作品内容": f"{content}[推文]" if content else "",
        "频道名称": profile_record.get("作者的名称", ""),
        "发布日期": base.get("发布时间", ""),
        "作品类型": "推文",
        "直播状态": "非直播",
        "关联作品标题": "",
        "关联作品链接": "",
        "作品时长": "",
        "作品简介": content,
        "浏览量": base.get("浏览量", ""),
        "播放量": "",
        "点赞数": base.get("点赞量", ""),
        "评论数": base.get("评论数", ""),
    }


def run_x_profile_bundle_spider(
    profile_urls_text: str,
    limit_time_str: str,
    start_date: str,
    end_date: str,
    cdp_port_or_url: str = DEFAULT_X_CDP_URL,
    log_callback=None,
    finish_callback=None,
    stop_event=None,
    config=None,
    pause_event=None,
):
    if config is None:
        config = {}
    completed_path = None
    page = None

    try:
        if sync_playwright is None:
            log_error(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        profile_urls = parse_profile_urls(profile_urls_text)
        if not profile_urls:
            log_warn(log_callback, "未读取到有效的 X 博主主页链接。")
            return

        page_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
        scroll_delay = float(config.get("scroll_interval", SCROLL_DELAY))
        no_new_scroll_limit = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
        max_scrolls = int(config.get("max_scrolls", DEFAULT_MAX_SCROLLS))
        max_tweets_per_author = max(1, int(config.get("max_tweets_per_author", DEFAULT_PROFILE_TWEET_LIMIT)))
        scroll_px = int(config.get("scroll_px", SCROLL_PX))
        initial_load_delay = float(config.get("initial_load_delay", INITIAL_LOAD_DELAY))
        date_window_size = int(config.get("date_window_size", 20))
        include_reposts = str(config.get("include_reposts", "否")).strip() == "是"
        browser_choice = config.get("browser")

        requested_time_limit = limit_time_str == "是"
        limit_time_bool = False
        start_dt = end_dt = None
        if requested_time_limit:
            log_line(log_callback, "主页推文采集采用最新数量优先，已忽略时间窗口过滤。")
        checkpoint = open_task_checkpoint(
            "x_profile_bundle",
            {
                "profile_urls": profile_urls,
                "max_tweets_per_author": max_tweets_per_author,
                "max_scrolls": max_scrolls,
                "include_reposts": include_reposts,
            },
            log_callback=log_callback,
        )

        default_output_path = build_output_path(
            "x",
            f"x_profile_bundle_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
            channel="profile_bundle",
        )
        output_path, writer = open_checkpointed_multi_sheet_writer(
            checkpoint,
            default_output_path,
            {
                "博主信息": PROFILE_FIELDS,
                "博主对应推文": TWEET_FIELDS,
            },
            log_callback=log_callback,
            autosave_every=10,
        )
        checkpoint.add_output_path(output_path)

        with sync_playwright() as playwright:
            log_line(log_callback, "正在连接本地浏览器...")
            try:
                _, context = connect_existing_chromium(playwright, cdp_port_or_url, browser=browser_choice)
            except Exception as exc:
                log_error(log_callback, f"无法连接浏览器：{exc}")
                log_error(log_callback, "连接失败：请确认浏览器已自动打开并已登录 X/Twitter。")
                return

            page = context.new_page()
            tweet_index = 0
            total_profiles = len(profile_urls)

            for profile_index, profile_url in enumerate(profile_urls, 1):
                if should_stop(stop_event):
                    log_line(log_callback, "任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break

                profile_url = normalize_x_url(profile_url)
                if checkpoint.is_completed(profile_url):
                    log_line(log_callback, f"[{profile_index}/{total_profiles}] 断点续跑跳过已完成博主：{profile_url}")
                    continue
                log_line(log_callback, f"[{profile_index}/{total_profiles}] 采集博主信息与推文：{profile_url}")

                profile_ready = False
                try:
                    profile_ready = navigate_to_profile_via_search(
                        page,
                        profile_url,
                        log_callback,
                        page_timeout=page_timeout,
                        stop_event=stop_event,
                        pause_event=pause_event,
                        initial_delay=initial_load_delay,
                    )
                    if not profile_ready:
                        log_warn(log_callback, f"  未能通过搜索页进入作者主页，使用链接兜底：{profile_url}")
                        profile_record = _fallback_profile_record(profile_url)
                    else:
                        profile_record = extract_profile_record(
                            page,
                            profile_url,
                            log_callback,
                            page_timeout=page_timeout,
                            stop_event=stop_event,
                            needs_navigation=False,
                        ) or _fallback_profile_record(profile_url)
                except Exception as exc:
                    log_warn(log_callback, f"  博主信息采集失败，使用链接兜底：{exc}")
                    profile_record = _fallback_profile_record(profile_url)

                writer.writerow("博主信息", build_profile_row(profile_record))

                try:
                    if not profile_ready:
                        raise RuntimeError("未能通过搜索页进入作者主页")
                    tweets = collect_profile_tweets(
                        page,
                        None,
                        profile_record.get("作者主页链接") or profile_url,
                        max_scrolls,
                        limit_time_bool,
                        start_dt,
                        end_dt,
                        False,
                        0,
                        log_callback,
                        stop_event=stop_event,
                        writer=None,
                        page_timeout=page_timeout,
                        scroll_delay=scroll_delay,
                        no_new_scroll_limit=no_new_scroll_limit,
                        pause_event=pause_event,
                        max_collect=max_tweets_per_author,
                        scroll_px=scroll_px,
                        initial_load_delay=initial_load_delay,
                        page_already_loaded=True,
                        date_window_size=date_window_size,
                        include_reposts=include_reposts,
                    )
                except Exception as exc:
                    log_warn(log_callback, f"  推文采集失败：{exc}")
                    tweets = []

                for tweet in tweets:
                    tweet_index += 1
                    writer.writerow("博主对应推文", build_tweet_row(tweet_index, profile_record, tweet))
                writer.save()
                log_line(
                    log_callback,
                    f"  完成：{profile_record.get('账号ID') or profile_url}，写入 {len(tweets)} 条推文。",
                )
                checkpoint.mark_completed(
                    profile_url,
                    {"output_path": output_path, "profile_index": profile_index, "tweet_count": len(tweets)},
                )

        completed_path = output_path
        writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
    finally:
        try:
            if page is not None and not page.is_closed():
                page.close()
        except Exception:
            pass
        if finish_callback:
            finish_callback(completed_path)
