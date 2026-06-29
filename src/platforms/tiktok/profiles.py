from __future__ import annotations

import random
import re
import time
import html as html_lib
import json

from playwright.sync_api import sync_playwright

from src.core import (
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    random_cooldown,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)
from src.core.task_checkpoint import open_checkpointed_row_writer, open_task_checkpoint

CSV_FIELDS = ["博主主页链接", "博主名称", "博主ID", "粉丝量", "作者简介"]

def clean_url(url: str) -> str:
    """
    清洗并规范化输入的主页链接，补齐协议头并裁剪查询参数。
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        url = "https://www.tiktok.com" + url
    if not url.startswith("http"):
        url = "https://" + url
    return url.split("?")[0].split("#")[0].rstrip("/")

def normalize_profile_url(url: str) -> str:
    """
    从链接中提取博主 ID，组装成标准的 TikTok 博主主页 URL。
    """
    cleaned = clean_url(url)
    match = re.search(r"tiktok\.com/(@[^/?#]+)", cleaned)
    return f"https://www.tiktok.com/{match.group(1)}" if match else ""

def profile_id_from_url(profile_url: str) -> str:
    """
    从博主主页 URL 提取 @username 博主 ID。
    """
    match = re.search(r"tiktok\.com/@([^/?#]+)", profile_url)
    return f"@{match.group(1)}" if match else ""

def profile_handle_from_url(profile_url: str) -> str:
    """
    从博主主页 URL 提取不带 @ 的 username，便于和页面状态树中的 uniqueId 对比。
    """
    match = re.search(r"tiktok\.com/@([^/?#]+)", profile_url or "")
    return match.group(1).strip().lower() if match else ""

def parse_profile_urls(txt_path: str) -> list[str]:
    """
    从博主 TXT 配置文件中读取全部非重复且合法的博主主页链接。
    """
    urls: list[str] = []
    seen = set()
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for part in re.split(r"\s+", stripped):
                profile_url = normalize_profile_url(part)
                if profile_url and profile_url not in seen:
                    urls.append(profile_url)
                    seen.add(profile_url)
                    break
    return urls

def get_first_text(page, selectors: list[str], timeout: int = 2500) -> str:
    """
    使用多重选择器候选列表，安全返回页面中第一个匹配成功的节点的 inner_text。
    """
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() <= 0:
                continue
            try:
                text = loc.inner_text(timeout=timeout).strip()
            except Exception:
                text = (loc.text_content(timeout=timeout) or "").strip()
            if text:
                return text
        except Exception:
            continue
    return ""

def _format_plain_text(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"none", "null", "undefined", "nan"} else text

def _format_count(value) -> str:
    text = _format_plain_text(value)
    return expand_compact_number(text) if text else ""

def _iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)

def _parse_script_json(html: str, script_id: str):
    pattern = rf'<script[^>]+id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.S)
    if not match:
        return None
    try:
        return json.loads(html_lib.unescape(match.group(1)).strip())
    except Exception:
        return None

def _page_state_sources(page) -> list[dict]:
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
        for script_id in ("SIGI_STATE", "__UNIVERSAL_DATA_FOR_REHYDRATION__", "RENDER_DATA"):
            data = _parse_script_json(html, script_id)
            if isinstance(data, dict):
                sources.append(data)
    except Exception:
        pass
    return sources

def _user_matches_profile(user: dict, handle: str) -> bool:
    if not handle:
        return False
    unique_id = _format_plain_text(
        user.get("uniqueId") or user.get("unique_id") or user.get("unique_id_str") or user.get("username")
    ).lstrip("@")
    return unique_id.lower() == handle if unique_id else False

def _first_count_from_sources(sources: list[dict], keys: tuple[str, ...]) -> str:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source:
                value = _format_count(source.get(key))
                if value:
                    return value
    return ""

def _row_from_state_user(user: dict, stats: dict | None, profile_url: str) -> dict[str, str]:
    unique_id = _format_plain_text(
        user.get("uniqueId") or user.get("unique_id") or user.get("unique_id_str") or user.get("username")
    ).lstrip("@")
    author_id = f"@{unique_id}" if unique_id else profile_id_from_url(profile_url)
    author_name = _format_plain_text(
        user.get("nickname") or user.get("nickName") or user.get("displayName") or user.get("name")
    )
    bio = _format_plain_text(
        user.get("signature") or user.get("bio") or user.get("description") or user.get("desc")
    )
    stats_sources = [
        stats or {},
        user.get("stats") if isinstance(user.get("stats"), dict) else {},
        user.get("statsV2") if isinstance(user.get("statsV2"), dict) else {},
        user.get("statistics") if isinstance(user.get("statistics"), dict) else {},
        user,
    ]
    followers = _first_count_from_sources(
        stats_sources,
        ("followerCount", "follower_count", "followers", "fans", "fansCount", "fans_count"),
    )
    return {
        "博主主页链接": normalize_profile_url(profile_url) or profile_url,
        "博主名称": author_name or author_id,
        "博主ID": author_id,
        "粉丝量": followers,
        "作者简介": bio.replace("\r", "").replace("\n", " | "),
    }

def _extract_profile_row_from_state(page, profile_url: str) -> dict[str, str]:
    handle = profile_handle_from_url(profile_url)
    if not handle:
        return {}

    for source in _page_state_sources(page):
        user_module = source.get("UserModule") if isinstance(source, dict) else None
        if isinstance(user_module, dict):
            users = user_module.get("users") if isinstance(user_module.get("users"), dict) else {}
            stats_map = user_module.get("stats") if isinstance(user_module.get("stats"), dict) else {}
            for key, user in users.items():
                if not isinstance(user, dict):
                    continue
                if key.lower().lstrip("@") != handle and not _user_matches_profile(user, handle):
                    continue
                stats = stats_map.get(key) if isinstance(stats_map.get(key), dict) else {}
                return _row_from_state_user(user, stats, profile_url)

        for node in _iter_dicts(source):
            if not isinstance(node, dict):
                continue
            nested_user = node.get("user") if isinstance(node.get("user"), dict) else None
            nested_stats = node.get("stats") if isinstance(node.get("stats"), dict) else {}
            if nested_user and _user_matches_profile(nested_user, handle):
                return _row_from_state_user(nested_user, nested_stats, profile_url)
            if _user_matches_profile(node, handle):
                return _row_from_state_user(node, nested_stats, profile_url)
    return {}

def _sleep(seconds: float, stop_event=None, pause_event=None) -> bool:
    if stop_event is not None:
        return interruptible_sleep(seconds, stop_event, pause_event=pause_event)
    if pause_event is not None:
        return interruptible_sleep(seconds, pause_event=pause_event)
    time.sleep(seconds)
    return False

def extract_profile_row(page, profile_url: str, page_load_timeout: int = 35000, captcha_wait: int = 12, stop_event=None, pause_event=None) -> dict[str, str]:
    """
    进入指定的博主主页，检测人机验证码，并安全提取博主名称、ID、粉丝量、博主简介等元数据。
    如果账号不存在或已注销，进行优雅的错误处理并返回标识字段。
    """
    profile_url = normalize_profile_url(profile_url) or clean_url(profile_url)
    wait_if_paused(pause_event, stop_event)
    page.goto(profile_url, wait_until="domcontentloaded", timeout=page_load_timeout)
    try:
        wait_if_paused(pause_event, stop_event)
        page.wait_for_selector(
            "[data-e2e='user-title'], [data-e2e='followers-count'], script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE, h1",
            timeout=10000,
        )
    except Exception:
        pass
    if _sleep(random.uniform(2.0, 3.8), stop_event, pause_event=pause_event):
        return {
            "博主主页链接": profile_url,
            "博主名称": "",
            "博主ID": profile_id_from_url(profile_url),
            "粉丝量": "",
            "作者简介": "",
        }

    try:
        # 检测是否弹出人机验证页面，若有则睡眠指定秒数供人工操作
        if "captcha" in page.url or page.locator("div[id^='captcha']").count() > 0:
            _sleep(captcha_wait, stop_event, pause_event=pause_event)
    except Exception:
        pass

    missing_text = page.locator("text=/Couldn't find this account|无法找到此账号|账号不存在/i")
    if missing_text.count() > 0:
        return {
            "博主主页链接": profile_url,
            "博主名称": "账号不可用",
            "博主ID": profile_id_from_url(profile_url),
            "粉丝量": "",
            "作者简介": "账号不存在、已注销或当前不可见",
        }

    state_row = _extract_profile_row_from_state(page, profile_url)

    # 多重元素路径定位保障数据高提取率
    user_title = get_first_text(page, ["[data-e2e='user-title']", "h1[data-e2e='user-title']", "h1"])
    user_subtitle = get_first_text(page, ["[data-e2e='user-subtitle']", "h2[data-e2e='user-subtitle']", "h2"])
    followers = expand_compact_number(get_first_text(page, [
        "[data-e2e='followers-count']",
        "strong[data-e2e='followers-count']",
        "span[data-e2e='followers-count']",
    ]))
    bio = get_first_text(page, [
        "[data-e2e='user-bio']",
        "[data-e2e='user-signature']",
        "h2[data-e2e='user-subtitle'] + div",
    ])

    profile_id = profile_id_from_url(profile_url)
    dom_author_id = ""
    dom_author_name = ""
    if user_title.startswith("@"):
        dom_author_id = user_title
        dom_author_name = user_subtitle
    elif user_subtitle.startswith("@"):
        dom_author_id = user_subtitle
        dom_author_name = user_title
    else:
        dom_author_id = user_title
        dom_author_name = user_subtitle

    if dom_author_id and profile_id and dom_author_id.lower().lstrip("@") == profile_id.lower().lstrip("@"):
        dom_author_id = profile_id

    author_id = state_row.get("博主ID") or dom_author_id or profile_id
    author_name = state_row.get("博主名称") or dom_author_name or user_title or author_id
    followers = followers or state_row.get("粉丝量", "")
    bio = (bio or state_row.get("作者简介", "")).replace("\r", "").replace("\n", " | ")

    return {
        "博主主页链接": profile_url,
        "博主名称": author_name,
        "博主ID": author_id,
        "粉丝量": followers,
        "作者简介": bio,
    }

def run_tiktok_profile_spider(txt_path: str, cdp_port_or_url: str, log_callback, finish_callback, stop_event=None, pause_event=None, config=None):
    """
    TikTok 博主主页基础元数据爬虫主入口。
    顺序遍历 TXT 文件中的博主链接，提取基础元数据并保存至对应的 Excel 报表中，
    支持随机频控降温和动态配置超时及验证码等待秒数。
    """
    if config is None:
        config = {}
    page_load_timeout = int(config.get("page_load_timeout", 35000))
    captcha_wait = int(config.get("captcha_wait", 12))
    cooldown_every_val = int(config.get("cooldown_every", 5))
    cooldown_min_val = float(config.get("cooldown_min", 3.0))
    cooldown_max_val = float(config.get("cooldown_max", 8.0))

    output_path = None
    completed_path = None
    try:
        profile_urls = parse_profile_urls(txt_path)
        if not profile_urls:
            log_warn(log_callback, "TXT 中没有找到有效的 TikTok 博主主页链接。")
            return
        checkpoint = open_task_checkpoint(
            "tiktok_profile_directory",
            {"profile_urls": profile_urls},
            log_callback=log_callback,
        )

        default_output_path = build_output_path("tiktok", f"tiktok_profiles_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="profiles")
        output_path, writer = open_checkpointed_row_writer(
            checkpoint,
            default_output_path,
            CSV_FIELDS,
            log_callback=log_callback,
        )
        checkpoint.add_output_path(output_path)

        with sync_playwright() as p:
            log_line(log_callback, "正在连接本地 Chrome...")
            try:
                _, context = connect_existing_chromium(p, cdp_port_or_url)
            except Exception as exc:
                log_error(log_callback, f"连接失败：请确认 Chrome 已自动打开并已登录 TikTok。错误：{exc}")
                return

            page = context.new_page()
            for index, profile_url in enumerate(profile_urls, 1):
                if should_stop(stop_event):
                    log_line(log_callback, "任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                claimed, claim_status = checkpoint.claim_item(profile_url, positive_count_fields=("profile_ok",))
                if not claimed:
                    if claim_status == "active":
                        log_line(log_callback, f"[{index}/{len(profile_urls)}] 双开分流跳过正在处理的博主：{profile_url}")
                    else:
                        log_line(log_callback, f"[{index}/{len(profile_urls)}] 断点续跑跳过已完成博主：{profile_url}")
                    continue
                log_line(log_callback, f"[{index}/{len(profile_urls)}] 提取博主信息：{profile_url}")
                profile_ok = False
                try:
                    row = extract_profile_row(page, profile_url, page_load_timeout=page_load_timeout, captcha_wait=captcha_wait, stop_event=stop_event, pause_event=pause_event)
                    profile_ok = True
                    log_line(log_callback, f"  完成：{row['博主名称']} | {row['博主ID']} | 粉丝 {row['粉丝量'] or '未提取'}")
                except Exception as exc:
                    row = {
                        "博主主页链接": profile_url,
                        "博主名称": "抓取失败",
                        "博主ID": profile_id_from_url(profile_url),
                        "粉丝量": "",
                        "作者简介": str(exc),
                    }
                    log_error(log_callback, f"  失败：{exc}")

                writer.writerow(sanitize_csv_row(row))
                if profile_ok:
                    checkpoint.mark_completed(profile_url, {"output_path": output_path, "index": index, "profile_ok": 1})
                else:
                    checkpoint.release_item(profile_url)
                    log_warn(log_callback, "  本轮未完整采集成功，未写入断点完成标记，下次会继续重试。")
                # 每抓取 5 个博主主页进行随机冷却，以避免触发高频风控限制
                if index % cooldown_every_val == 0:
                    if random_cooldown(log_callback, stop_event, cooldown_min_val, cooldown_max_val, pause_event=pause_event):
                        break

            if not page.is_closed():
                page.close()

        writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
        completed_path = output_path
    finally:
        finish_callback(completed_path)
