from __future__ import annotations

import re
import time

from src.core import interruptible_sleep, log_line, log_warn, should_stop, wait_if_paused


X_TRANSIENT_ERROR_PHRASES = (
    "問題が発生しました。再読み込みしてください。",
    "問題が発生しました",
    "再読み込みしてください",
    "やりなおす",
    "Something went wrong. Try reloading.",
    "Something went wrong",
    "Try reloading",
    "出错了",
    "发生错误",
    "请重新加载",
    "再試行",
)
X_RECOVERY_BACKOFF_SECONDS = (120.0, 180.0, 240.0)
X_READY_SELECTOR = (
    'article[data-testid="tweet"], '
    'div[data-testid="UserName"], '
    'div[data-testid="UserDescription"], '
    'div[data-testid="primaryColumn"], '
    '[data-testid="cellInnerDiv"]'
)


def is_x_transient_error_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    for phrase in X_TRANSIENT_ERROR_PHRASES:
        if phrase in text or phrase in normalized:
            return phrase
    return ""


def detect_x_transient_error(page) -> str:
    try:
        text = page.evaluate(
            """() => {
                const body = document.body;
                return body ? (body.innerText || body.textContent || '') : '';
            }"""
        )
    except Exception:
        return ""
    return is_x_transient_error_text(text)


def _format_wait(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)} 分钟"
    return f"{seconds:.0f} 秒"


def _sleep_with_pause(seconds: float, stop_event=None, pause_event=None) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds or 0.0))
    while time.monotonic() < deadline:
        if should_stop(stop_event):
            return False
        if wait_if_paused(pause_event, stop_event):
            return False
        chunk = min(1.0, max(0.0, deadline - time.monotonic()))
        if interruptible_sleep(chunk, stop_event, step=0.2):
            return False
    return not should_stop(stop_event)


def wait_for_x_page_recovery(
    page,
    log_callback=None,
    page_timeout: int | None = None,
    stop_event=None,
    pause_event=None,
    context_label: str = "X 页面",
    backoff_seconds: tuple[float, ...] | None = None,
    ready_selector: str = X_READY_SELECTOR,
) -> bool:
    """Wait and reload while X shows a transient reload/error screen."""
    backoff = tuple(backoff_seconds or X_RECOVERY_BACKOFF_SECONDS) or X_RECOVERY_BACKOFF_SECONDS
    timeout = int(page_timeout or 30000)
    attempt = 0

    while True:
        if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
            return False

        matched_phrase = detect_x_transient_error(page)
        if not matched_phrase:
            if attempt:
                log_line(log_callback, f"  {context_label} 已恢复正常，继续采集。")
            return True

        wait_seconds = backoff[min(attempt, len(backoff) - 1)]
        log_warn(
            log_callback,
            (
                f"  {context_label} 出现 X 临时错误/风控提示：{matched_phrase}；"
                f"等待 {_format_wait(wait_seconds)} 后刷新重试（第 {attempt + 1} 次）。"
            ),
        )
        if not _sleep_with_pause(wait_seconds, stop_event=stop_event, pause_event=pause_event):
            return False

        try:
            page.reload(wait_until="domcontentloaded", timeout=timeout)
        except Exception as exc:
            log_warn(log_callback, f"  {context_label} 刷新失败，将继续等待重试：{exc}")

        try:
            page.wait_for_selector(ready_selector, timeout=min(timeout, 15000))
        except Exception:
            pass
        if interruptible_sleep(2.0, stop_event):
            return False
        attempt += 1
