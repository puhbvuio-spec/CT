# -*- coding: utf-8 -*-
"""YouTube 视频数据与评论采集核心模块。

本模块基于 Google YouTube v3 API，提供视频详情（标题、播放量、发布日期、时长、简介等）、
精确视频类型检测（通过 HEAD 请求判断是否为 Shorts 短视频），以及视频主楼评论的高效分页采集。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import re
import time
from urllib.parse import parse_qs, urlparse

from googleapiclient.errors import HttpError
from src.platforms.youtube.keyword import YouTubeClientPool, execute_with_retry
from src.platforms.youtube.video_type import check_video_type_bulk

from src.core import MultiSheetXlsxWriter, XlsxRowWriter, build_output_path, log_error, log_line, log_warn, sanitize_csv_row, sanitize_csv_rows, should_stop, wait_if_paused, interruptible_sleep

# Excel 表头定义
VIDEO_FIELDS = ["编号", "视频链接", "博主主页链接", "标题", "频道名称", "发布日期", "视频类型", "直播状态", "关联视频标题", "关联视频链接", "视频时长", "视频简介", "播放量", "点赞数", "评论数"]
COMMENT_FIELDS = ["编号", "视频链接", "评论的点赞量", "评论内容", "发布时间"]

# 默认导出热门评论的上限
TOP_COMMENT_LIMIT = 100
# 默认扫描评论的最大安全阈值
DEFAULT_SCAN_LIMIT = 500
COMMENT_MODE_FAST = "快速模式"
COMMENT_MODE_DEEP = "深扫模式"
DEFAULT_COMMENT_WORKERS = 5


@dataclass(frozen=True)
class CommentFetchTask:
    video_id: str
    video_url: str = ""
    index: str = ""


@dataclass
class CommentFetchResult:
    video_id: str
    comments: list[dict]
    status: str = "ok"
    error: str = ""
    http_status: int = 0


def normalize_comment_mode(value: str | None) -> str:
    if str(value or "").strip() == COMMENT_MODE_DEEP:
        return COMMENT_MODE_DEEP
    return COMMENT_MODE_FAST


def effective_comment_scan_limit(max_scan_comments: int, top_comment_limit: int, comment_mode: str) -> int:
    scan_limit = max(0, int(max_scan_comments or 0))
    top_limit = max(1, int(top_comment_limit or TOP_COMMENT_LIMIT))
    if normalize_comment_mode(comment_mode) == COMMENT_MODE_FAST:
        return min(scan_limit, top_limit) if scan_limit > 0 else top_limit
    return max(scan_limit, top_limit)


def normalize_comment_workers(value) -> int:
    try:
        workers = int(value)
    except (TypeError, ValueError):
        workers = DEFAULT_COMMENT_WORKERS
    return max(1, min(workers, 10))


def format_youtube_datetime(date_str: str) -> str:
    """格式化 YouTube 返回的 ISO 8601 日期时间字符串为 "YYYY-MM-DD HH:MM:SS"。

    Args:
        date_str: 原始日期时间字符串（例如 "2026-06-04T12:00:00Z"）。

    Returns:
        str: 规整后的日期时间字符串。
    """
    if not date_str:
        return ""
    date_str = date_str.strip()
    cleaned = date_str.replace("T", " ").replace("Z", "").strip()
    if "." in cleaned:
        cleaned = cleaned.split(".")[0]
    return cleaned


def build_video_url(video_id: str, video_type: str) -> str:
    """根据视频 ID 和类型组装标准的视频播放 URL。

    Args:
        video_id: 视频的唯一 ID。
        video_type: 视频的类别（"Shorts" 或 其他）。

    Returns:
        str: 完整的播放链接。
    """
    if not video_id:
        return ""
    if video_type == "Shorts":
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize_youtube_url(url: str) -> str:
    """清洗并规范化输入的 YouTube 链接，丢弃锚点。

    Args:
        url: 原始链接。

    Returns:
        str: 规范化后的链接。
    """
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if not value.startswith("http"):
        value = "https://" + value
    return value.split("#")[0].strip()


def extract_video_id(url: str) -> str:
    """从各种格式的 YouTube 链接中提取 11 位的视频 ID。

    支持的链接样式：
    - Standard: youtube.com/watch?v=VIDEO_ID
    - Shorts: youtube.com/shorts/VIDEO_ID
    - Embed: youtube.com/embed/VIDEO_ID
    - Share link: youtu.be/VIDEO_ID
    - Live: youtube.com/live/VIDEO_ID

    Args:
        url: 输入的播放链接。

    Returns:
        str: 11 位的视频唯一 ID，若提取失败返回空。
    """
    normalized = normalize_youtube_url(url)
    parsed = urlparse(normalized)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if "youtu.be" in host and path_parts:
        return path_parts[0]
    if "youtube.com" in host:
        query_id = parse_qs(parsed.query).get("v", [""])[0]
        if query_id:
            return query_id
        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            return path_parts[1]

    # 正则作为后备兜底匹配
    match = re.search(r"(?:v=|/video/|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{6,})", normalized)
    return match.group(1) if match else ""


def canonical_video_url(video_id: str) -> str:
    """根据视频 ID 生成规范的普通视频链接。"""
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


def parse_video_entries(txt_path: str) -> list[dict[str, object]]:
    """读取 TXT 视频列表输入文件，提取唯一的视频链接及 ID 并去重。

    Args:
        txt_path: 存放链接的文本文件。

    Returns:
        list[dict]: 包含去重后视频编号、链接及 ID 的数据词典列表。
    """
    entries: list[dict[str, object]] = []
    seen_video_ids: set[str] = set()
    valid_line_count = 0
    duplicate_count = 0
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            raw_url = normalize_youtube_url(stripped.split()[0])
            video_id = extract_video_id(raw_url)
            if not video_id:
                continue
            valid_line_count += 1
            if video_id in seen_video_ids:
                duplicate_count += 1
                continue
            seen_video_ids.add(video_id)
            entries.append(
                {
                    "编号": len(entries) + 1,
                    "视频链接": canonical_video_url(video_id),
                    "视频ID": video_id,
                    "预测类型": "Shorts" if "/shorts/" in raw_url else "视频",
                }
            )
    # 为每一条条目记录本次解析的行数特征，方便驱动端日志打印
    for entry in entries:
        entry["有效行数"] = valid_line_count
        entry["重复行数"] = duplicate_count
    return entries


def clean_comment_text(text: str) -> str:
    """清洗评论内容文本，去除换行符并替换为特征空格以防破坏表格布局。"""
    return (text or "").replace("\r", "").replace("\n", " | ").strip()


def non_text_placeholder(snippet: dict) -> str:
    """针对富媒体/非纯文本的评论生成占位占字符标记。"""
    keys = " ".join(str(key).lower() for key in snippet.keys())
    if "image" in keys or "photo" in keys:
        return "[图片]"
    if "video" in keys:
        return "[视频]"
    if "sticker" in keys:
        return "[贴纸]"
    return "[非文本]"


def format_youtube_duration(iso_duration: str) -> str:
    """将 YouTube 返回的 ISO 8601 时长格式（如 PT1H23M45S）转换为标准时间格式（HH:MM:SS）。

    Args:
        iso_duration: ISO 8601 时长字符串。

    Returns:
        str: "HH:MM:SS" 格式的时间长度字符串。
    """
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        iso_duration or "",
    )
    if not match:
        return ""
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0) + days * 24
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def fetch_video_metrics(client_pool, video_ids: list[str], live_stream_policy: str = "不处理") -> dict[str, dict]:
    """调用 API 批量拉取视频的基本指标参数信息，单批上限 50 个。

    Args:
        youtube: 已实例化的 API 客户端。
        video_ids: 视频 ID 列表。

    Returns:
        dict[str, dict]: 视频 ID 到指标字典的映射映射表。
    """
    result = {}
    api_part = "snippet,statistics,contentDetails"
    if live_stream_policy in ("保留并标记", "直接排除"):
        api_part += ",liveStreamingDetails"

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        while True:
            try:
                response = execute_with_retry(
                    client_pool.client.videos().list(
                        part=api_part,
                        id=",".join(batch)
                    ),
                    None
                )
                break
            except HttpError as e:
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        continue
                raise e
        for item in response.get("items", []):
            vid = item.get("id")
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            pub_date = str(snippet.get("publishedAt", "")).replace("T", " ").replace("Z", "")
            if "." in pub_date:
                pub_date = pub_date.split(".")[0]
            desc = (snippet.get("description") or "").replace("\n", " | ").replace("\r", "")
            # 简介截断，防止表格内容过于臃肿
            if len(desc) > 300:
                desc = desc[:300] + "..."
            
            # 检测直播状态
            live_status = "非直播"
            if live_stream_policy != "不处理":
                broadcast_content = snippet.get("liveBroadcastContent", "none").lower()
                has_live_details = "liveStreamingDetails" in item
                
                if broadcast_content == "live":
                    live_status = "正在直播"
                elif broadcast_content == "upcoming":
                    live_status = "预告直播"
                elif has_live_details:
                    live_status = "直播回放"
                
                if live_stream_policy == "直接排除" and live_status != "非直播":
                    result[vid] = {"is_excluded": True}
                    continue
            
            result[vid] = {
                "标题": snippet.get("title", ""),
                "频道名称": snippet.get("channelTitle", ""),
                "频道ID": snippet.get("channelId", ""),
                "发布日期": pub_date,
                "直播状态": live_status if live_stream_policy != "不处理" else "",
                "视频时长": format_youtube_duration(content.get("duration", "")),
                "视频简介": desc,
                "播放量": stats.get("viewCount", ""),
                "点赞数": stats.get("likeCount", ""),
                "评论数": stats.get("commentCount", "")
            }
    return result


def fetch_top_level_comments(client_pool, video_id: str, max_scan_comments: int, log_callback, stop_event=None, pause_event=None, api_page_size: int = 100) -> list[dict]:
    """调用 YouTube API 分页获取指定视频下相关性排序的首层主楼评论。

    Args:
        youtube: API 客户端。
        video_id: 目标视频 ID。
        max_scan_comments: 最多扫描的评论条数。
        log_callback: 日志回调。
        stop_event: 线程停止信号。
        pause_event: 线程暂停信号。
        api_page_size: API 每次拉取的页面数据大小。

    Returns:
        list[dict]: 提取出的评论列表数据。
    """
    comments: list[dict] = []
    next_page_token = None
    page_size = max(1, min(api_page_size, 100))

    while len(comments) < max_scan_comments:
        if should_stop(stop_event):
            log_line(log_callback, "  任务已停止。")
            break
        if wait_if_paused(pause_event, stop_event):
            break
        
        # 请求 API 获取评论线程列表
        while True:
            try:
                response = execute_with_retry(
                    client_pool.client.commentThreads().list(
                        part="snippet",
                        videoId=video_id,
                        maxResults=min(page_size, max_scan_comments - len(comments)),
                        pageToken=next_page_token,
                        order="relevance",
                        textFormat="plainText",
                    ),
                    log_callback
                )
                break
            except HttpError as e:
                if "commentsDisabled" in str(e) or (e.resp.status == 403 and "disabled" in str(e).lower()):
                    log_line(log_callback, f"  [API] 视频评论被禁用 ({video_id})，跳过...")
                    return []
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        log_line(log_callback, f"  [API] 评论获取额度受限 ({e.resp.status})，切换 Key ({client_pool.current_idx + 1}/{len(client_pool.api_keys)})...")
                        continue
                    log_line(log_callback, f"  [API] 所有 API Key 配额均已耗尽 ({e.resp.status})，终止评论获取。")
                raise e

        for item in response.get("items", []):
            if should_stop(stop_event):
                break
            top_comment = item.get("snippet", {}).get("topLevelComment", {})
            snippet = top_comment.get("snippet", {})
            text = clean_comment_text(snippet.get("textDisplay") or snippet.get("textOriginal") or "")
            if not text:
                text = non_text_placeholder(snippet)
            published_at = str(snippet.get("publishedAt") or "")
            if published_at:
                published_at = format_youtube_datetime(published_at)
            comments.append(
                {
                    "like_count": int(snippet.get("likeCount", 0) or 0),
                    "text": text,
                    "published_at": published_at,
                }
            )
            if len(comments) >= max_scan_comments:
                log_line(log_callback, f"  已达扫描上限 {max_scan_comments} 条，停止翻页。")
                break

        if len(comments) % 200 == 0 or len(comments) < 100:
            log_line(log_callback, f"  已扫描主楼评论 {len(comments)} 条。")

        # 检查是否还有下一页
        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            log_line(log_callback, f"  评论已翻到底，共 {len(comments)} 条。")
            break

    return comments


def build_comment_rows(video_index: str, video_url: str, comments: list[dict], top_comment_limit: int = TOP_COMMENT_LIMIT) -> list[dict[str, str]]:
    sorted_comments = sorted(comments, key=lambda item: item["like_count"], reverse=True)
    rows: list[dict[str, str]] = []
    for comment in sorted_comments[:top_comment_limit]:
        rows.append(
            {
                "编号": str(video_index),
                "视频链接": video_url,
                "评论的点赞量": str(comment["like_count"]),
                "评论内容": comment["text"],
                "发布时间": comment.get("published_at", ""),
            }
        )
    return rows


def fetch_top_comments_for_videos(
    api_keys: list[str],
    video_tasks: list[CommentFetchTask],
    max_scan_comments: int,
    top_comment_limit: int,
    comment_mode: str,
    workers: int,
    log_callback,
    stop_event=None,
    pause_event=None,
    api_page_size: int = 100,
) -> dict[str, CommentFetchResult]:
    tasks = [task for task in video_tasks if task.video_id]
    if not tasks:
        return {}

    scan_limit = effective_comment_scan_limit(max_scan_comments, top_comment_limit, comment_mode)
    worker_count = min(normalize_comment_workers(workers), len(tasks))
    mode = normalize_comment_mode(comment_mode)
    log_line(log_callback, f"  评论采集：{mode}，并发 {worker_count}，每视频扫描上限 {scan_limit}，输出前 {top_comment_limit} 条。")

    results: dict[str, CommentFetchResult] = {}
    completed = 0
    failed = 0
    empty = 0
    written_comments = 0

    def _fetch_one(task: CommentFetchTask) -> CommentFetchResult:
        if should_stop(stop_event):
            return CommentFetchResult(task.video_id, [], "stopped")
        if wait_if_paused(pause_event, stop_event):
            return CommentFetchResult(task.video_id, [], "stopped")
        try:
            worker_pool = YouTubeClientPool(api_keys)
            comments = fetch_top_level_comments(
                worker_pool,
                task.video_id,
                scan_limit,
                None,
                stop_event,
                pause_event,
                api_page_size=api_page_size,
            )
            comments.sort(key=lambda item: item["like_count"], reverse=True)
            return CommentFetchResult(task.video_id, comments[:top_comment_limit], "ok" if comments else "empty")
        except HttpError as exc:
            if "commentsDisabled" in str(exc) or (exc.resp.status == 403 and "disabled" in str(exc).lower()):
                return CommentFetchResult(task.video_id, [], "disabled", str(exc), http_status=exc.resp.status)
            return CommentFetchResult(task.video_id, [], "error", str(exc), http_status=exc.resp.status)
        except Exception as exc:
            return CommentFetchResult(task.video_id, [], "error", str(exc))

    if worker_count <= 1:
        for task in tasks:
            if should_stop(stop_event):
                break
            result = _fetch_one(task)
            results[task.video_id] = result
            completed += 1
            if result.status == "error":
                failed += 1
            if result.status in {"empty", "disabled"}:
                empty += 1
            written_comments += len(result.comments)
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {executor.submit(_fetch_one, task): task for task in tasks}
            try:
                for future in as_completed(future_map):
                    task = future_map[future]
                    if should_stop(stop_event):
                        for f in future_map:
                            f.cancel()
                        break
                    result = future.result()
                    results[task.video_id] = result
                    completed += 1
                    if result.status == "error":
                        failed += 1
                    if result.status in {"empty", "disabled"}:
                        empty += 1
                    written_comments += len(result.comments)
                    if completed == len(tasks) or completed % max(1, worker_count) == 0:
                        log_line(log_callback, f"  评论采集进度：{completed}/{len(tasks)}，失败 {failed}，空/禁评 {empty}，已取评论 {written_comments} 条。")
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

    log_line(log_callback, f"  评论采集完成：{completed}/{len(tasks)}，失败 {failed}，空/禁评 {empty}，已取评论 {written_comments} 条。")
    return results


def empty_video_row(video_index: int, video_url: str) -> dict[str, str]:
    """当视频无评论或获取失败时，构建的空评论占位行。"""
    return {
        "编号": str(video_index),
        "视频链接": video_url,
        "评论的点赞量": "",
        "评论内容": "",
        "发布时间": "",
    }


def run_youtube_video_metrics_spider(api_keys: list[str], txt_path: str, fetch_shorts_related: str, live_stream_policy: str, get_comments: str, check_type: str, max_scan_comments: int, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    """运行 YouTube 视频数据与评论采集任务的主驱动函数。

    根据 TXT 文件输入批量获取视频基本热度、判定是否为 Shorts，
    并可选择性导出点赞排序后的置顶评论。

    Args:
        api_keys: API 服务密钥。
        txt_path: 输入文件路径。
        fetch_shorts_related: 抓取 Shorts 关联长视频。
        live_stream_policy: 直播内容处理策略。
        get_comments: 是否同时抓取置顶评论（"是" / "否"）。
        check_type: 是否进行精确长短类型（Shorts）判别（"是" / "否"）。
        max_scan_comments: 每视频最多评论提取深度。
        log_callback: 日志通知。
        finish_callback: 结束回调。
        stop_event: 线程安全停止信号。
        config: 参数配置。
        pause_event: 线程安全暂停信号。
    """
    if config is None:
        config = {}
    get_comments_bool = (get_comments == "是")
    check_type_bool = check_type == "是"
    fetch_shorts_related_bool = fetch_shorts_related == "是"
    
    # 用户明确反馈：若在TXT导入时已经由链接本身区分长短，则不强制开启检测，避免多余请求。
    # 仅当用户主动勾选精确检测时才做深层请求。
    top_comment_limit = int(config.get("comment_top_limit", TOP_COMMENT_LIMIT))
    api_page_size = int(config.get("youtube_api_page_size", 100))
    comment_mode = normalize_comment_mode(config.get("youtube_comment_mode", COMMENT_MODE_FAST))
    comment_workers = normalize_comment_workers(config.get("youtube_comment_workers", DEFAULT_COMMENT_WORKERS))

    output_path = None
    completed_path = None
    try:
        entries = parse_video_entries(txt_path)
        if not entries:
            log_warn(log_callback, "TXT 中没有找到有效的 YouTube 视频链接。")
            return
        valid_line_count = int(entries[0].get("有效行数", len(entries)))
        duplicate_count = int(entries[0].get("重复行数", 0))
        log_line(log_callback, f"读取到 {valid_line_count} 行有效视频链接，去重后唯一视频 {len(entries)} 个，重复链接 {duplicate_count} 行。")

        # 初始化 Google API 代理服务
        client_pool = YouTubeClientPool(api_keys)
        output_path = build_output_path("youtube", f"youtube_video_metrics_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="video_metrics")
        
        if get_comments_bool:
            # 涉及评论输出时，导出为包含“视频信息”和“评论信息”两个 Tab 页的 Excel
            writer = MultiSheetXlsxWriter(output_path, {"视频信息": VIDEO_FIELDS, "评论信息": COMMENT_FIELDS}, autosave_every=500)
        else:
            writer = XlsxRowWriter(output_path, VIDEO_FIELDS, autosave_every=500)
        
        video_ids = [str(e["视频ID"]) for e in entries]
        
        try:
            log_line(log_callback, f"正在批量获取 {len(video_ids)} 个视频的热度数据...")
            metrics_map = fetch_video_metrics(client_pool, video_ids, live_stream_policy)
        except Exception as exc:
            import googleapiclient.errors
            if isinstance(exc, googleapiclient.errors.HttpError) and exc.resp.status in [403, 429]:
                log_error(log_callback, "API 配额已耗尽，或无权访问，请更换 API Key。")
                return
            else:
                log_error(log_callback, f"获取视频热度失败: {exc}")
                return
                
        type_map = {}
        if check_type_bool:
            log_line(log_callback, f"正在精确检测 {len(video_ids)} 个视频的长短类型 (网络请求可能较慢)...")
            type_map = check_video_type_bulk(video_ids)

        comment_results: dict[str, CommentFetchResult] = {}
        if get_comments_bool:
            comment_tasks = [
                CommentFetchTask(
                    video_id=str(entry["视频ID"]),
                    video_url=str(entry["视频链接"]),
                    index=str(entry["编号"]),
                )
                for entry in entries
            ]
            comment_results = fetch_top_comments_for_videos(
                api_keys,
                comment_tasks,
                max_scan_comments,
                top_comment_limit,
                comment_mode,
                comment_workers,
                log_callback,
                stop_event,
                pause_event,
                api_page_size,
            )

        for progress_index, entry in enumerate(entries, 1):
            if should_stop(stop_event):
                log_line(log_callback, "任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break
            video_index = int(entry["编号"])
            video_url = str(entry["视频链接"])
            video_id = str(entry["视频ID"])

            log_line(log_callback, f"[{progress_index}/{len(entries)}] 处理编号 {video_index}：{video_url}")
            
            v_info = metrics_map.get(video_id)
            if v_info and v_info.get("is_excluded"):
                log_line(log_callback, "  因属于直播内容，跳过该视频。")
                continue
                
            detected_type = ""
            if not v_info:
                v_info = {
                    "标题": "[已删除或不可用]",
                    "频道名称": "",
                    "频道ID": "",
                    "发布日期": "",
                    "直播状态": "",
                    "视频时长": "",
                    "视频简介": "",
                    "播放量": "",
                    "点赞数": "",
                    "评论数": "",
                }
                detected_type = "已删除"
            elif check_type_bool:
                detected_type = type_map.get(video_id, "未知")
            else:
                detected_type = str(entry.get("预测类型", "视频"))
            
            final_pub_date = format_youtube_datetime(v_info.get("发布日期", ""))
            final_video_url = build_video_url(video_id, detected_type)
            channel_id = v_info.get("频道ID", "")
            channel_url = f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""
            
            rt, rl = "", ""
            if fetch_shorts_related_bool and detected_type == "Shorts":
                if interruptible_sleep(1.0, stop_event):
                    break
                log_line(log_callback, f"  获取 Shorts 关联长视频：{video_id}")
                from src.platforms.youtube.shorts import fetch_short_related_video
                rt, rl = fetch_short_related_video(video_id)
            
            row_video = {
                "编号": str(video_index),
                "视频链接": final_video_url,
                "博主主页链接": channel_url,
                "标题": v_info.get("标题", ""),
                "频道名称": v_info.get("频道名称", ""),
                "发布日期": final_pub_date,
                "视频类型": detected_type,
                "直播状态": v_info.get("直播状态", ""),
                "关联视频标题": rt,
                "关联视频链接": rl,
                "视频时长": v_info.get("视频时长", ""),
                "视频简介": v_info.get("视频简介", ""),
                "播放量": v_info.get("播放量", ""),
                "点赞数": v_info.get("点赞数", ""),
                "评论数": v_info.get("评论数", ""),
            }

            if get_comments_bool:
                writer.writerow("视频信息", sanitize_csv_row(row_video))
                
                try:
                    result = comment_results.get(video_id, CommentFetchResult(video_id, [], "empty"))
                    if result.status == "error":
                        if result.http_status in (403, 429) or "quotaExceeded" in result.error:
                            log_error(log_callback, f"  停止任务：API 配额耗尽 ({result.error})")
                            break
                        else:
                            log_warn(log_callback, f"  评论获取失败 ({video_id})：{result.error}，写入空占位行。")
                            writer.writerow("评论信息", sanitize_csv_row(empty_video_row(video_index, final_video_url)))
                            continue
                    rows = build_comment_rows(str(video_index), final_video_url, result.comments, top_comment_limit)
                    if not rows:
                        rows = [empty_video_row(video_index, final_video_url)]
                    for r in sanitize_csv_rows(rows):
                        writer.writerow("评论信息", r)
                    written_comments = len([row for row in rows if row["评论内容"]])
                    log_line(log_callback, f"  完成：播放 {v_info.get('播放量')}，点赞 {v_info.get('点赞数')}，评论 {v_info.get('评论数')}。写入热评 {written_comments} 条。")
                except Exception as exc:
                    writer.writerow("评论信息", sanitize_csv_row(empty_video_row(video_index, final_video_url)))
                    log_warn(log_callback, f"  抓取评论失败：{exc}，已写入空评论占位行。")
            else:
                writer.writerow(sanitize_csv_row(row_video))
                log_line(log_callback, f"  完成：播放 {v_info.get('播放量')}，点赞 {v_info.get('点赞数')}，评论 {v_info.get('评论数')}。")

        writer.save()

        log_line(log_callback, f"完成，已保存：{output_path}")
        completed_path = output_path
    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
        output_path = None
        completed_path = None
    finally:
        finish_callback(completed_path)
