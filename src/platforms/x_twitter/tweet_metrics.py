from __future__ import annotations

import re
import time

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from src.core import (
    DEFAULT_X_CDP_URL,
    XlsxRowWriter,
    MultiSheetXlsxWriter,
    _is_page_closed,
    _recreate_page,
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    random_cooldown,
    should_stop,
    wait_if_paused,
)
from src.platforms.x_twitter.comments import extract_comments
from src.platforms.x_twitter.keyword import _x_media_tag, get_media_label


CSV_FIELDS = ["序号", "推文链接", "推文的内容", "浏览量", "评论数", "点赞量", "转发量", "标签"]
PAGE_LOAD_TIMEOUT = 30000
COOLDOWN_EVERY = 3
COOLDOWN_MIN_SECONDS = 3.0
COOLDOWN_MAX_SECONDS = 8.0
STATUS_RE = re.compile(r"/[^/?#]+/status/(\d+)")
NUMBER_RE = re.compile(r"(\d[\d,.]*(?:\.\d+)?\s*(?:[KkMmBb]|千|万|萬|亿|億)?)")


def clean_tweet_url(url: str) -> str:
    value = (url or "").strip().replace("twitter.com", "x.com")
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://x.com" + value
    if not value.startswith("http"):
        value = "https://" + value
    return value.split("?")[0].split("#")[0].rstrip("/")


def parse_tweet_urls(txt_path: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            url = clean_tweet_url(stripped.split()[0])
            if "/status/" in url and url not in seen:
                urls.append(url)
                seen.add(url)
    return urls


def extract_status_id(url: str) -> str:
    match = STATUS_RE.search(clean_tweet_url(url))
    return match.group(1) if match else ""


def normalize_metric_text(text: str, default: str = "") -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return default
    match = NUMBER_RE.search(value)
    return expand_compact_number(match.group(1).strip(), default=default) if match else default


def normalize_interaction_metric(text: str) -> str:
    return normalize_metric_text(text, default="0")


def article_has_status_id(article, status_id: str) -> bool:
    if not status_id:
        return False
    try:
        return bool(
            article.evaluate(
                """(article, statusId) => {
                    return Array.from(article.querySelectorAll('time')).some(time => {
                        const link = time.closest('a[href*="/status/"]');
                        return Boolean(link && link.href && link.href.includes(`/status/${statusId}`));
                    });
                }""",
                status_id,
            )
        )
    except Exception:
        return False


def find_target_article(page, status_id: str, page_timeout=None):
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    try:
        page.wait_for_selector('article[data-testid="tweet"], article', timeout=page_timeout)
    except Exception:
        return None

    try:
        articles = page.locator('article[data-testid="tweet"], article').all()
    except Exception:
        return None

    for article in articles:
        if article_has_status_id(article, status_id):
            return article
    return None


def extract_article_payload(article) -> dict[str, str]:
    return article.evaluate(
        """async (article) => {
            const firstText = selector => {
                const node = article.querySelector(selector);
                return node ? (node.innerText || node.textContent || '').trim() : '';
            };
            const firstMetric = selectors => {
                for (const selector of selectors) {
                    for (const node of article.querySelectorAll(selector)) {
                        const rawText = (node.innerText || node.textContent || '').trim();
                        const aria = (node.getAttribute('aria-label') || '').trim();
                        if (/\\d/.test(rawText)) return rawText;
                        if (/\\d/.test(aria)) return aria;
                    }
                }
                return '';
            };
            const nonTextContent = () => {
                const types = [];
                if (article.querySelector('[data-testid="tweetPhoto"], img[src*="/media/"]')) types.push('图片');
                if (article.querySelector('video')) types.push('视频');
                if ((article.innerText || '').split('\\n').some(line => line.trim().toLowerCase() === 'gif')) types.push('GIF');
                if (article.querySelector('[data-testid="card.wrapper"], [data-testid="card.layoutLarge.media"], [data-testid="card.layoutSmall.media"]')) types.push('卡片');
                return types.length ? `[${types.join('+')}]` : '[非文本]';
            };

            const tweetTextEl = article.querySelector('[data-testid="tweetText"]');
            if (tweetTextEl) {
                // Step 1: Revert auto-translation
                const revertTexts = ['view original', '查看原文', '原文を表示', 'show original', '原文を見る'];
                const allNodes = article.querySelectorAll('*');
                for (const node of allNodes) {
                    const nodeText = (node.textContent || '').trim().toLowerCase();
                    if (!nodeText || node.children.length > 0) continue;
                    if (revertTexts.includes(nodeText)) {
                        try { node.click(); } catch (_) {}
                        break;
                    }
                }
                // Step 2: Remove CSS truncation
                tweetTextEl.style.setProperty('max-height', 'none', 'important');
                tweetTextEl.style.setProperty('overflow', 'visible', 'important');
                tweetTextEl.style.setProperty('-webkit-line-clamp', 'unset', 'important');
                tweetTextEl.style.setProperty('display', 'block', 'important');
                tweetTextEl.style.setProperty('white-space', 'normal', 'important');
                // Step 3: Click "Show more" if present
                const expandTexts = ['show more', 'show more...', 'もっと見る', '더 보기'];
                for (const node of allNodes) {
                    const nodeText = (node.textContent || '').trim().toLowerCase();
                    if (!nodeText || node.children.length > 0) continue;
                    if (!expandTexts.includes(nodeText)) continue;
                    try { node.click(); } catch (_) {}
                    break;
                }
                // Wait for React to re-render with original text
                await new Promise(r => setTimeout(r, 400));
            }
            const content = firstText('[data-testid="tweetText"]') || nonTextContent();
            return {
                content,
                views: firstMetric([
                    'a[href*="/analytics"]',
                    'div[data-testid="postViewCount"]',
                    '[aria-label*="Views"]',
                    '[aria-label*="views"]',
                    '[aria-label*="浏览"]',
                ]),
                replies: firstMetric(['[data-testid="reply"]']),
                likes: firstMetric(['[data-testid="like"]', '[data-testid="unlike"]']),
                reposts: firstMetric(['[data-testid="retweet"]', '[data-testid="unretweet"]']),
            };
        }"""
    )


def collect_tweet_metrics(page, tweet_url: str, page_timeout=None, page_ready_wait=2.5, stop_event=None, log_callback=None) -> dict[str, str]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    normalized_url = clean_tweet_url(tweet_url)
    status_id = extract_status_id(normalized_url)
    if not status_id:
        raise ValueError("无法解析推文 ID")

    # X 是 SPA，wait_until="load" 后经常只显示 X logo 而推文未渲染。
    # 这里用 domcontentloaded 加速导航，然后显式等待推文 article 出现。
    page.goto(normalized_url, wait_until="domcontentloaded", timeout=page_timeout)

    # 等待推文内容渲染，最多 15 秒；若超时则 reload 一次再等待，处理偶发卡加载
    _tweet_rendered = False
    for _ in range(2):
        try:
            page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
            _tweet_rendered = True
            break
        except Exception:
            if log_callback:
                log_line(log_callback, "  推文内容未渲染，尝试刷新页面...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=page_timeout)
            except Exception:
                pass
    if not _tweet_rendered:
        if log_callback:
            log_warn(log_callback, "  刷新后仍无法定位推文内容。")

    # 额外固定等待，让懒加载指标（浏览量等）稳定
    interruptible_sleep(page_ready_wait, stop_event)

    current_url = page.url
    if "login" in current_url.lower() or "account" in current_url.lower():
        log_warn(log_callback, f"  警告：当前页面疑似登录页：{current_url}")
        raise RuntimeError("页面跳转到登录页，请确认当前浏览器已登录 X/Twitter")

    article = find_target_article(page, status_id, page_timeout=page_timeout)
    if article is None:
        log_warn(log_callback, f"  当前页面 URL：{current_url}")
        raise RuntimeError("未找到目标推文 DOM（页面可能未完全加载、被限流或推文不存在）")

    payload = extract_article_payload(article)
    media_label = get_media_label(article)
    return {
        "推文链接": normalized_url,
        "推文的内容": payload.get("content", ""),
        "浏览量": normalize_metric_text(payload.get("views", "")),
        "评论数": normalize_interaction_metric(payload.get("replies", "")),
        "点赞量": normalize_interaction_metric(payload.get("likes", "")),
        "转发量": normalize_interaction_metric(payload.get("reposts", "")),
        "标签": _x_media_tag(media_label),
    }


def run_x_tweet_metrics_spider(
    txt_path: str,
    get_comments_str: str,
    max_comments: int,
    cdp_port_or_url: str = DEFAULT_X_CDP_URL,
    log_callback=None,
    finish_callback=None,
    stop_event=None,
    config=None,
    pause_event=None,
):
    if config is None:
        config = {}
    page_load_timeout_val = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    page_ready_wait_val = float(config.get("page_ready_wait", 2.5))
    tweet_comment_top_limit = int(config.get("comment_top_limit", 100))
    cooldown_every_val = int(config.get("cooldown_every", COOLDOWN_EVERY))
    cooldown_min_val = float(config.get("cooldown_min", COOLDOWN_MIN_SECONDS))
    cooldown_max_val = float(config.get("cooldown_max", COOLDOWN_MAX_SECONDS))
    browser_choice = config.get("browser")

    completed_path = None
    page = None
    try:
        if sync_playwright is None:
            log_error(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        tweet_urls = parse_tweet_urls(txt_path)
        if not tweet_urls:
            log_line(log_callback, "TXT 中没有有效的推文链接。")
            return

        get_comments_bool = get_comments_str == "是"
        scan_limit = max(int(max_comments), tweet_comment_top_limit)

        output_path = build_output_path("x", f"x_tweet_metrics_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="tweet_metrics")
        if get_comments_bool:
            comment_fields = ["序号", "推文链接", "评论的点赞量", "评论内容", "评论发布时间"]
            writer = MultiSheetXlsxWriter(output_path, {"推文信息": CSV_FIELDS, "评论信息": comment_fields}, autosave_every=20)
        else:
            writer = XlsxRowWriter(output_path, CSV_FIELDS, autosave_every=20)

        with sync_playwright() as playwright:
            log_line(log_callback, "正在连接本地浏览器...")
            try:
                _, context = connect_existing_chromium(playwright, cdp_port_or_url, browser=browser_choice)
            except Exception as exc:
                log_error(log_callback, f"无法连接浏览器：{exc}")
                log_error(log_callback, "连接失败：请确认浏览器已自动打开并已登录 X/Twitter。")
                return

            page = context.new_page()
            consecutive_failures = 0
            for index, tweet_url in enumerate(tweet_urls, 1):
                if should_stop(stop_event):
                    log_line(log_callback, "任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break

                normalized_url = clean_tweet_url(tweet_url)
                row = {
                    "序号": str(index),
                    "推文链接": normalized_url,
                    "推文的内容": "",
                    "浏览量": "",
                    "评论数": "",
                    "点赞量": "",
                    "转发量": "",
                    "标签": "",
                }
                log_line(log_callback, f"[{index}/{len(tweet_urls)}] 读取推文：{normalized_url}")

                # 风控/崩溃可能导致 page 被关闭，先检测并重建
                if _is_page_closed(page):
                    log_line(log_callback, "  推文页已关闭，重新创建页面...")
                    page = _recreate_page(context, page)
                    consecutive_failures = 0

                def _try_collect_metrics(attempt_page):
                    row.update(
                        collect_tweet_metrics(
                            attempt_page,
                            normalized_url,
                            page_timeout=page_load_timeout_val,
                            page_ready_wait=page_ready_wait_val,
                            stop_event=stop_event,
                            log_callback=log_callback,
                        )
                    )

                success = False
                try:
                    _try_collect_metrics(page)
                    success = True
                except PlaywrightTimeoutError:
                    log_error(log_callback, "  页面加载超时，写入空指标行。")
                except Exception as exc:
                    err_msg = str(exc)
                    # page 被关闭：重建后重试一次
                    if _is_page_closed(page) or "Target page, context or browser has been closed" in err_msg:
                        log_line(log_callback, f"  读取失败（{err_msg}），重建页面后重试...")
                        page = _recreate_page(context, page)
                        try:
                            _try_collect_metrics(page)
                            success = True
                        except PlaywrightTimeoutError:
                            log_error(log_callback, "  页面加载超时，写入空指标行。")
                        except Exception as exc2:
                            log_error(log_callback, f"  重试仍失败，写入空指标行：{exc2}")
                    else:
                        log_error(log_callback, f"  处理失败，写入空指标行：{exc}")

                if success:
                    consecutive_failures = 0
                    if get_comments_bool:
                        try:
                            comments = extract_comments(page, normalized_url, scan_limit, log_callback, stop_event, pause_event=pause_event)
                            comments.sort(key=lambda item: int(item.get("likes", "0") or 0), reverse=True)
                            for comment in comments[:tweet_comment_top_limit]:
                                comment_row = {
                                    "序号": row["序号"],
                                    "推文链接": normalized_url,
                                    "评论的点赞量": comment.get("likes", ""),
                                    "评论内容": comment.get("content", ""),
                                    "评论发布时间": comment.get("time", "")
                                }
                                writer.writerow("评论信息", comment_row)
                        except Exception as exc:
                            log_line(log_callback, f"  提取评论失败：{exc}")
                else:
                    consecutive_failures += 1

                if get_comments_bool:
                    writer.writerow("推文信息", row)
                else:
                    writer.writerow(row)
                log_line(log_callback, "  完成：已写入。")

                # 连续多次失败（限流/页面变脏），主动刷新标签页清理状态
                if consecutive_failures >= 3:
                    log_line(log_callback, "  连续失败 3 次，主动刷新标签页...")
                    page = _recreate_page(context, page)
                    consecutive_failures = 0

                if index < len(tweet_urls) and index % cooldown_every_val == 0:
                    if random_cooldown(log_callback, stop_event, cooldown_min_val, cooldown_max_val):
                        break

            if page and not page.is_closed():
                page.close()

        completed_path = output_path
        writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
    finally:
        try:
            if page and not page.is_closed():
                page.close()
        except Exception:
            pass
        if finish_callback:
            finish_callback(completed_path)
