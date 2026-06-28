from __future__ import annotations

import re
import time
from typing import Any

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
    sanitize_xlsx_cell,
    should_stop,
    wait_if_paused,
)
from src.core.task_checkpoint import open_checkpointed_row_writer, open_task_checkpoint
from src.platforms.x_twitter.page_recovery import wait_for_x_page_recovery
from src.platforms.x_twitter.profile_tweets import navigate_to_profile_via_search

OUTPUT_FIELDS = ["推文链接", "作者主页链接", "作者的名称", "账号ID", "粉丝数", "简介"]
OUTPUT_FIELDS_PROFILE_MODE = ["作者主页链接", "作者的名称", "账号ID", "粉丝数", "简介"]
PAGE_LOAD_TIMEOUT = 45000
STATUS_RE = re.compile(r"/status/(\d+)")
TWEET_READY_TIMEOUT = 12000

def normalize_x_url(url: str) -> str:
    if not url:
        return ""
    normalized = url.strip().replace("twitter.com", "x.com")
    normalized = normalized.split("?")[0].split("#")[0]
    if normalized.startswith("//"):
        normalized = "https:" + normalized
    if normalized.startswith("/"):
        normalized = "https://x.com" + normalized
    if normalized and not normalized.startswith("http"):
        normalized = "https://" + normalized
    return normalized

def parse_tweet_links(txt_path: str) -> list[str]:
    links: list[str] = []
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            url = normalize_x_url(stripped.split()[0])
            if "/status/" in url:
                links.append(url)
    return links

def parse_profile_links(txt_path: str) -> list[str]:
    links: list[str] = []
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            url = normalize_x_url(stripped.split()[0])
            if url and "/status/" not in url:
                links.append(url)
    return links

def extract_status_id(url: str) -> str:
    match = STATUS_RE.search(url or "")
    return match.group(1) if match else ""

def parse_metric_number(text: str) -> float:
    if not text:
        return 0
    expanded = expand_compact_number(text)
    try:
        return float(expanded)
    except ValueError:
        return 0

def safe_text(locator, default: str = "") -> str:
    try:
        if locator.count() <= 0:
            return default
        return locator.first.inner_text(timeout=2000).strip() or default
    except Exception:
        return default

def safe_attr(locator, attr: str, default: str = "") -> str:
    try:
        if locator.count() <= 0:
            return default
        return locator.first.get_attribute(attr, timeout=2000) or default
    except Exception:
        return default

def extract_bio(page) -> str:
    """Extract the bio/description from a profile page."""
    selectors = [
        'div[data-testid="UserDescription"]',
        '[data-testid="UserDescription"] span',
        'div[data-testid="profile_bio"]',
        'div[id="profile-acc-bio"]',
    ]
    start_time = time.time()
    while time.time() - start_time < 8:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() <= 0:
                    continue
                text = locator.first.inner_text(timeout=1500).strip()
                if text:
                    return text.replace("\r", "").replace("\n", " | ")
            except Exception:
                continue
        try:
            text = page.evaluate(
                """() => {
                    const node = document.querySelector('[data-testid="UserDescription"]');
                    return node ? (node.textContent || '').trim() : '';
                }"""
            )
            if text:
                return text.replace("\r", "").replace("\n", " | ")
        except Exception:
            pass
        time.sleep(0.5)
    return ""


def find_target_article(page, target_status_id: str):
    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=20000)
    except Exception:
        return None

    articles = page.locator('article[data-testid="tweet"]').all()
    for article in articles:
        try:
            hrefs = [
                a.get_attribute("href") or ""
                for a in article.locator('a[href*="/status/"]').all()
            ]
            if any(target_status_id in href for href in hrefs):
                return article
        except Exception:
            continue
    return articles[0] if articles else None

def load_tweet_page(page, tweet_url: str, target_status_id: str, log_callback, page_timeout=None, tweet_ready_timeout=None, stop_event=None, pause_event=None) -> bool:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if tweet_ready_timeout is None:
        tweet_ready_timeout = TWEET_READY_TIMEOUT
    try:
        page.goto(tweet_url, wait_until="domcontentloaded", timeout=page_timeout)
        if not wait_for_x_page_recovery(
            page,
            log_callback=log_callback,
            page_timeout=page_timeout,
            stop_event=stop_event,
            pause_event=pause_event,
            context_label="X 推文页",
        ):
            return False
        page.wait_for_selector('article[data-testid="tweet"]', timeout=tweet_ready_timeout)
        return True
    except Exception as e:
        current_url = getattr(page, "url", "")
        title = ""
        try:
            title = page.title()
        except Exception:
            pass
        log_warn(log_callback, 
            f"  推文正文未在 {tweet_ready_timeout // 1000} 秒内渲染，快速跳过。当前 URL: {current_url or '未知'}，标题: {title or '未知'}，错误: {e}"
        )
    return False

def extract_author_from_article(article) -> dict:
    user_block = article.locator('div[data-testid="User-Name"]').first
    author_name = ""
    account_id = ""
    profile_url = ""

    try:
        spans = user_block.locator("span").all()
        for span in spans:
            text = span.inner_text(timeout=1000).strip()
            if not text:
                continue
            if text.startswith("@") and not account_id:
                account_id = text.lstrip("@")
            elif not author_name:
                author_name = text
    except Exception:
        pass

    try:
        links = user_block.locator('a[role="link"]').all()
        for link in links:
            href = link.get_attribute("href") or ""
            normalized = normalize_x_url(href)
            if not normalized or "/status/" in normalized:
                continue
            handle_match = re.search(r"x\.com/([^/?#]+)/?$", normalized)
            if handle_match:
                account_id = handle_match.group(1)
                profile_url = f"https://x.com/{account_id}"
                break
    except Exception:
        pass

    if account_id and not profile_url:
        profile_url = f"https://x.com/{account_id}"

    return {
        "author_name": author_name,
        "account_id": account_id,
        "profile_url": profile_url,
    }

def extract_view_count(article) -> tuple[str, float]:
    selectors = [
        'a[href*="/analytics"]',
        'div[data-testid="postViewCount"]',
        'span[aria-label*="Views"]',
        'span[aria-label*="浏览"]',
        'span[aria-label*="表示"]',
    ]
    for selector in selectors:
        try:
            locator = article.locator(selector)
            if locator.count() <= 0:
                continue
            text = locator.first.inner_text(timeout=1500).strip()
            aria = locator.first.get_attribute("aria-label", timeout=1500) or ""
            raw = text or aria
            if raw:
                return raw, parse_metric_number(raw)
        except Exception:
            continue
    return "", 0

def extract_followers_count(page, profile_url: str, page_timeout=None, stop_event=None, needs_navigation=True) -> str:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    
    if needs_navigation:
        try:
            # 去除末尾斜杠以确保对比一致
            target_clean = profile_url.rstrip('/').lower()
            current_clean = page.url.rstrip('/').lower()
            if target_clean not in current_clean:
                page.goto(profile_url, wait_until="domcontentloaded", timeout=page_timeout)
                if not wait_for_x_page_recovery(
                    page,
                    page_timeout=page_timeout,
                    stop_event=stop_event,
                    context_label="X 作者主页",
                ):
                    return ""
        except Exception:
            return ""

    selectors = [
        'a[href$="/followers"]',
        'a[href*="/followers"]',
        'a[href*="/verified_followers"]',
        'text=/(?:followers?|粉丝|关注者|粉絲|關注者|フォロワー|팔로워|seguidores?|abonn[eé]s?|читатели|متابع(ون)?|takipçi(ler)?|pengikut|फ़ॉलोअर्स)/i',
    ]

    start_time = time.time()
    max_poll_time = 15  # 最多轮询15秒等待网络数据返回
    
    while time.time() - start_time < max_poll_time:
        if should_stop(stop_event):
            break
            
        # 尝试绕过敏感内容警告 (Sensitive Content Warning)
        try:
            btn_selectors = [
                'div[data-testid="empty_state_button_text"]',
                'div[role="button"]:has-text("view profile")',
                'div[role="button"]:has-text("查看个人主页")',
                'div[role="button"]:has-text("プロフィールを表示")'
            ]
            for btn_sel in btn_selectors:
                btn = page.locator(btn_sel)
                if btn.count() > 0 and btn.first.is_visible(timeout=100):
                    btn.first.click(timeout=1000)
                    interruptible_sleep(1.0, stop_event)
                    break
        except Exception:
            pass

        for selector in selectors:
            try:
                for node in page.locator(selector).all():
                    # 使用 text_content() 代替 inner_text()，即使被弹窗遮挡也能读取
                    text = node.text_content(timeout=500) or ""
                    aria = node.get_attribute("aria-label", timeout=500) or ""
                    
                    # 为了应对文本选择器匹配到最深层节点（只有“粉丝”而无数字）的情况，向上获取父级和祖父级的文本
                    family_text = ""
                    try:
                        family_text = node.evaluate("""n => {
                            let p1 = n.parentElement ? n.parentElement.textContent : '';
                            let p2 = (n.parentElement && n.parentElement.parentElement) ? n.parentElement.parentElement.textContent : '';
                            return [n.textContent, p1, p2].join(' | ');
                        }""")
                    except Exception:
                        pass
                    
                    for raw in (text, aria, family_text):
                        if not raw:
                            continue
                            
                        # 确保数字紧挨着粉丝关键词，防止误匹配个人简介里的无关文本
                        pattern = r"([\d,.]+(?:\.\d+)?\s*(?:[KkMmBb]|千|万|萬|亿|億)?)\s*(?:followers?|粉丝|关注者|粉絲|關注者|フォロワー|팔로워|seguidores?|abonn[eé]s?|читатели|متابع(?:ون)?|takipçi(?:ler)?|pengikut|फ़ॉलोअर्स)"
                        match = re.search(pattern, raw, re.IGNORECASE)
                        if match:
                            return expand_compact_number(match.group(1).strip())
            except Exception:
                continue
                
        interruptible_sleep(1.0, stop_event)

    return ""

def extract_tweet_author_record(tweet_page, profile_page, tweet_url: str, log_callback, page_timeout=None, tweet_ready_timeout=None, stop_event=None) -> dict | None:
    target_status_id = extract_status_id(tweet_url)
    if not target_status_id:
        log_warn(log_callback, f"跳过：无法解析推文 ID：{tweet_url}")
        return None

    if not load_tweet_page(tweet_page, tweet_url, target_status_id, log_callback, page_timeout=page_timeout, tweet_ready_timeout=tweet_ready_timeout, stop_event=stop_event):
        log_warn(log_callback, f"跳过：推文页面一直卡在 X 启动页或未渲染正文：{tweet_url}")
        return None

    article = find_target_article(tweet_page, target_status_id)
    if article is None:
        log_warn(log_callback, f"跳过：未找到推文正文：{tweet_url}")
        return None

    author = extract_author_from_article(article)
    if not author["account_id"] or not author["profile_url"]:
        log_warn(log_callback, f"跳过：无法提取作者信息：{tweet_url}")
        return None

    view_text, view_value = extract_view_count(article)
    followers = extract_followers_count(profile_page, author["profile_url"], page_timeout=page_timeout, stop_event=stop_event)

    bio = extract_bio(profile_page)

    return {
        "推文链接": normalize_x_url(tweet_url),
        "作者主页链接": author["profile_url"],
        "作者的名称": author["author_name"],
        "账号ID": author["account_id"],
        "粉丝数": followers,
        "简介": bio,
        "_view_text": view_text,
        "_view_value": view_value,
    }

def extract_profile_record(profile_page, profile_url: str, log_callback, page_timeout=None, stop_event=None, needs_navigation=True) -> dict | None:
    """Extract profile info from a profile page, optionally navigating first."""
    profile_url = normalize_x_url(profile_url)
    if needs_navigation:
        try:
            profile_page.goto(profile_url, wait_until="domcontentloaded", timeout=page_timeout if page_timeout is not None else PAGE_LOAD_TIMEOUT)
        except Exception as e:
            log_warn(log_callback, f"跳过：无法加载主页：{profile_url}，错误：{e}")
            return None

    if not wait_for_x_page_recovery(
        profile_page,
        log_callback=log_callback,
        page_timeout=page_timeout if page_timeout is not None else PAGE_LOAD_TIMEOUT,
        stop_event=stop_event,
        context_label="X 作者主页",
    ):
        return None

    # Extract account ID from URL
    account_match = re.search(r"x\.com/([^/?#]+)/?$", profile_url)
    if not account_match:
        log_warn(log_callback, f"跳过：无法解析账号 ID：{profile_url}")
        return None
    account_id = account_match.group(1)

    # Extract author name from profile header
    author_name = ""
    try:
        profile_page.wait_for_selector('div[data-testid="UserName"]', state="attached", timeout=10000)
    except Exception:
        pass

    try:
        name_selectors = [
            'div[data-testid="UserName"] span',
            'div[data-testid="profile_header_0"] div[dir="auto"] span'
        ]
        for selector in name_selectors:
            try:
                for node in profile_page.locator(selector).all():
                    text = node.text_content(timeout=500)
                    if text and text.strip() and not text.strip().startswith('@'):
                        author_name = text.strip()
                        break
            except Exception:
                continue
            if author_name:
                break
    except Exception:
        pass

    # Extract followers count
    followers = extract_followers_count(profile_page, profile_url, page_timeout=page_timeout, stop_event=stop_event, needs_navigation=False)

    # Extract bio
    bio = extract_bio(profile_page)

    return {
        "作者主页链接": profile_url,
        "作者的名称": author_name,
        "账号ID": account_id,
        "粉丝数": followers,
        "简介": bio,
    }

def output_row(record: dict, fields: list[str]) -> dict:
    return {field: record.get(field, "") for field in fields}


def update_writer_row(writer: Any, row_number: int, record: dict, fields: list[str]) -> None:
    row = output_row(record, fields)
    for column_number, field in enumerate(fields, start=1):
        writer.worksheet.cell(row=row_number, column=column_number).value = sanitize_xlsx_cell(row.get(field, ""))
    writer.save()

def run_scraper(txt_path: str, input_mode: str, cdp_port_or_url: str, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    if config is None:
        config = {}
    page_load_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    tweet_ready_timeout = int(config.get("tweet_ready_timeout", TWEET_READY_TIMEOUT))
    cooldown_min = float(config.get("cooldown_min", 2.0))
    cooldown_max = float(config.get("cooldown_max", 5.0))
    cooldown_every_val = int(config.get("cooldown_every", 5))
    browser_choice = config.get("browser")

    output_path = None
    try:
        is_profile_mode = input_mode == "博主链接"
        
        if is_profile_mode:
            links = parse_profile_links(txt_path)
            output_fields = OUTPUT_FIELDS_PROFILE_MODE
            if not links:
                log_warn(log_callback, "TXT 中没有有效的博主链接。")
                return
        else:
            links = parse_tweet_links(txt_path)
            output_fields = OUTPUT_FIELDS
            if not links:
                log_warn(log_callback, "TXT 中没有有效的推文链接。")
                return
        checkpoint = open_task_checkpoint(
            "x_tweet_author_profiles",
            {"input_mode": input_mode, "links": links},
            log_callback=log_callback,
        )

        with sync_playwright() as p:
            log_line(log_callback, "正在连接本地浏览器...")
            try:
                _, context = connect_existing_chromium(p, cdp_port_or_url, browser=browser_choice)
            except Exception as e:
                log_error(log_callback, f"连接失败：请确认浏览器已自动打开并已登录 X/Twitter。错误：{e}")
                return

            tweet_page = context.new_page() if not is_profile_mode else None
            profile_page = context.new_page()
            default_output_path = build_output_path("x", f"x_profiles_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="profiles")
            output_path, writer = open_checkpointed_row_writer(
                checkpoint,
                default_output_path,
                output_fields,
                log_callback=log_callback,
            )
            checkpoint.add_output_path(output_path)
            best_by_author: dict[str, dict] = {}
            row_by_author: dict[str, int] = {}
            written_count = 0

            for index, link in enumerate(links, 1):
                if should_stop(stop_event):
                    log_line(log_callback, "任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                if checkpoint.is_completed(link):
                    log_line(log_callback, f"[{index}/{len(links)}] 断点续跑跳过已完成链接：{link}")
                    continue
                
                if is_profile_mode:
                    log_line(log_callback, f"[{index}/{len(links)}] 处理博主链接：{link}")
                    if navigate_to_profile_via_search(
                        profile_page,
                        link,
                        log_callback,
                        page_timeout=page_load_timeout,
                        stop_event=stop_event,
                        pause_event=pause_event,
                        initial_delay=2.0,
                    ):
                        record = extract_profile_record(profile_page, link, log_callback, page_timeout=page_load_timeout, stop_event=stop_event, needs_navigation=False)
                    else:
                        log_warn(log_callback, f"  跳过：未能通过搜索页进入作者主页：{link}")
                        record = None
                else:
                    log_line(log_callback, f"[{index}/{len(links)}] 处理推文：{link}")
                    record = extract_tweet_author_record(tweet_page, profile_page, link, log_callback, page_timeout=page_load_timeout, tweet_ready_timeout=tweet_ready_timeout, stop_event=stop_event)
                
                if not record:
                    continue

                account_key = record["账号ID"].lower()
                old_record = best_by_author.get(account_key)
                if old_record is None:
                    writer.writerow(output_row(record, output_fields))
                    best_by_author[account_key] = record
                    row_by_author[account_key] = writer.worksheet.max_row
                    written_count += 1
                    if is_profile_mode:
                        log_line(log_callback, f"  写入作者 {account_key or '未知'}。")
                    else:
                        log_line(log_callback, f"  写入作者 {account_key or '未知'}，当前推文浏览量 {record.get('_view_text') or '未知'}。")
                elif not is_profile_mode and record["_view_value"] > old_record.get("_view_value", 0):
                    best_by_author[account_key] = record
                    update_writer_row(writer, row_by_author[account_key], record, output_fields)
                    log_line(log_callback, 
                        f"  更新作者 {record['账号ID']}：更高浏览量 {record.get('_view_text') or '未知'}。"
                    )
                else:
                    if is_profile_mode:
                        log_line(log_callback, f"  跳过：作者 {record['账号ID']} 已处理过。")
                    else:
                        log_line(log_callback, f"  跳过：作者 {record['账号ID']} 已有更高浏览量推文。")
                checkpoint.mark_completed(link, {"output_path": output_path, "index": index, "account": account_key})
                
                # 按配置的冷却间隔应用随机休眠
                if index % cooldown_every_val == 0:
                    if random_cooldown(log_callback, stop_event, cooldown_min, cooldown_max):
                        break

            for opened_page in (tweet_page, profile_page):
                if opened_page is not None and not opened_page.is_closed():
                    opened_page.close()

        if not output_path:
            log_warn(log_callback, "没有提取到可输出的数据。")
            return
        writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
    finally:
        finish_callback(output_path)
