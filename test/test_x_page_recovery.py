import unittest

from src.platforms.x_twitter.page_recovery import (
    XPageRecoveryConfig,
    is_x_transient_error_text,
    resolve_x_page_recovery_config,
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

    def test_configurable_wait_values(self):
        config = resolve_x_page_recovery_config(
            {
                "x_recovery_wait_1": 10,
                "x_recovery_wait_2": 20,
                "x_recovery_wait_later": 30,
                "x_network_check_enabled": "否",
                "x_network_check_timeout": 3,
                "x_network_issue_wait": 40,
            }
        )

        self.assertEqual(config.backoff_seconds, (10.0, 20.0, 30.0))
        self.assertFalse(config.network_check_enabled)
        self.assertEqual(config.network_check_timeout, 3.0)
        self.assertEqual(config.network_issue_wait_seconds, 40.0)

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
            recovery_config={"x_network_check_enabled": False},
        )

        self.assertTrue(ok)
        self.assertEqual(page.reload_count, 1)
        self.assertTrue(page.selector_waits)
        self.assertTrue(any("临时错误" in message for message in messages))
        self.assertTrue(any("已恢复正常" in message for message in messages))

    def test_network_problem_uses_network_wait_before_x_backoff(self):
        page = FakePage(
            [
                "Something went wrong. Try reloading.",
                "Recovered profile content",
            ]
        )
        messages = []
        network_checks = []

        def fake_network_checker(config: XPageRecoveryConfig):
            network_checks.append(config.network_check_url)
            return False, "timed out"

        ok = wait_for_x_page_recovery(
            page,
            log_callback=messages.append,
            page_timeout=100,
            context_label="测试页",
            recovery_config={
                "x_recovery_wait_1": 999,
                "x_network_issue_wait": 0,
                "x_network_check_url": "https://www.youtube.com/generate_204",
            },
            network_checker=fake_network_checker,
        )

        self.assertTrue(ok)
        self.assertEqual(page.reload_count, 1)
        self.assertEqual(network_checks, ["https://www.youtube.com/generate_204"])
        self.assertTrue(any("网络检测失败" in message for message in messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
