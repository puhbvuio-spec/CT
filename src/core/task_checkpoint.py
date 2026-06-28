"""Task input cache and lightweight checkpoint helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from src.core.app_state import get_app_state_root
from src.core.output import get_workspace_root


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value))[:80] or "task"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def normalize_lines(value: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(value, str):
        raw_items = value.splitlines()
    else:
        raw_items = list(value or [])
    return [str(item).strip() for item in raw_items if str(item).strip()]


def task_fingerprint(tool_id: str, scope: dict[str, Any]) -> str:
    payload = {"tool_id": tool_id, "scope": _jsonable(scope)}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


_MIGRATED_CHECKPOINT_PAIRS: set[tuple[str, str]] = set()


def _copy_missing_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def checkpoint_root() -> Path:
    override = os.environ.get("SCRAPER_CHECKPOINT_DIR")
    root = Path(override).expanduser() if override else get_app_state_root() / "checkpoints"
    legacy_root = get_workspace_root() / "output" / "checkpoints"
    if not _same_path(root, legacy_root):
        pair = (str(legacy_root.absolute()).lower(), str(root.absolute()).lower())
        if pair not in _MIGRATED_CHECKPOINT_PAIRS:
            _copy_missing_tree(legacy_root, root)
            _MIGRATED_CHECKPOINT_PAIRS.add(pair)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save_tool_inputs(tool_id: str, values: dict[str, Any]) -> None:
    if not tool_id:
        return
    path = checkpoint_root() / "last_inputs" / f"{_safe_name(tool_id)}.json"
    _atomic_write(
        path,
        {
            "tool_id": tool_id,
            "updated_at": _now(),
            "values": _jsonable(values),
        },
    )


def load_tool_inputs(tool_id: str) -> dict[str, Any]:
    if not tool_id:
        return {}
    path = checkpoint_root() / "last_inputs" / f"{_safe_name(tool_id)}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    values = data.get("values", {})
    return values if isinstance(values, dict) else {}


class TaskCheckpoint:
    def __init__(self, tool_id: str, scope: dict[str, Any]):
        self.tool_id = tool_id
        self.scope = _jsonable(scope)
        self.fingerprint = task_fingerprint(tool_id, self.scope)
        self.path = checkpoint_root() / "tasks" / _safe_name(tool_id) / f"{self.fingerprint}.json"
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("completed", {})
                    return data
            except Exception:
                pass
        return {
            "tool_id": self.tool_id,
            "fingerprint": self.fingerprint,
            "scope": self.scope,
            "created_at": _now(),
            "updated_at": _now(),
            "completed": {},
            "output_paths": [],
        }

    @property
    def completed(self) -> dict[str, Any]:
        completed = self.data.setdefault("completed", {})
        return completed if isinstance(completed, dict) else {}

    def completed_count(self) -> int:
        return len(self.completed)

    def is_completed(self, key: str) -> bool:
        return str(key).strip().lower() in self.completed

    def is_successfully_completed(self, key: str, positive_count_fields: tuple[str, ...] = ()) -> bool:
        entry = self.completed.get(str(key).strip().lower())
        if not isinstance(entry, dict):
            return False
        if entry.get("status") == "completed":
            return True
        meta = entry.get("meta", {})
        if positive_count_fields and isinstance(meta, dict):
            for field in positive_count_fields:
                try:
                    if int(meta.get(field, 0) or 0) > 0:
                        return True
                except (TypeError, ValueError):
                    continue
            return False
        return bool(entry.get("completed_at"))

    def mark_completed(self, key: str, meta: dict[str, Any] | None = None) -> None:
        normalized = str(key).strip().lower()
        if not normalized:
            return
        self.completed[normalized] = {
            "status": "completed",
            "completed_at": _now(),
            "meta": _jsonable(meta or {}),
        }
        self.save()

    def add_output_path(self, output_path: str | None) -> None:
        if not output_path:
            return
        paths = self.data.setdefault("output_paths", [])
        if output_path not in paths:
            paths.append(output_path)
            self.save()

    def latest_output_path(self) -> str | None:
        paths = self.data.get("output_paths", [])
        if not isinstance(paths, list):
            return None
        for output_path in reversed(paths):
            if output_path and Path(str(output_path)).exists():
                return str(output_path)
        return None

    def merge_compatible_siblings(self, list_keys: tuple[str, ...], keep_keys: tuple[str, ...] = ()) -> int:
        if not list_keys:
            return 0
        merged_count = 0
        changed = False
        best_completed_count = self.completed_count()
        best_output_paths: list[str] = []
        paths = self.data.setdefault("output_paths", [])
        if not isinstance(paths, list):
            paths = []
            self.data["output_paths"] = paths

        for sibling_path in self.path.parent.glob("*.json"):
            if sibling_path == self.path:
                continue
            try:
                sibling = json.loads(sibling_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(sibling, dict):
                continue
            if not self._compatible_scope(sibling.get("scope", {}), list_keys, keep_keys):
                continue

            sibling_completed = sibling.get("completed", {})
            if not isinstance(sibling_completed, dict):
                continue
            for key, entry in sibling_completed.items():
                if key not in self.completed:
                    self.completed[key] = entry
                    merged_count += 1
                    changed = True

            sibling_paths = [str(path) for path in sibling.get("output_paths", []) if path]
            for output_path in sibling_paths:
                if output_path not in paths:
                    paths.append(output_path)
                    changed = True
            if len(sibling_completed) > best_completed_count and sibling_paths:
                best_completed_count = len(sibling_completed)
                best_output_paths = sibling_paths

        if best_output_paths:
            ordered_paths = [path for path in paths if path not in best_output_paths] + best_output_paths
            if ordered_paths != self.data.get("output_paths"):
                self.data["output_paths"] = ordered_paths
                changed = True
        if changed:
            self.save()
        return merged_count

    def _compatible_scope(self, other_scope: dict[str, Any], list_keys: tuple[str, ...], keep_keys: tuple[str, ...]) -> bool:
        if not isinstance(other_scope, dict):
            return False
        matched_list = False
        for key in list_keys:
            current_value = self.scope.get(key)
            other_value = other_scope.get(key)
            if isinstance(current_value, list) and isinstance(other_value, list):
                if current_value != other_value:
                    return False
                matched_list = True
        if not matched_list:
            return False
        for key in keep_keys:
            if key in self.scope and key in other_scope and self.scope.get(key) != other_scope.get(key):
                return False
        return True

    def save(self) -> None:
        self.data["updated_at"] = _now()
        _atomic_write(self.path, self.data)


def open_task_checkpoint(
    tool_id: str,
    scope: dict[str, Any],
    log_callback=None,
    merge_on_keys: tuple[str, ...] = (),
    merge_keep_keys: tuple[str, ...] = (),
) -> TaskCheckpoint:
    checkpoint = TaskCheckpoint(tool_id, scope)
    merged_count = checkpoint.merge_compatible_siblings(merge_on_keys, merge_keep_keys) if merge_on_keys else 0
    if checkpoint.completed_count():
        try:
            scope_note = _scope_count_note(checkpoint.scope)
            log_callback(
                f"断点续跑：本次输入{scope_note}，已加载 {checkpoint.completed_count()} 条历史断点记录；"
                "只会跳过确认成功的项。"
            )
            if merged_count:
                log_callback(f"断点续跑：已从旧参数任务合并 {merged_count} 条历史记录。")
        except Exception:
            pass
    return checkpoint


def _scope_count_note(scope: dict[str, Any]) -> str:
    labels = (
        ("profile_urls", "博主链接"),
        ("links", "链接"),
        ("keywords", "关键词"),
    )
    for key, label in labels:
        value = scope.get(key) if isinstance(scope, dict) else None
        if isinstance(value, list):
            return f" {len(value)} 条{label}"
    return ""


def open_checkpointed_row_writer(
    checkpoint: TaskCheckpoint,
    default_output_path: str,
    fieldnames,
    log_callback=None,
    writer_class=None,
    **kwargs,
):
    if writer_class is None:
        from src.core.xlsx import XlsxRowWriter as writer_class

    resume_path = checkpoint.latest_output_path()
    if resume_path:
        try:
            writer = writer_class(resume_path, fieldnames, append=True, **kwargs)
            _log_resume_output(log_callback, resume_path)
            return resume_path, writer
        except Exception as exc:
            _log_new_output(log_callback, exc)
    writer = writer_class(default_output_path, fieldnames, **kwargs)
    return default_output_path, writer


def open_checkpointed_multi_sheet_writer(
    checkpoint: TaskCheckpoint,
    default_output_path: str,
    sheets_fields,
    log_callback=None,
    **kwargs,
):
    from src.core.xlsx import MultiSheetXlsxWriter

    resume_path = checkpoint.latest_output_path()
    if resume_path:
        try:
            writer = MultiSheetXlsxWriter(resume_path, sheets_fields, append=True, **kwargs)
            _log_resume_output(log_callback, resume_path)
            return resume_path, writer
        except Exception as exc:
            _log_new_output(log_callback, exc)
    writer = MultiSheetXlsxWriter(default_output_path, sheets_fields, **kwargs)
    return default_output_path, writer


def _log_resume_output(log_callback, output_path: str) -> None:
    try:
        log_callback(f"断点续跑：继续写入上次输出文件：{output_path}")
    except Exception:
        pass


def _log_new_output(log_callback, exc: Exception) -> None:
    try:
        log_callback(f"断点续跑：上次输出文件无法追加，将新建输出文件。原因：{exc}")
    except Exception:
        pass
