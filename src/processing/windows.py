from __future__ import annotations

import time
from pathlib import Path

from src.core import build_output_path
from src.judge_aigc.config import config as aigc_config
from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


class JudgeAIGCWindow(SimpleToolWindow):
    tool_id = "judge_aigc"

    def __init__(self) -> None:
        super().__init__(
            "AIGC 内容判断",
            [
                FieldSpec("input_path", "输入内容，每行一条", kind="text_or_file", required=True, placeholder="序号 标题"),
                FieldSpec("row_limit", "每批行数", kind="int", default=aigc_config.ROW_LIMIT, minimum=1, maximum=100000),
                FieldSpec("max_workers", "当前批 AI 并发数", kind="int", default=3, minimum=1, maximum=100),
                FieldSpec("save_every_batches", "每几批保存一次", kind="int", default=aigc_config.SAVE_EVERY_BATCHES, minimum=1, maximum=100000),
            ],
            height=600,
        )

    def tool_config_params(self):
        return [
            ConfigParam("temperature", "AI 温度 (0-2)", kind="float", default=aigc_config.TEMPERATURE, minimum=0.0, maximum=2.0, step=0.1, decimals=1),
            ConfigParam("sleep_seconds", "批次间隔(秒)", kind="float", default=aigc_config.SLEEP_SECONDS, minimum=0.1, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("trust_local_negative_aigc", "信任本地非AIGC判断", kind="bool", default=aigc_config.TRUST_LOCAL_NEGATIVE_AIGC),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.processing.judge_aigc import judge

        output_path = build_output_path("data", f"judge_aigc_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="aigc")
        log_callback(f"输出文件：{output_path}")
        if stop_event and stop_event.is_set():
            finish_callback(None)
            return None
        config = {k: v for k, v in values.items() if k in ("temperature", "sleep_seconds", "trust_local_negative_aigc")}
        judge(
            self._text_to_tempfile(values["input_path"], prefix="aigc_input"),
            output_path,
            row_limit=int(values["row_limit"]),
            max_workers=int(values["max_workers"]),
            save_every_batches=int(values["save_every_batches"]),
            log_callback=log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
            config_overrides=config,
        )
        log_callback(f"完成，已保存：{output_path}")
        finish_callback(output_path)
        return output_path


class XlsxMergeWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "XLSX 文件合并",
            [
                FieldSpec("folder", "XLSX 文件夹", kind="folder", required=True),
                FieldSpec("platform", "平台前缀", kind="combo", default="tiktok", options=("youtube", "tiktok", "x")),
                FieldSpec("keyword", "文件名包含", default="keyword", required=True),
            ],
            height=600,
        )

    def validate_values(self, values):
        if not Path(values["folder"]).exists():
            raise ValueError("文件夹不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.processing.xlsx_merge import merge_xlsx_files

        log_callback(f"合并文件夹：{values['folder']}")
        log_callback(f"平台前缀：{values['platform']}")
        log_callback(f"文件名关键词：{values['keyword']}")
        if stop_event and stop_event.is_set():
            finish_callback(None)
            return None
        output_path, file_count, row_count = merge_xlsx_files(values["folder"], values["keyword"], values["platform"])
        log_callback(f"完成：合并 {file_count} 个文件，{row_count} 行。")
        log_callback(f"输出文件：{output_path}")
        finish_callback(output_path)
        return output_path

class AnomalyDetectionWindow(SimpleToolWindow):
    tool_id = "processing_anomaly_detection"

    def __init__(self) -> None:
        super().__init__(
            "数据异常分析检测",
            [
                FieldSpec("input_xlsx", "输入 Excel 文件", kind="file", required=True, placeholder="选择需检测的 .xlsx 文件"),
            ],
            height=600,
        )

    def tool_config_params(self):
        return [
            ConfigParam("high_view_threshold", "高浏览量阈值", kind="int", default=1000, minimum=1, maximum=100000000),
            ConfigParam("high_like_threshold", "高点赞阈值", kind="int", default=50, minimum=1, maximum=100000000),
            ConfigParam("abnormal_ratio_multiplier", "失调倍数", kind="float", default=2.0, minimum=1.0, maximum=100.0, step=0.1, decimals=1),
            ConfigParam("abnormal_ratio_min_trigger", "失调最小转发触发数", kind="int", default=5, minimum=1, maximum=10000),
            ConfigParam("strict_zero_check", "严格 0 值矛盾检测", kind="bool", default=True),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.processing.anomaly_detection import run_anomaly_detection

        return run_anomaly_detection(values, self.config_values, log_callback, finish_callback, stop_event, pause_event)
