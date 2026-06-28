import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.task_checkpoint import (
    load_tool_inputs,
    open_task_checkpoint,
    save_tool_inputs,
    task_fingerprint,
)
from src.core.xlsx import MultiSheetXlsxWriter, XlsxRowWriter


class TestTaskCheckpoint(unittest.TestCase):
    def test_fingerprint_is_stable_for_dict_order(self):
        left = task_fingerprint("tool", {"b": 2, "a": [1, 2]})
        right = task_fingerprint("tool", {"a": [1, 2], "b": 2})
        self.assertEqual(left, right)

    def test_save_and_load_tool_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.core.task_checkpoint.get_workspace_root", return_value=Path(tmp)):
                save_tool_inputs("tool_x", {"keywords": "a\nb", "limit_time": "是"})
                self.assertEqual(load_tool_inputs("tool_x")["keywords"], "a\nb")

    def test_checkpoint_marks_completed_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.core.task_checkpoint.get_workspace_root", return_value=Path(tmp)):
                checkpoint = open_task_checkpoint("tool_y", {"links": ["A", "B"]})
                self.assertFalse(checkpoint.is_completed("A"))
                checkpoint.mark_completed("A", {"row": 1})

                reloaded = open_task_checkpoint("tool_y", {"links": ["A", "B"]})
                self.assertTrue(reloaded.is_completed("a"))
                self.assertEqual(reloaded.completed_count(), 1)

    def test_xlsx_row_writer_can_append_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "rows.xlsx"
            writer = XlsxRowWriter(str(output_path), ["name", "count"])
            writer.writerow({"name": "one", "count": 1})
            writer.save()

            resumed = XlsxRowWriter(str(output_path), ["name", "count"], append=True)
            resumed.writerow({"name": "two", "count": 2})
            resumed.save()

            from openpyxl import load_workbook

            rows = list(load_workbook(output_path).active.iter_rows(values_only=True))
            self.assertEqual(rows, [("name", "count"), ("one", 1), ("two", 2)])

    def test_multi_sheet_writer_can_append_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "multi.xlsx"
            fields = {"profiles": ["url"], "works": ["url", "title"]}
            writer = MultiSheetXlsxWriter(str(output_path), fields)
            writer.writerow("profiles", {"url": "a"})
            writer.save()

            resumed = MultiSheetXlsxWriter(str(output_path), fields, append=True)
            resumed.writerow("works", {"url": "b", "title": "title"})
            resumed.save()

            from openpyxl import load_workbook

            workbook = load_workbook(output_path)
            self.assertEqual(list(workbook["profiles"].iter_rows(values_only=True)), [("url",), ("a",)])
            self.assertEqual(list(workbook["works"].iter_rows(values_only=True)), [("url", "title"), ("b", "title")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
