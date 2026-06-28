"""Run X keyword scraper end-to-end and verify output has data."""
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

import threading
from src.platforms.x_twitter.keyword import run_x_spider

output_path_result = []
stop_event = threading.Event()
pause_event = threading.Event()
finish_lock = threading.Lock()

def log(msg):
    print(msg)

def finish_callback(path):
    with finish_lock:
        if path:
            output_path_result.append(path)
            print(f"\n[FINISH] 输出文件: {path}")
        else:
            print("\n[FINISH] 无输出文件")

# Run with 2 keywords, no comments for speed
keywords = ["AI"]
adv_params = {"get_comments": "否"}
config = {
    "max_parallel_tabs": 1,
    "max_scrolls": 5,
    "slice_days": 1,
    "no_new_scroll_limit": 2,
    "search_page_timeout": 40000,
    "cooldown_min": 3.0,
    "cooldown_max": 5.0,
    "search_retry_count": 1,
    "search_retry_cooldown_min": 2.0,
    "search_retry_cooldown_max": 3.0,
}

print("=== 直接运行 X 关键词搜索: AI ===\n")
run_x_spider(
    keywords_list=keywords,
    adv_params=adv_params,
    port=9222,
    log_callback=log,
    finish_callback=finish_callback,
    stop_event=stop_event,
    config=config,
    pause_event=pause_event,
)

# Check output
if output_path_result:
    import pandas as pd
    path = output_path_result[0]
    print(f"\n=== 验证输出文件: {path} ===")
    xl = pd.ExcelFile(path)
    for sheet in xl.sheet_names:
        df = pd.read_excel(path, sheet_name=sheet)
        print(f"  [{sheet}] 行数={len(df)}")
        if len(df) > 0 and '类型' in df.columns:
            print(f"  类型分布: {df['类型'].value_counts().to_dict()}")
    if all(len(pd.read_excel(path, sheet_name=s)) > 0 for s in xl.sheet_names if s == '推文信息'):
        print("\n✅ 成功！输出文件有数据。")
    else:
        print("\n❌ 失败！推文信息 sheet 空。")
else:
    print("\n❌ 无输出文件生成！")
