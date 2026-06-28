import unittest

from src.platforms.x_twitter.page_recovery import (
    is_x_transient_error_text,
    wait_for_x_page_recovery,
)


class FakePage:
    def __init__(self, texts):
        self.texts = list(texts)
        self.reload_count = 0
        self.selector_waits = []

    def evaluate(self, _script):
        index = min(self.reload_count, len(self.texts) - 1)
        return self.texts[index]

    def reload(self, wait_until=None, timeout=None):
        self.reload_count += 1

    def wait_for_selector(self, selector, timeout=None):
        self.selector_waits.append((selector, timeout))


class TestXPageRecovery(unittest.TestCase):
    def test_detects_japanese_reload_error_text(self):
        text = "問題が発生しました。 再読み込みしてください。"
        self.assertEqual(is_x_transient_error_text(text), "問題が発生しました")

    def test_waits_reloads_and_returns_after_recovery(self):
        page = FakePage(
            [
                "問題が発生しました。再読み込みしてください。 やりなおす",
                "Nintendo Everything @NinEverything Latest Nintendo updates",
            ]
        )
        messages = []

        ok = wait_for_x_page_recovery(
            page,
            log_callback=messages.append,
            page_timeout=100,
            context_label="测试页",
            backoff_seconds=(0,),
        )

        self.assertTrue(ok)
        self.assertEqual(page.reload_count, 1)
        self.assertTrue(page.selector_waits)
        self.assertTrue(any("临时错误" in message for message in messages))
        self.assertTrue(any("已恢复正常" in message for message in messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
