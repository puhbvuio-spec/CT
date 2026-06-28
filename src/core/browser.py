"""
浏览器控制模块，负责在 Windows 环境下查找、自动启动 Chrome / Edge 进程，并通过 Playwright
的 CDP (Chrome DevTools Protocol) 接口进行连接，支持持久化用户数据以保持登录态。

Edge 与 Chrome 同为 Chromium 内核，CDP 协议完全兼容，可由用户在全局配置里选择使用哪一个。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from src.core.app_logging import log_line

logger = logging.getLogger(__name__)

# 默认 CDP 调试接口地址
DEFAULT_X_CDP_URL = "http://localhost:9222"
DEFAULT_TIKTOK_CDP_URL = "http://localhost:9222"
DEFAULT_EDGE_CDP_URL = "http://localhost:9223"

# 浏览器类型标识
BROWSER_AUTO = "auto"
BROWSER_CHROME = "chrome"
BROWSER_EDGE = "edge"
SUPPORTED_BROWSERS = (BROWSER_AUTO, BROWSER_CHROME, BROWSER_EDGE)

# Chrome 默认安装路径
DEFAULT_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

# Edge 默认安装路径
DEFAULT_EDGE_PATHS = (
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

# 各浏览器的可执行文件名（用于 taskkill 与 LOCALAPPDATA 路径拼接）
_BROWSER_EXE_NAME = {
    BROWSER_CHROME: "chrome.exe",
    BROWSER_EDGE: "msedge.exe",
}
# 各浏览器在 LOCALAPPDATA 下的相对路径
_BROWSER_LOCAL_REL = {
    BROWSER_CHROME: ("Google", "Chrome", "Application", "chrome.exe"),
    BROWSER_EDGE: ("Microsoft", "Edge", "Application", "msedge.exe"),
}

# 保存自动拉起的浏览器子进程实例，以便在退出时进行清理
_chrome_processes: list[subprocess.Popen] = []


def _cleanup_chrome():
    """
    进程退出时的清理勾子，确保自动启动的浏览器进程被正确终止，避免后台残留。
    """
    global _chrome_processes
    for p in _chrome_processes:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    _chrome_processes.clear()


# 注册 Python 退出钩子
atexit.register(_cleanup_chrome)


def build_cdp_url(port_or_url: str | int) -> str:
    """
    将端口号或简写 URL 规范化为完整的 HTTP CDP 连接地址。

    Args:
        port_or_url: 端口号（如 9222）或已是完整地址

    Returns:
        str: 规范化的 HTTP 链接
    """
    value = str(port_or_url).strip()
    if not value:
        raise ValueError("CDP port or URL is required.")

    if value.startswith("http://") or value.startswith("https://"):
        return value

    return f"http://localhost:{value}"


def debug_port_from_cdp_url(port_or_url: str | int) -> str:
    """
    从给定的 CDP 端口或 URL 中解析提取出单纯的端口号。

    Args:
        port_or_url: 端口号或 CDP 链接

    Returns:
        str: 端口号或 netloc 串
    """
    cdp_url = build_cdp_url(port_or_url)
    parsed = urlparse(cdp_url)
    if parsed.port is not None:
        return str(parsed.port)
    return parsed.netloc or cdp_url


def cdp_url_for_browser(browser: str | None = None, default_url: str = DEFAULT_X_CDP_URL) -> str:
    """
    根据浏览器偏好返回推荐 CDP 地址。

    Chrome 沿用 9222；Edge 使用 9223，避免误连到已经触发风控的 Chrome 会话。
    browser 为 None/auto 时读取全局配置。
    """
    resolved_browser = _resolve_browser_preference(browser) if browser else _get_configured_browser()
    if resolved_browser == BROWSER_EDGE:
        return DEFAULT_EDGE_CDP_URL
    return default_url


def get_workspace_root():
    """
    获取工作空间根目录。采用延迟导入以避免与 output 模块产生循环引用。
    """
    from src.core.output import get_workspace_root

    return get_workspace_root()


def get_chrome_user_data_dir(browser: str = BROWSER_CHROME) -> str:
    """
    获取浏览器缓存及用户登录信息的存储路径。

    Chrome 与 Edge 的 profile 格式不完全兼容，分目录存放避免冲突：
        workspace/user_data/        -> Chrome（保持原路径以兼容老用户）
        workspace/user_data_edge/   -> Edge

    Args:
        browser: BROWSER_CHROME 或 BROWSER_EDGE

    Returns:
        str: 绝对路径字符串
    """
    dir_name = "user_data_edge" if browser == BROWSER_EDGE else "user_data"
    user_data_dir = get_workspace_root() / dir_name
    user_data_dir.mkdir(parents=True, exist_ok=True)
    return str(user_data_dir)


def _resolve_browser_preference(browser: str | None) -> str:
    """
    将用户传入的浏览器偏好规范化为具体的浏览器类型。

    "auto" / None / 空串 → 优先 Chrome（已安装时），否则 Edge；都没有时仍返回 Chrome
    （由 find_browser_executable 的退避兜底处理 PATH 查找）。
    """
    value = (browser or "").strip().lower() or BROWSER_AUTO
    if value == BROWSER_CHROME:
        return BROWSER_CHROME
    if value == BROWSER_EDGE:
        return BROWSER_EDGE
    # auto：依次探测 Chrome → Edge
    if _find_executable_for(BROWSER_CHROME) is not None:
        return BROWSER_CHROME
    if _find_executable_for(BROWSER_EDGE) is not None:
        return BROWSER_EDGE
    return BROWSER_CHROME


def _find_executable_for(browser: str) -> str | None:
    """
    在已知安装路径中查找指定浏览器的可执行文件，找不到返回 None。
    """
    paths = DEFAULT_EDGE_PATHS if browser == BROWSER_EDGE else DEFAULT_CHROME_PATHS
    for path in paths:
        if os.path.exists(path):
            return path
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        rel = _BROWSER_LOCAL_REL.get(browser)
        if rel:
            candidate = os.path.join(local_app_data, *rel)
            if os.path.exists(candidate):
                return candidate
    return None


def find_browser_executable(browser: str = BROWSER_CHROME) -> str:
    """
    自动查找系统中的浏览器可执行文件路径。

    Args:
        browser: BROWSER_CHROME 或 BROWSER_EDGE

    Returns:
        str: 找到的浏览器路径；均未找到时退避返回 exe 名（依赖 PATH 查找）
    """
    path = _find_executable_for(browser)
    if path is not None:
        return path
    return _BROWSER_EXE_NAME.get(browser, "chrome.exe")


def find_chrome_executable() -> str:
    """
    兼容旧调用的别名：默认查找 Chrome 可执行文件。新代码请使用 find_browser_executable。
    """
    return find_browser_executable(BROWSER_CHROME)


def chrome_launch_hint(port_or_url: str | int, browser: str = BROWSER_CHROME) -> str:
    """
    生成在命令行中手动启动浏览器的提示命令，方便用户排查 CDP 问题。
    """
    return (
        f'"{find_browser_executable(browser)}" '
        f"--remote-debugging-port={debug_port_from_cdp_url(port_or_url)} "
        "--remote-allow-origins=* "
        f'--user-data-dir="{get_chrome_user_data_dir(browser)}"'
    )


def is_cdp_available(port_or_url: str | int, timeout: float = 1.0) -> bool:
    """
    检测给定的 CDP 调试地址是否已经可用（通过请求 JSON 状态端点进行确认）。

    Args:
        port_or_url: CDP 端口或连接地址
        timeout: 超时时长（秒）

    Returns:
        bool: 是否可用
    """
    cdp_url = build_cdp_url(port_or_url).rstrip("/")
    try:
        with urlopen(f"{cdp_url}/json/version", timeout=timeout) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def _list_cdp_page_targets(port_or_url: str | int, timeout: float = 1.0) -> list[dict]:
    """
    查询 CDP `/json` 端点返回的 page 类型目标列表。
    用于判断 Chrome 是否已经具备可用的标签页，而不仅仅是 DevTools HTTP 服务就绪。
    """
    cdp_url = build_cdp_url(port_or_url).rstrip("/")
    try:
        with urlopen(f"{cdp_url}/json", timeout=timeout) as response:
            if response.status != 200:
                return []
            data = json.loads(response.read().decode("utf-8", "ignore"))
            if isinstance(data, list):
                return [t for t in data if isinstance(t, dict) and t.get("type") == "page"]
    except (OSError, ValueError):
        return []
    return []


def _wait_for_initial_page(port_or_url: str | int, timeout: float = 8.0, log_callback=None) -> bool:
    """
    轮询等待 Chrome 至少存在一个 page 目标。

    Chrome 冷启动时，DevTools HTTP 服务（`/json/version`）会比初始窗口更早可用，
    若在此时直接连接并新建页面进行 goto，容易撞上首启流程导致页面被关闭
    （表现为 `Page.goto: Target page, context or browser has been closed`）。
    等待首个 page 目标出现，可确保浏览器窗口已经完成基本初始化。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _list_cdp_page_targets(port_or_url, timeout=1.0):
            return True
        time.sleep(0.3)
    log_line(log_callback, "未能在预期时间内检测到浏览器初始页面，继续尝试连接...")
    return False


def _is_port_occupied(port_or_url: str | int, timeout: float = 1.0) -> bool:
    """
    检测端口是否被占用（不管是否为 CDP 服务）。
    Chrome 不带 --remote-debugging-port 运行时，/json/version 返回 400，
    说明端口被占用但不是 CDP 模式。

    Args:
        port_or_url: CDP 端口或连接地址
        timeout: 超时时长（秒）

    Returns:
        bool: 端口是否被占用
    """
    cdp_url = build_cdp_url(port_or_url).rstrip("/")
    try:
        with urlopen(f"{cdp_url}/json/version", timeout=timeout) as response:
            # 200 = CDP 可用, 其他状态码 = 端口被占用但非 CDP
            return True
    except (OSError, ValueError):
        return False


def launch_chrome_for_cdp(port_or_url: str | int, browser: str = BROWSER_CHROME) -> subprocess.Popen:
    """
    以子进程的方式在后台启动带有 CDP 调试端口的浏览器（Chrome 或 Edge）。

    Args:
        port_or_url: 调试端口
        browser: BROWSER_CHROME 或 BROWSER_EDGE

    Returns:
        subprocess.Popen: 启动的子进程实例
    """
    global _chrome_processes
    browser_path = find_browser_executable(browser)
    port = debug_port_from_cdp_url(port_or_url)
    user_data_dir = get_chrome_user_data_dir(browser)

    # 清除向浏览器传递的环境变量中的 HTTP_PROXY 以免影响 Playwright 浏览器
    chrome_env = os.environ.copy()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        chrome_env.pop(k, None)

    p = subprocess.Popen(
        [
            browser_path,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=chrome_env,
        # CREATE_NO_WINDOW 避免在 Windows GUI 界面下弹出黑色控制台闪窗
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _chrome_processes.append(p)
    return p


def _kill_chrome_on_port(port_or_url: str | int, log_callback=None, browser: str = BROWSER_CHROME) -> None:
    """
    尝试关闭占用指定端口的浏览器进程。
    当浏览器已在运行但未开启 CDP 调试端口时，需要先关闭再重新以 CDP 模式启动。

    Args:
        port_or_url: CDP 端口或连接地址
        log_callback: 日志回调
        browser: 要关闭的浏览器类型
    """
    port = debug_port_from_cdp_url(port_or_url)
    exe_name = _BROWSER_EXE_NAME.get(browser, "chrome.exe")
    log_line(log_callback, f"端口 {port} 被占用但非 CDP 模式，尝试关闭现有 {exe_name}...")
    try:
        # Windows: 通过 taskkill 终止对应浏览器进程
        subprocess.run(
            ["taskkill", "/F", "/IM", exe_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # 等待进程完全退出
        time.sleep(1.0)
    except Exception:
        pass


def _get_configured_browser() -> str:
    """
    从全局配置读取用户选择的浏览器，返回规范化的浏览器类型（chrome 或 edge）。
    延迟导入避免循环引用。失败时退回 auto 探测。
    """
    try:
        from src.core.config_store import GLOBAL_CONFIG_DEFAULTS, GLOBAL_TOOL_ID, load_config

        cfg = load_config(GLOBAL_TOOL_ID, GLOBAL_CONFIG_DEFAULTS, None)
        return _resolve_browser_preference(cfg.get("browser"))
    except Exception:
        return _resolve_browser_preference(BROWSER_AUTO)


def ensure_chrome_for_cdp(port_or_url: str | int, log_callback=None, wait_seconds: float = 20.0, browser: str | None = None) -> bool:
    """
    确保浏览器（Chrome 或 Edge）CDP 调试端点已就绪。
    如果未就绪则自动拉起后台浏览器，并循环等待其加载就绪。

    Args:
        port_or_url: 端口或路径
        log_callback: 接收状态消息的日志回调函数
        wait_seconds: 最长等待就绪的超时时长（秒）
        browser: 显式指定浏览器类型（chrome/edge/auto）；为 None 则读全局配置

    Returns:
        bool: 本次调用是否主动拉起了浏览器（用于上层决定是否做额外预热）
    """
    # 已有就绪的 CDP 服务时，仍确认初始页面存在，避免连接到刚启动但未完成初始化的实例
    if is_cdp_available(port_or_url):
        _wait_for_initial_page(port_or_url, timeout=5.0, log_callback=log_callback)
        return False

    # 选定要启动/复用的浏览器类型（按用户偏好）
    if browser:
        resolved_browser = _resolve_browser_preference(browser)
    elif debug_port_from_cdp_url(port_or_url) == debug_port_from_cdp_url(DEFAULT_EDGE_CDP_URL):
        resolved_browser = BROWSER_EDGE
    else:
        resolved_browser = _get_configured_browser()
    browser_display = "Edge" if resolved_browser == BROWSER_EDGE else "Chrome"

    # 检测端口是否被非 CDP 模式的浏览器占用（返回 400）
    if _is_port_occupied(port_or_url):
        _kill_chrome_on_port(port_or_url, log_callback, browser=resolved_browser)

    log_line(log_callback, f"未检测到浏览器，正在自动启动 {browser_display}...")
    launch_chrome_for_cdp(port_or_url, browser=resolved_browser)
    launched = True

    # 循环检查 CDP 可用性，设定上限是考虑到老旧机器上浏览器冷启动的延迟时间
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_cdp_available(port_or_url):
            # DevTools HTTP 服务已就绪，再等待首个 page 目标出现，确保窗口初始化完成
            if _wait_for_initial_page(port_or_url, timeout=10.0, log_callback=log_callback):
                # 首次拉起后给浏览器一段缓冲，让默认上下文与首启流程彻底稳定
                log_line(log_callback, "浏览器已就绪，等待初始窗口稳定...")
                time.sleep(2.5)
            return launched
        # 如果端口仍被非 CDP 的进程占用，再次尝试关闭
        if _is_port_occupied(port_or_url):
            _kill_chrome_on_port(port_or_url, log_callback, browser=resolved_browser)
            launch_chrome_for_cdp(port_or_url, browser=resolved_browser)
        # 每隔 0.4 秒检查一次，平衡响应灵敏度与无用轮询开销
        time.sleep(0.4)

    raise RuntimeError(
        f"{browser_display} 未能在 {wait_seconds}s 内启动在端口 {debug_port_from_cdp_url(port_or_url)}。"
        f"请检查 {browser_display} 是否已安装且未被阻止。"
    )


def _warmup_context(context, log_callback=None, attempts: int = 3) -> bool:
    """
    用一次轻量导航预热浏览器上下文，确认其能够正常打开并导航页面。

    冷启动时 Chrome 的首启流程偶发会关闭新建的 page 目标，导致首个业务页 goto
    报 `Target page, context or browser has been closed`。这里通过一个
    about:blank 的临时页面提前暴露并消化该问题，避免影响真正的业务页面。
    """
    for i in range(attempts):
        page = None
        try:
            page = context.new_page()
            page.goto("about:blank", wait_until="load", timeout=8000)
            return True
        except Exception:
            log_line(log_callback, f"浏览器预热中，等待初始窗口稳定（{i + 1}/{attempts}）...")
            time.sleep(1.5)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
    log_line(log_callback, "浏览器预热未成功，仍尝试继续执行业务流程。")
    return False


def connect_existing_chromium(
    playwright: Any,
    port_or_url: str | int,
    *,
    context_index: int = 0,
    log_callback=None,
    warmup: bool = True,
    browser: str | None = None,
):
    """
    拉起（或确认）浏览器调试端口后，通过 Playwright 连接已有的 Chromium 实例。
    重用现有的上下文以避免清理已登录的会话，保留用户的 Cookies 和 Session。

    Args:
        playwright: sync_playwright 实例
        port_or_url: 端口或链接
        context_index: 获取哪一个已有的 browser context（通常为 0）
        log_callback: 日志输出回调
        warmup: 是否在返回前做一次轻量导航预热（默认开启）

    Returns:
        (browser, context): Playwright 的 browser 和 context 实例
    """
    launched = ensure_chrome_for_cdp(port_or_url, log_callback=log_callback, browser=browser)
    cdp_url = build_cdp_url(port_or_url)
    browser = playwright.chromium.connect_over_cdp(cdp_url)

    # 冷启动时 contexts 可能短暂为空，等待默认上下文就绪后再复用
    deadline = time.time() + 10.0
    while time.time() < deadline and len(browser.contexts) <= context_index:
        time.sleep(0.3)
    contexts = browser.contexts
    # 如果浏览器已有 Context 则直接复用以继承 Session 状态，否则新建一个
    context = contexts[context_index] if len(contexts) > context_index else browser.new_context()

    # 仅在本次主动拉起 Chrome 时做预热，避免对已长期运行的浏览器做多余操作
    if warmup and launched:
        _warmup_context(context, log_callback=log_callback)
    return browser, context


def _is_page_closed(page) -> bool:
    """
    检测 Playwright page 是否已关闭或底层连接已断。
    page.is_closed() 仅反映主动关闭；额外用 page.url 触发一次底层通信，
    可探测出因浏览器崩溃/重启导致的连接已断。
    """
    if page is None:
        return True
    try:
        if page.is_closed():
            return True
        # 触发一次底层通信；连接已断时会抛异常
        _ = page.url
        return False
    except Exception:
        return True


def _recreate_page(context, old_page):
    """
    关闭旧 page（若仍可关）并在给定 context 上新建一个 page。
    用于 page 被目标站点风控关闭后的自动恢复。
    """
    if old_page is not None:
        try:
            if not old_page.is_closed():
                old_page.close()
        except Exception:
            pass
    return context.new_page()
