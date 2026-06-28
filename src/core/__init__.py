"""
核心基础工具包，提供配置、浏览器 CDP 连接、文件输出、通用数值及 CSV/Excel 格式化工具。
"""

from src.core.app_logging import get_logger, log_error, log_line, log_warn, make_keyword_log, setup_console_logging
from src.core.app_state import get_app_state_root
from src.core.config_store import (
    GLOBAL_ALIAS_MAP,
    GLOBAL_CONFIG_DEFAULTS,
    GLOBAL_CONFIG_PARAMS,
    GLOBAL_TOOL_ID,
    generate_all_defaults,
    get_config_path,
    load_config,
    save_config,
)
from src.core.browser import (
    DEFAULT_EDGE_CDP_URL,
    DEFAULT_TIKTOK_CDP_URL,
    DEFAULT_X_CDP_URL,
    _is_page_closed,
    _recreate_page,
    cdp_url_for_browser,
    connect_existing_chromium,
    debug_port_from_cdp_url,
    ensure_chrome_for_cdp,
)
from src.core.tiktok_metadata import (
    extract_tiktok_video_title,
    resolve_tiktok_card_container,
)
from src.core.output import build_output_path, derive_run_date_from_filename, get_output_root, get_platform_output_dir, get_workspace_root
from src.core.timing import interruptible_random_sleep, interruptible_sleep, random_cooldown, should_stop, wait_if_paused
from src.core.number_format import expand_compact_number
from src.core.csv_utils import sanitize_csv_cell, sanitize_csv_row, sanitize_csv_rows
from src.core.xlsx import XlsxRowWriter, sanitize_xlsx_cell, MultiSheetXlsxWriter
from src.core.xlsx_summary import summarize_outputs

__all__ = [
    "GLOBAL_ALIAS_MAP",
    "GLOBAL_CONFIG_DEFAULTS",
    "GLOBAL_CONFIG_PARAMS",
    "GLOBAL_TOOL_ID",
    "get_app_state_root",
    "generate_all_defaults",
    "get_config_path",
    "load_config",
    "save_config",
    "DEFAULT_TIKTOK_CDP_URL",
    "DEFAULT_X_CDP_URL",
    "DEFAULT_EDGE_CDP_URL",
    "_is_page_closed",
    "_recreate_page",
    "build_output_path",
    "derive_run_date_from_filename",
    "connect_existing_chromium",
    "cdp_url_for_browser",
    "debug_port_from_cdp_url",
    "ensure_chrome_for_cdp",
    "get_output_root",
    "get_platform_output_dir",
    "get_workspace_root",
    "extract_tiktok_video_title",
    "resolve_tiktok_card_container",
    "interruptible_random_sleep",
    "interruptible_sleep",
    "random_cooldown",
    "should_stop",
    "wait_if_paused",
    "expand_compact_number",
    "sanitize_csv_cell",
    "sanitize_csv_row",
    "sanitize_csv_rows",
    "sanitize_xlsx_cell",
    "XlsxRowWriter",
    "MultiSheetXlsxWriter",
    "summarize_outputs",
    "log_line",
    "log_warn",
    "make_keyword_log",
    "log_error",
    "get_logger",
    "setup_console_logging",
]
