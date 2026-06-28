# -*- coding: utf-8 -*-
"""YouTube 博主详情数据采集与解析模块。

本模块提供基于 Google YouTube v3 API 的博主/频道主页信息采集，
支持多种格式的 YouTube 频道 URL 识别、归一化与元数据（如名称、ID、粉丝数、简介）抓取。
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

from src.platforms.youtube.keyword import YouTubeClientPool, execute_with_retry
from googleapiclient.errors import HttpError

from src.core import XlsxRowWriter, build_output_path, log_error, log_line, log_warn, sanitize_csv_row, should_stop, wait_if_paused

# Excel 输出表头定义
CSV_FIELDS = ["作者主页链接", "作者名称", "作者ID", "粉丝量", "作者简介"]

def normalize_youtube_url(url: str) -> str:
    """归一化 YouTube URL 链接，清除 Query 参数和锚点。

    Args:
        url: 原始 URL。

    Returns:
        str: 规整后的标准 URL。
    """
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    if not value.startswith("http"):
        return "https://" + value
    return value.split("?")[0].split("#")[0].rstrip("/")

def parse_channel_url(url: str) -> tuple[str, str]:
    """解析 YouTube 频道 URL 类型，提取对应的特征识别值。

    支持以下频道链接格式：
    - ID 模式: youtube.com/channel/UC... (返回 UC...)
    - 用户名模式: youtube.com/user/username (返回 username)
    - 唯一标识模式: youtube.com/@handle (返回 @handle)
    - 自定义/短链接模式: youtube.com/c/custom_name (返回 custom_name)

    Args:
        url: 频道的 URL。

    Returns:
        tuple[str, str]: (识别键类型, 特征识别值)。
    """
    normalized = normalize_youtube_url(url)
    parsed = urlparse(normalized)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return "", ""

    first = parts[0]
    if first == "channel" and len(parts) >= 2:
        return "id", parts[1]
    if first == "user" and len(parts) >= 2:
        return "username", parts[1]
    if first.startswith("@"):
        return "handle", first
    if first in {"c", "custom"} and len(parts) >= 2:
        return "search", parts[1]
    return "search", first.lstrip("@")

def resolve_channel(client_pool, profile_url: str) -> dict:
    """调用 YouTube API 解析并获取频道的原始元数据。

    根据 URL 特征匹配调用相应的 API 参数（id / forUsername / forHandle）；
    若非标准结构则采用搜索 API 进行模糊检索定位。

    Args:
        client_pool: YouTubeClientPool 实例。
        profile_url: 频道的 URL。

    Returns:
        dict: API 返回的频道元数据字典，若未找到则返回空字典。
    """
    hint_type, hint_value = parse_channel_url(profile_url)
    if not hint_value:
        return {}

    def _execute_req(build_req):
        while True:
            try:
                return execute_with_retry(build_req(), None)
            except HttpError as e:
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        continue
                raise e

    if hint_type == "id":
        response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,statistics", id=hint_value))
    elif hint_type == "username":
        response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,statistics", forUsername=hint_value))
    elif hint_type == "handle":
        response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,statistics", forHandle=hint_value))
    else:
        # 针对自定义别名等非标准路径进行频道搜索
        search_response = _execute_req(lambda: client_pool.client.search().list(
            part="id",
            q=hint_value,
            type="channel",
            maxResults=1,
        ))
        items = search_response.get("items", [])
        if not items:
            return {}
        channel_id = items[0].get("id", {}).get("channelId", "")
        if not channel_id:
            return {}
        response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,statistics", id=channel_id))

    items = response.get("items", [])
    return items[0] if items else {}

def channel_row(profile_url: str, item: dict) -> dict:
    """提取频道元数据字典为符合保存格式的规范字典。

    Args:
        profile_url: 作者的主页原始 URL。
        item: API 返回的频道原始字典。

    Returns:
        dict: 清洗规范化后的导出数据行。
    """
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    channel_id = item.get("id", "")
    description = (snippet.get("description") or "").replace("\n", " | ").replace("\r", "").strip()
    return {
        "作者主页链接": normalize_youtube_url(profile_url),
        "作者名称": snippet.get("title", ""),
        "作者ID": channel_id,
        "粉丝量": stats.get("subscriberCount", "已隐藏"),
        "作者简介": description,
    }

def run_channel_spider(api_keys: list[str], txt_file_path, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    """运行 YouTube 频道/博主元数据获取任务的驱动入口函数。

    Args:
        api_key: YouTube API 密钥。
        txt_file_path: 存放作者主页链接的 TXT 文件路径。
        log_callback: 运行状态日志回调函数。
        finish_callback: 结束任务时的回调函数，接收导出的文件路径作为参数。
        stop_event: 线程停止事件信号。
        config: 任务参数配置字典。
        pause_event: 线程暂停事件信号。
    """
    output_path = None
    try:
        with open(txt_file_path, "r", encoding="utf-8-sig") as f:
            profile_urls = [normalize_youtube_url(line.strip()) for line in f if line.strip() and not line.strip().startswith("#")]

        profile_urls = [url for url in profile_urls if "youtube.com" in url or "youtu.be" in url]
        if not profile_urls:
            log_warn(log_callback, "TXT 中没有有效的 YouTube 作者主页链接。")
            return

        # 实例化 YouTube V3 API 服务客户端
        client_pool = YouTubeClientPool(api_keys)
        output_path = build_output_path("youtube", f"youtube_profiles_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="profiles")

        writer = XlsxRowWriter(output_path, CSV_FIELDS)
        for index, profile_url in enumerate(profile_urls, 1):
            if should_stop(stop_event):
                log_line(log_callback, "任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break
            log_line(log_callback, f"[{index}/{len(profile_urls)}] 解析作者：{profile_url}")
            try:
                item = resolve_channel(client_pool, profile_url)
                if not item:
                    log_warn(log_callback, "  未找到作者信息")
                    writer.writerow(
                        sanitize_csv_row({
                            "作者主页链接": profile_url,
                            "作者名称": "未找到",
                            "作者ID": "",
                            "粉丝量": "",
                            "作者简介": "",
                        })
                    )
                    continue

                row = channel_row(profile_url, item)
                writer.writerow(sanitize_csv_row(row))
                log_line(log_callback, f"  成功：{row['作者名称']} | 粉丝量：{row['粉丝量']}")
            except Exception as exc:
                log_warn(log_callback, f"  解析失败：{exc}")

        writer.save()

        log_line(log_callback, f"完成，已保存：{output_path}")
    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
        output_path = None
    finally:
        finish_callback(output_path)
