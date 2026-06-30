# -*- coding: utf-8 -*-
"""Helpers for saving files from visible browser download controls."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.core.output import build_output_path

VISIBLE_DOWNLOAD_FIELDS = [
    "页面类型",
    "页面URL",
    "最终URL",
    "控件序号",
    "控件文本",
    "控件链接",
    "建议文件名",
    "保存路径",
    "状态",
    "备注",
    "查询时间",
]

_DOWNLOAD_TEXT_RE = re.compile(r"(?<![a-z0-9])(?:download|export|csv|json|xlsx?|excel)(?![a-z0-9])", flags=re.I)
_DOWNLOAD_CN_RE = re.compile(r"(?:导出|下载)")
_DOWNLOAD_DATA_PHRASE_RE = re.compile(r"(?<![a-z0-9])(?:(?:download|export)\\s+data|data\\s+(?:download|export))(?![a-z0-9])", flags=re.I)
_EXPORT_MENU_RE = re.compile(r"(?<![a-z0-9])(?:chart\\s+menu|export\\s+menu|context\\s+menu|view\\s+chart\\s+menu)(?![a-z0-9])", flags=re.I)
_DOWNLOAD_EXT_RE = re.compile(r"\.(?:csv|json|xlsx?|zip|txt)(?:$|[?#])", flags=re.I)
_MEDIA_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|svg)(?:$|[?#])", flags=re.I)
_MEDIA_CONTROL_RE = re.compile(r"(?<![a-z0-9])(?:image|png|jpe?g|gif|webp|svg)(?![a-z0-9])", flags=re.I)
_SAFE_FILENAME_RE = re.compile(r'[\\/*?:"<>|]+')


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def make_download_record(
    *,
    page_type: str = "",
    page_url: str = "",
    final_url: str = "",
    control_index: int = 0,
    control_text: str = "",
    control_href: str = "",
    suggested_filename: str = "",
    saved_path: str = "",
    status: str = "",
    note: str = "",
) -> dict[str, Any]:
    return {
        "页面类型": page_type,
        "页面URL": page_url,
        "最终URL": final_url,
        "控件序号": control_index,
        "控件文本": control_text,
        "控件链接": control_href,
        "建议文件名": suggested_filename,
        "保存路径": saved_path,
        "状态": status,
        "备注": note,
        "查询时间": now_text(),
    }


def safe_download_filename(value: str, default: str = "download") -> str:
    text = unquote(str(value or "")).strip().replace("\x00", "")
    text = text.split("?")[0].split("#")[0]
    text = Path(text).name if text else ""
    text = _SAFE_FILENAME_RE.sub("_", text).strip(" ._")
    if not text:
        text = default
    if len(text) > 120:
        stem = Path(text).stem[:90].strip(" ._") or default
        suffix = Path(text).suffix[:20]
        text = f"{stem}{suffix}"
    return text or default


def _safe_prefix(value: str) -> str:
    text = _SAFE_FILENAME_RE.sub("_", str(value or "download")).strip(" ._")
    return (text[:80].strip(" ._") or "download")


def _unique_output_path(platform: str, channel: str, filename: str) -> str:
    path = Path(build_output_path(platform, filename, channel=channel))
    if not path.exists():
        return str(path)
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return str(candidate)
    return str(path.with_name(f"{stem}_{int(time.time())}{suffix}"))


def _allowed_host(href: str, allowed_hosts: set[str]) -> bool:
    if not href or not allowed_hosts:
        return True
    parsed = urlparse(href)
    if not parsed.netloc:
        return True
    host = parsed.hostname or ""
    return host.lower() in allowed_hosts


def _looks_downloadable(text: str, href: str, download_attr: str, *, allow_media: bool = False) -> bool:
    text_value = str(text or "")
    href_value = str(href or "")
    download_value = str(download_attr or "")
    if not allow_media and (
        _MEDIA_EXT_RE.search(href_value)
        or _MEDIA_EXT_RE.search(download_value)
        or _MEDIA_CONTROL_RE.search(text_value)
    ):
        return False
    if download_value.strip():
        return True
    if _DOWNLOAD_EXT_RE.search(href_value):
        return True
    control_text = " ".join([text_value, download_value]).strip()
    if _DOWNLOAD_CN_RE.search(control_text):
        return True
    if _DOWNLOAD_TEXT_RE.search(control_text):
        return True
    return bool(_DOWNLOAD_DATA_PHRASE_RE.search(control_text))


def _is_export_menu_control(text: str, href: str = "", download_attr: str = "") -> bool:
    if href or download_attr:
        return False
    text_value = str(text or "").strip()
    return bool(text_value and _EXPORT_MENU_RE.search(text_value))


def find_visible_download_controls(
    page,
    *,
    allowed_hosts: list[str] | tuple[str, ...] | set[str] | None = None,
    max_controls: int = 20,
    allow_media: bool = False,
) -> list[dict[str, Any]]:
    """Return visible download/export-like controls already present in the page DOM."""
    try:
        controls = page.evaluate(
            """
            (maxControls) => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style && (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none')) return false;
                    const rect = el.getBoundingClientRect();
                    return rect && rect.width > 0 && rect.height > 0;
                };
                const clean = (text) => (text || '').replace(/\\s+/g, ' ').trim();
                const downloadText = /(^|[^a-z0-9])(download|export|csv|json|xlsx?|excel)([^a-z0-9]|$)/i;
                const downloadCn = /(导出|下载)/;
                const dataPhrase = /(^|[^a-z0-9])((download|export)\\s+data|data\\s+(download|export))([^a-z0-9]|$)/i;
                const ext = /\\.(csv|json|xlsx?|zip|txt)(?:$|[?#])/i;
                const mediaExt = /\\.(png|jpe?g|gif|webp|svg)(?:$|[?#])/i;
                const mediaText = /(^|[^a-z0-9])(image|png|jpe?g|gif|webp|svg)([^a-z0-9]|$)/i;
                const nodes = Array.from(document.querySelectorAll('a[href], button, [role="button"]'));
                const out = [];
                for (const el of nodes) {
                    if (!visible(el)) continue;
                    const tag = (el.tagName || '').toLowerCase();
                    const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                    const href = el.href || el.getAttribute('href') || '';
                    const download = el.getAttribute('download') || '';
                    const label = `${text} ${download}`;
                    if (!download && !ext.test(href) && !downloadCn.test(label) && !downloadText.test(label) && !dataPhrase.test(label)) continue;
                    if (mediaExt.test(href) || mediaExt.test(download) || mediaText.test(text)) continue;
                    const idx = out.length + 1;
                    el.setAttribute('data-zcode-download-index', String(idx));
                    out.push({ index: idx, tag, text, href, download, kind: 'direct' });
                    if (out.length >= maxControls) break;
                }
                return out;
            }
            """,
            max(1, int(max_controls or 1)),
        ) or []
    except Exception:
        return []

    allowed = {str(host or "").lower() for host in (allowed_hosts or []) if str(host or "").strip()}
    filtered: list[dict[str, Any]] = []
    for control in controls:
        href = str((control or {}).get("href") or "")
        text = str((control or {}).get("text") or "")
        download_attr = str((control or {}).get("download") or "")
        if not _looks_downloadable(text, href, download_attr, allow_media=allow_media):
            continue
        if not _allowed_host(href, allowed):
            control = dict(control or {})
            control["_skipped_reason"] = "控件链接不在允许站点范围内，已跳过。"
        filtered.append(control)
    return filtered


def find_visible_export_menu_controls(page, *, max_controls: int = 10) -> list[dict[str, Any]]:
    """Return visible chart/export menu buttons such as Highcharts context buttons."""
    try:
        controls = page.evaluate(
            """
            (maxControls) => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style && (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none')) return false;
                    const rect = el.getBoundingClientRect();
                    return rect && rect.width > 0 && rect.height > 0;
                };
                const clean = (text) => (text || '').replace(/\\s+/g, ' ').trim();
                const menuText = /(^|[^a-z0-9])(chart\\s+menu|export\\s+menu|context\\s+menu|view\\s+chart\\s+menu)([^a-z0-9]|$)/i;
                const selectors = [
                    '.highcharts-contextbutton',
                    '.highcharts-exporting-group .highcharts-button',
                    'button[aria-label*="Chart menu" i]',
                    'button[aria-label*="Export" i]',
                    '[role="button"][aria-label*="Chart menu" i]',
                    '[role="button"][aria-label*="Export" i]'
                ].join(',');
                const nodes = Array.from(document.querySelectorAll(selectors));
                for (const image of document.querySelectorAll('image.highcharts-button-symbol')) {
                    const href = image.getAttribute('href') || image.getAttribute('xlink:href') || '';
                    if (!/download/i.test(href)) continue;
                    const button = image.closest('.highcharts-contextbutton') || image.closest('.highcharts-exporting-group');
                    if (button && !nodes.includes(button)) nodes.push(button);
                }
                const out = [];
                for (const el of nodes) {
                    if (!visible(el)) continue;
                    const tag = (el.tagName || '').toLowerCase();
                    const titleText = clean(Array.from(el.querySelectorAll('title')).map((node) => node.textContent || '').join(' '));
                    const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || titleText || 'Chart menu');
                    const href = el.href || el.getAttribute('href') || '';
                    const download = el.getAttribute('download') || '';
                    if (href || download) continue;
                    if (!menuText.test(text) && !el.classList.contains('highcharts-contextbutton')) continue;
                    const idx = out.length + 1;
                    el.setAttribute('data-zcode-export-menu-index', String(idx));
                    out.push({ index: idx, tag, text, href, download, kind: 'menu' });
                    if (out.length >= maxControls) break;
                }
                return out;
            }
            """,
            max(1, int(max_controls or 1)),
        ) or []
    except Exception:
        return []
    return list(controls)


def _download_file_from_event(download, platform: str, channel: str, filename_prefix: str, control_index: int, suggested: str) -> tuple[str, str]:
    suggested = safe_download_filename(getattr(download, "suggested_filename", "") or suggested)
    prefix = _safe_prefix(filename_prefix)
    filename = safe_download_filename(f"{prefix}_{control_index}_{suggested}")
    if "." not in Path(filename).name:
        filename = f"{filename}.download"
    output_path = _unique_output_path(platform, channel, filename)
    download.save_as(output_path)
    return suggested, output_path


def _fallback_record(page_type: str, page_url: str, note: str) -> list[dict[str, Any]]:
    return [
        make_download_record(
            page_type=page_type,
            page_url=page_url,
            final_url=page_url,
            status="not_found",
            note=note,
        )
    ]


def _current_page_url(page) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def _save_download_row(
    page,
    *,
    download,
    platform: str,
    channel: str,
    filename_prefix: str,
    page_type: str,
    page_url: str,
    control_index: int,
    control_text: str,
    control_href: str = "",
    suggested: str = "download",
    note: str = "已点击页面可见下载/导出控件并保存文件。",
) -> dict[str, Any]:
    suggested, output_path = _download_file_from_event(
        download,
        platform,
        channel,
        filename_prefix,
        control_index,
        suggested,
    )
    return make_download_record(
        page_type=page_type,
        page_url=page_url,
        final_url=_current_page_url(page) or page_url,
        control_index=control_index,
        control_text=control_text,
        control_href=control_href,
        suggested_filename=suggested,
        saved_path=output_path,
        status="ok",
        note=note,
    )


def _click_export_menu_download(
    page,
    control: dict[str, Any],
    *,
    platform: str,
    channel: str,
    filename_prefix: str,
    page_type: str,
    page_url: str,
    timeout: int,
    allow_media: bool,
) -> dict[str, Any]:
    control_index = int((control or {}).get("index") or 0)
    control_text = str((control or {}).get("text") or "Chart menu")
    selector = f'[data-zcode-export-menu-index="{control_index}"]'
    try:
        page.locator(selector).first.click(timeout=min(5000, max(1000, int(timeout or 10000))))
        page.wait_for_timeout(300)
        item = page.evaluate(
            """
            () => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style && (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none')) return false;
                    const rect = el.getBoundingClientRect();
                    return rect && rect.width > 0 && rect.height > 0;
                };
                const clean = (text) => (text || '').replace(/\\s+/g, ' ').trim();
                const dataText = /(^|[^a-z0-9])((download\\s+)?(csv|xls|xlsx|json)(\\s+export)?|download\\s+(csv|xls|xlsx|json)|view\\s+data\\s+table)([^a-z0-9]|$)/i;
                const mediaText = /(^|[^a-z0-9])(png|jpeg|jpg|svg|image|pdf|print)([^a-z0-9]|$)/i;
                const nodes = Array.from(document.querySelectorAll('.highcharts-menu-item, li, button, [role="menuitem"], [role="button"]'));
                let idx = 0;
                for (const el of nodes) {
                    if (!visible(el)) continue;
                    const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                    if (!text || !dataText.test(text) || mediaText.test(text)) continue;
                    idx += 1;
                    el.setAttribute('data-zcode-export-menu-item-index', String(idx));
                    return { index: idx, text, href: el.href || el.getAttribute('href') || '' };
                }
                return null;
            }
            """
        )
        if not item:
            return make_download_record(
                page_type=page_type,
                page_url=page_url,
                final_url=_current_page_url(page) or page_url,
                control_index=control_index,
                control_text=control_text,
                status="not_found",
                note="已发现图表导出菜单，但菜单中未发现 CSV/XLS/JSON 等数据下载项；已回退到页面表格数据。",
            )
        item_index = int((item or {}).get("index") or 0)
        item_text = str((item or {}).get("text") or "")
        item_href = str((item or {}).get("href") or "")
        if re.search(r"(?<![a-z0-9])login\s+for\s+(?:csv|xls|xlsx|json)\s+export(?![a-z0-9])", item_text, flags=re.I):
            return make_download_record(
                page_type=page_type,
                page_url=page_url,
                final_url=_current_page_url(page) or page_url,
                control_index=control_index,
                control_text=f"{control_text} > {item_text}",
                control_href=item_href,
                status="skipped",
                note="图表存在 CSV/XLS/JSON 导出项，但当前页面要求登录后才能导出；已回退到页面表格数据。",
            )
        if not allow_media and _MEDIA_CONTROL_RE.search(item_text):
            return make_download_record(
                page_type=page_type,
                page_url=page_url,
                final_url=_current_page_url(page) or page_url,
                control_index=control_index,
                control_text=f"{control_text} > {item_text}",
                control_href=item_href,
                status="skipped",
                note="导出菜单项是图片/媒体导出，已跳过。",
            )
        with page.expect_download(timeout=max(1000, int(timeout or 10000))) as download_info:
            page.locator(f'[data-zcode-export-menu-item-index="{item_index}"]').first.click(timeout=min(5000, max(1000, int(timeout or 10000))))
        return _save_download_row(
            page,
            download=download_info.value,
            platform=platform,
            channel=channel,
            filename_prefix=filename_prefix,
            page_type=page_type,
            page_url=page_url,
            control_index=control_index,
            control_text=f"{control_text} > {item_text}",
            control_href=item_href,
            suggested=item_text or "download",
            note="已点击页面可见图表导出菜单并保存数据文件。",
        )
    except PlaywrightTimeoutError:
        return make_download_record(
            page_type=page_type,
            page_url=page_url,
            final_url=_current_page_url(page) or page_url,
            control_index=control_index,
            control_text=control_text,
            status="error",
            note="点击图表导出菜单后未触发浏览器下载事件，已回退到页面表格数据。",
        )
    except Exception as exc:
        return make_download_record(
            page_type=page_type,
            page_url=page_url,
            final_url=_current_page_url(page) or page_url,
            control_index=control_index,
            control_text=control_text,
            status="error",
            note=str(exc)[:500],
        )


def download_visible_page_data(
    page,
    *,
    platform: str,
    channel: str,
    filename_prefix: str,
    allowed_hosts: list[str] | tuple[str, ...] | set[str] | None = None,
    page_type: str = "",
    max_downloads: int = 1,
    timeout: int = 10000,
    allow_media: bool = False,
    log_callback=None,
) -> list[dict[str, Any]]:
    """Click visible download/export controls and save resulting files.

    The function only interacts with controls already visible in the browser page.
    It does not synthesize hidden endpoints or issue direct HTTP requests.
    """
    page_url = _current_page_url(page)
    controls = find_visible_download_controls(
        page,
        allowed_hosts=allowed_hosts,
        max_controls=max(1, int(max_downloads or 1)) * 5,
        allow_media=allow_media,
    )

    rows: list[dict[str, Any]] = []
    attempts = 0
    for control in controls:
        if attempts >= max(1, int(max_downloads or 1)):
            break
        control_index = int((control or {}).get("index") or 0)
        control_text = str((control or {}).get("text") or "")
        control_href = str((control or {}).get("href") or "")
        suggested = safe_download_filename(str((control or {}).get("download") or "") or control_href or "download")
        skipped_reason = str((control or {}).get("_skipped_reason") or "")
        if skipped_reason:
            rows.append(
                make_download_record(
                    page_type=page_type,
                    page_url=page_url,
                    final_url=page_url,
                    control_index=control_index,
                    control_text=control_text,
                    control_href=control_href,
                    suggested_filename=suggested,
                    status="skipped",
                    note=skipped_reason,
                )
            )
            continue

        attempts += 1
        selector = f'[data-zcode-download-index="{control_index}"]'
        try:
            with page.expect_download(timeout=max(1000, int(timeout or 10000))) as download_info:
                page.locator(selector).first.click(timeout=min(5000, max(1000, int(timeout or 10000))))
            rows.append(
                _save_download_row(
                    page,
                    download=download_info.value,
                    platform=platform,
                    channel=channel,
                    filename_prefix=filename_prefix,
                    page_type=page_type,
                    page_url=page_url,
                    control_index=control_index,
                    control_text=control_text,
                    control_href=control_href,
                    suggested=suggested,
                )
            )
        except PlaywrightTimeoutError:
            rows.append(
                make_download_record(
                    page_type=page_type,
                    page_url=page_url,
                    final_url=_current_page_url(page) or page_url,
                    control_index=control_index,
                    control_text=control_text,
                    control_href=control_href,
                    suggested_filename=suggested,
                    status="error",
                    note="点击可见控件后未触发浏览器下载事件，已回退到页面表格数据。",
                )
            )
        except Exception as exc:
            rows.append(
                make_download_record(
                    page_type=page_type,
                    page_url=page_url,
                    final_url=_current_page_url(page) or page_url,
                    control_index=control_index,
                    control_text=control_text,
                    control_href=control_href,
                    suggested_filename=suggested,
                    status="error",
                    note=str(exc)[:500],
                )
            )

    if attempts < max(1, int(max_downloads or 1)):
        menu_controls = find_visible_export_menu_controls(page, max_controls=max(1, int(max_downloads or 1)) * 3)
        for menu_control in menu_controls:
            if attempts >= max(1, int(max_downloads or 1)):
                break
            attempts += 1
            rows.append(
                _click_export_menu_download(
                    page,
                    menu_control,
                    platform=platform,
                    channel=channel,
                    filename_prefix=filename_prefix,
                    page_type=page_type,
                    page_url=page_url,
                    timeout=timeout,
                    allow_media=allow_media,
                )
            )

    return rows or _fallback_record(page_type, page_url, "未发现可见下载/导出控件，已回退到页面表格数据。")


__all__ = [
    "VISIBLE_DOWNLOAD_FIELDS",
    "download_visible_page_data",
    "find_visible_download_controls",
    "find_visible_export_menu_controls",
    "make_download_record",
    "safe_download_filename",
]
