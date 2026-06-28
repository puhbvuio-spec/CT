# -*- coding: utf-8 -*-
"""YouTube 关键词搜索采集 Pro 模块。

在原有关键词搜索基础上扩展以下能力：
1. 视频字段增加评论数、作者粉丝数、简介、视频类型
2. 采用 HEAD 请求方式探测长短视频类型
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from src.core import MultiSheetXlsxWriter, XlsxRowWriter, build_output_path, interruptible_sleep, log_error, log_line, log_warn, sanitize_csv_rows, should_stop, wait_if_paused

# 复用 keyword.py 中的通用组件
from src.platforms.youtube.keyword import (
    YouTubeClientPool,
    _api_call_with_rotation,
    chunked,
    collect_video_ids_with_playwright,
    format_youtube_duration,
    iter_search_video_id_batches,
    language_matches_snippet,
    parse_language_filter,
    parse_date_range,
    safe_filename_part,
)
from src.platforms.youtube.comments import format_youtube_datetime
from src.platforms.youtube.video_type import check_video_type_bulk

CSV_FIELDS_PRO = [
    "搜索词",
    "序号",
    "视频标题",
    "视频类型",       # 新增
    "视频时长",
    "播放量",
    "点赞数",
    "评论数",         # 新增
    "作者粉丝数",     # 新增
    "视频简介",       # 新增
    "发布时间",
    "视频链接",
    "作者主页链接",
    "查询时间",
]


def fetch_video_rows_pro(client_pool, keyword: str, video_ids: list[str], stop_event=None, pause_event=None, batch_size: int = 50, log_callback=None, target_languages: set[str] | None = None) -> list[dict]:
    """批量获取指定视频 ID 的详情指标（包含评论数、简介等），封装为导出格式。"""
    import googleapiclient.errors

    rows: list[dict] = []
    for ids in chunked(video_ids, batch_size):
        if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
            break

        try:
            response = _api_call_with_rotation(
                client_pool,
                lambda ids=ids: client_pool.client.videos().list(
                    part="snippet,contentDetails,statistics",
                    id=",".join(ids),
                    maxResults=batch_size,
                    fields="items(id,snippet(title,channelId,publishedAt,description,defaultAudioLanguage,defaultLanguage),contentDetails(duration),statistics(viewCount,likeCount,commentCount))"
                ),
                log_callback,
                stop_event,
            )
        except googleapiclient.errors.HttpError as e:
            if e.resp.status in [403, 429]:
                log_warn(log_callback, f"API 配额耗尽或受限: {e}")
                break  # 所有 Key 配额耗尽，跳出循环
            raise

        batch_before = len(response.get("items", []))
        batch_kept = 0
        missing_language_count = 0
        mismatch_language_count = 0
        for item in response.get("items", []):
            if should_stop(stop_event):
                break
            snippet = item.get("snippet", {})
            language_ok, language_reason = language_matches_snippet(snippet, target_languages)
            if not language_ok:
                if language_reason == "missing":
                    missing_language_count += 1
                else:
                    mismatch_language_count += 1
                continue
            stats = item.get("statistics", {})
            content_details = item.get("contentDetails", {})
            video_id = item.get("id", "")
            channel_id = snippet.get("channelId", "")
            batch_kept += 1

            # 简介：不截断，但处理换行符以适应 CSV/Excel
            description = (snippet.get("description") or "").replace("\n", " | ").replace("\r", "")

            row = {
                "搜索词": keyword,
                "序号": "",
                "视频标题": snippet.get("title", ""),
                "视频类型": "未知",  # 默认占位，后续探测
                "视频时长": format_youtube_duration(content_details.get("duration", "")),
                "播放量": stats.get("viewCount", ""),
                "点赞数": stats.get("likeCount", ""),
                "评论数": stats.get("commentCount", ""),
                "作者粉丝数": "",    # 默认占位，后续探测
                "视频简介": description,
                "发布时间": format_youtube_datetime(snippet.get("publishedAt", "")),
                "视频链接": f"https://www.youtube.com/watch?v={video_id}",
                "作者主页链接": f"https://www.youtube.com/channel/{channel_id}" if channel_id else "",
                "查询时间": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }
            # 记录原始 channel_id 以便后续获取粉丝数
            row["_channel_id"] = channel_id
            row["_video_id"] = video_id
            rows.append(row)
        if target_languages:
            filtered = batch_before - batch_kept
            log_line(
                log_callback,
                f"  语种过滤：本批 {batch_before} -> {batch_kept}，过滤 {filtered}（无语言 {missing_language_count}，不匹配 {mismatch_language_count}）",
            )
    return rows


def fetch_channel_subscribers_batch(client_pool, channel_ids: list[str], log_callback=None, stop_event=None, pause_event=None) -> dict[str, str]:
    """批量获取频道粉丝数。"""
    import googleapiclient.errors

    subscriber_map = {}
    unique_ids = list(set([cid for cid in channel_ids if cid]))
    
    if not unique_ids:
        return subscriber_map

    log_line(log_callback, f"  开始获取 {len(unique_ids)} 个频道的粉丝数...")
    
    for batch in chunked(unique_ids, 50):
        if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
            break

        try:
            response = _api_call_with_rotation(
                client_pool,
                lambda batch=batch: client_pool.client.channels().list(
                    part="statistics",
                    id=",".join(batch),
                    fields="items(id,statistics(subscriberCount))"
                ),
                log_callback, stop_event,
            )
            for item in response.get("items", []):
                cid = item.get("id", "")
                sub_count = item.get("statistics", {}).get("subscriberCount", "")
                subscriber_map[cid] = sub_count
        except googleapiclient.errors.HttpError as e:
            if e.resp.status in [403, 429]:
                log_warn(log_callback, "  获取粉丝数额度耗尽，提前结束该环节。")
                break
            log_warn(log_callback, f"  获取粉丝数失败：{e}")

    return subscriber_map


def run_youtube_keyword_pro(api_keys: list[str], keywords_list, max_results, limit_time_str, start_date, end_date, get_comments_str, max_comments, check_video_type_str, auto_snapshot_3d_str, auto_snapshot_7d_str, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    """运行 YouTube 关键词采集 Pro 主驱动函数。"""
    from src.platforms.youtube.comments import (
        COMMENT_MODE_FAST,
        DEFAULT_COMMENT_WORKERS,
        CommentFetchTask,
        fetch_top_comments_for_videos,
        normalize_comment_mode,
        normalize_comment_workers,
    )
    from src.platforms.youtube.snapshot_scheduler import process_due_jobs, register_job

    if config is None:
        config = {}
    search_batch_size = int(config.get("youtube_search_batch_size", 50))
    video_batch_size = int(config.get("youtube_video_batch_size", 50))
    comment_top_limit = int(config.get("comment_top_limit", 100))
    date_chunk_days = int(config.get("youtube_date_chunk_days", 7))
    date_chunk_hours = int(config.get("youtube_date_chunk_hours", 0))
    search_method = config.get("youtube_search_method", "浏览器优先（省配额）")
    use_browser = (search_method == "浏览器优先（省配额）")
    browser_scroll_px = int(config.get("youtube_browser_scroll_px", 2500))
    browser_scroll_delay = float(config.get("youtube_browser_scroll_delay", 1.0))
    browser_max_scrolls = int(config.get("youtube_browser_max_scrolls", 100))
    browser_page_timeout = int(config.get("youtube_browser_page_timeout", 45000))
    browser_no_new_limit = int(config.get("youtube_browser_no_new_limit", 8))
    enable_timer = config.get("enable_timer", "否") == "是"
    timer_interval_minutes = int(config.get("timer_interval_minutes", 60))
    timer_max_runs = int(config.get("timer_max_runs", 3))
    target_languages = parse_language_filter(config.get("youtube_language_filter", ""))
    relevance_language = next(iter(target_languages)) if len(target_languages) == 1 else ""
    comment_mode = normalize_comment_mode(config.get("youtube_comment_mode", COMMENT_MODE_FAST))
    comment_workers = normalize_comment_workers(config.get("youtube_comment_workers", DEFAULT_COMMENT_WORKERS))

    output_path = None
    output_paths: list[str] = []
    playwright_context = None
    browser = None
    try:
        limit_time_bool = limit_time_str == "是"
        get_comments_bool = get_comments_str == "是"
        check_video_type_bool = check_video_type_str == "是"
        
        target_days = []
        if auto_snapshot_3d_str == "是":
            target_days.append(3)
        if auto_snapshot_7d_str == "是":
            target_days.append(7)
            
        start_dt, end_dt = None, None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)

        if not keywords_list:
            log_warn(log_callback, "关键词列表为空，无任务可执行。")
            return

        if use_browser and limit_time_bool:
            use_browser = False
            log_line(log_callback, "  [模式切换] 启用了时间过滤，自动切换为 API 模式。")

        current_run = 0
        tz_local = timezone(timedelta(hours=8))
        # 用户输入的日期视为北京时间 (UTC+8) 的 0 点，而非 UTC 的 0 点
        if enable_timer and limit_time_bool and start_dt and end_dt:
            start_dt = start_dt.replace(tzinfo=tz_local)
            end_dt = end_dt.replace(tzinfo=tz_local)
        
        while True:
            # 每次循环开始前，执行一次到期快照扫描
            process_due_jobs(api_keys, log_callback, stop_event, pause_event)
            
            # 每轮循环重新初始化 API 客户端池，防止由于长时间休眠导致底层 TCP 连接被服务器静默掐断 (ConnectionResetError 10054)
            client_pool = YouTubeClientPool(api_keys)
            if target_languages:
                log_line(log_callback, f"  语种严格过滤已启用：{', '.join(sorted(target_languages))}")
            
            current_run += 1
            if enable_timer and limit_time_bool:
                now = datetime.now(tz_local)
                if current_run == 1:
                    end_dt = min(end_dt, now)
                else:
                    start_dt = end_dt
                    end_dt = now
            if enable_timer:
                log_line(log_callback, f"=== 开始执行第 {current_run} 次任务 ===")
                if limit_time_bool:
                    log_line(log_callback, f"  定时模式：本次时间范围 {start_dt.strftime('%Y-%m-%d %H:%M')} 至 {end_dt.strftime('%Y-%m-%d %H:%M')}")
            run_stamp = time.strftime("%Y%m%d_%H%M%S")
            output_paths.clear()

            for index, keyword in enumerate(keywords_list, 1):
                if should_stop(stop_event):
                    log_line(log_callback, "任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                output_path = build_output_path(
                    "youtube",
                    f"youtube_keyword_pro_{safe_filename_part(keyword)}_{run_stamp}.xlsx",
                    channel="keyword",
                )
                output_paths.append(output_path)

                if get_comments_bool:
                    comment_fields = ["序号", "视频链接", "评论的点赞量", "评论内容", "评论发布时间"]
                    writer = MultiSheetXlsxWriter(output_path, {"视频信息": CSV_FIELDS_PRO, "评论信息": comment_fields}, autosave_every=500)
                else:
                    writer = XlsxRowWriter(output_path, CSV_FIELDS_PRO, autosave_every=500)
                serial_number = 1
                log_line(log_callback, f"[{index}/{len(keywords_list)}] 搜索关键词：{keyword}")
                log_line(log_callback, f"  输出文件：{output_path}")

                all_video_ids = []

                if use_browser:
                    from playwright.sync_api import sync_playwright
                    from src.core import connect_existing_chromium, DEFAULT_X_CDP_URL
                    try:
                        if not playwright_context:
                            playwright_context = sync_playwright().start()
                        if not browser:
                            browser, _ = connect_existing_chromium(playwright_context, DEFAULT_X_CDP_URL, log_callback=log_callback)
                        log_line(log_callback, "  [浏览器优先] Chromium 已连接。")
                        
                        page = browser.new_page()
                        all_video_ids = collect_video_ids_with_playwright(
                            page, keyword, max_results,
                            start_dt=start_dt, end_dt=end_dt,
                            log_callback=log_callback, stop_event=stop_event, pause_event=pause_event,
                            scroll_px=browser_scroll_px, scroll_delay=browser_scroll_delay, max_scrolls=browser_max_scrolls,
                            page_timeout=browser_page_timeout, no_new_limit=browser_no_new_limit,
                        )
                        if not all_video_ids:
                            log_line(log_callback, "  [浏览器优先] 未获取到任何视频 ID，将 Fallback 到 API 模式。")
                    except Exception as e:
                        log_warn(log_callback, f"  [浏览器优先] 模式失败 ({e})，将 Fallback 到 API 模式。")
                        use_browser = False
                    finally:
                        if 'page' in locals() and page:
                            try:
                                page.close()
                            except Exception:
                                pass
                        # 关闭浏览器防止在定时模式下长连接断开
                        if browser:
                            try:
                                browser.close()
                            except Exception:
                                pass
                            browser = None

                if not all_video_ids:
                    log_line(log_callback, "  使用 API 搜索模式获取视频 ID 列表中...")
                    try:
                        for batch_ids in iter_search_video_id_batches(client_pool, keyword, max_results, limit_time_bool, start_dt, end_dt, log_callback, stop_event, pause_event, search_batch_size, date_chunk_days, date_chunk_hours, relevance_language):
                            all_video_ids.extend(batch_ids)
                            if len(all_video_ids) >= max_results:
                                break
                    except Exception as exc:
                        log_error(log_callback, f"  API 搜索失败: {exc}")

                written_count = 0
                log_line(log_callback, f"  共获取到 {len(all_video_ids)} 个视频 ID，开始获取详细信息...")

                for chunk_ids in chunked(all_video_ids, video_batch_size):
                    if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
                        break

                    rows = fetch_video_rows_pro(client_pool, keyword, chunk_ids, stop_event, pause_event, video_batch_size, log_callback, target_languages)

                    if written_count + len(rows) > max_results:
                        rows = rows[:max_results - written_count]

                    if not rows:
                        continue

                    # 并行1：收集该批次频道 ID，获取粉丝数
                    channel_ids = [row["_channel_id"] for row in rows if row.get("_channel_id")]
                    subscriber_map = fetch_channel_subscribers_batch(client_pool, channel_ids, log_callback, stop_event, pause_event)
                    
                    # 并行2：检测长短视频类型
                    video_type_map = {}
                    if check_video_type_bool:
                        log_line(log_callback, f"  检测这 {len(rows)} 个视频的类型（长视频 / Shorts）...")
                        batch_vids = [row["_video_id"] for row in rows if row.get("_video_id")]
                        video_type_map = check_video_type_bulk(batch_vids)

                    # 整合并写入数据
                    comment_results = {}
                    if get_comments_bool:
                        comment_tasks = [
                            CommentFetchTask(video_id=row["_video_id"], video_url=row["视频链接"])
                            for row in rows
                            if row.get("_video_id")
                        ]
                        comment_results = fetch_top_comments_for_videos(
                            api_keys,
                            comment_tasks,
                            max_comments,
                            comment_top_limit,
                            comment_mode,
                            comment_workers,
                            log_callback,
                            stop_event,
                            pause_event,
                        )

                    for row in rows:
                        row["序号"] = str(serial_number)
                        vid = row.pop("_video_id", "")
                        cid = row.pop("_channel_id", "")
                        
                        if cid in subscriber_map:
                            row["作者粉丝数"] = subscriber_map[cid]
                        if vid in video_type_map:
                            row["视频类型"] = video_type_map[vid]

                        if get_comments_bool:
                            try:
                                result = comment_results.get(vid)
                                if result and result.status == "error":
                                    log_warn(log_callback, f"    评论获取失败 ({vid})：{result.error}")
                                comments = result.comments if result else []
                                if not comments:
                                    writer.writerow("评论信息", {"序号": row["序号"], "视频链接": row["视频链接"], "评论内容": "无评论或被禁用"})
                                else:
                                    for comment in comments:
                                        comment_row = {
                                            "序号": row["序号"],
                                            "视频链接": row["视频链接"],
                                            "评论的点赞量": str(comment["like_count"]),
                                            "评论内容": comment["text"],
                                            "评论发布时间": comment.get("published_at", "")
                                        }
                                        writer.writerow("评论信息", comment_row)
                            except Exception as exc:
                                if "commentsDisabled" in str(exc) or "403" in str(exc):
                                    log_warn(log_callback, f"    该视频 ({vid}) 评论已被禁用或无法访问。")
                                else:
                                    log_warn(log_callback, f"    提取评论失败 ({vid})：{exc}")
                                writer.writerow("评论信息", {"序号": row["序号"], "视频链接": row["视频链接"], "评论内容": "获取失败"})

                        serial_number += 1
                        log_line(log_callback, f"    [{serial_number - 1}] {row['视频链接']} ({row['视频类型']}) - 粉丝数: {row['作者粉丝数']}")

                    if get_comments_bool:
                        for r in sanitize_csv_rows(rows):
                            writer.writerow("视频信息", r)
                    else:
                        writer.writerows(sanitize_csv_rows(rows))

                    written_count += len(rows)
                    log_line(log_callback, f"  已写入 {written_count} 条视频")

                    if written_count >= max_results:
                        break

                writer.save()
                log_line(log_callback, f"  写入完成，共 {written_count} 条视频")

                if target_days:
                    register_job(output_path, target_days)
                    log_line(log_callback, f"  [快照注册] 成功为该文件注册了自动快照计划: {target_days}日")

            log_line(log_callback, "完成，已按关键词分别保存：")
            for path in output_paths:
                log_line(log_callback, f"  {path}")

            if should_stop(stop_event):
                break
            if not enable_timer or current_run >= timer_max_runs:
                break
            log_line(log_callback, f"=== 本次执行完毕。等待 {timer_interval_minutes} 分钟后进行下一次执行 ===")
            if interruptible_sleep(timer_interval_minutes * 60, stop_event):
                break

    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
        output_path = None
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if playwright_context:
            try:
                playwright_context.stop()
            except Exception:
                pass
        finish_callback(output_paths if output_paths else output_path)
