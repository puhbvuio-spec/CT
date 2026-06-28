import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core import browser


class TestBrowserReopen(unittest.TestCase):
    def test_ensure_page_target_opens_blank_when_cdp_has_no_pages(self):
        with (
            patch("src.core.browser._wait_for_initial_page", side_effect=[False, True]) as mock_wait,
            patch("src.core.browser._open_cdp_page_target", return_value=True) as mock_open,
        ):
            self.assertTrue(browser._ensure_cdp_page_target("9222", timeout=0.1))

        self.assertEqual(mock_wait.call_count, 2)
        mock_open.assert_called_once_with("9222")

    def test_existing_cdp_with_page_is_reused(self):
        with (
            patch("src.core.browser.is_cdp_available", return_value=True),
            patch("src.core.browser._ensure_cdp_page_target", return_value=True) as mock_ensure,
            patch("src.core.browser._kill_chrome_on_port") as mock_kill,
            patch("src.core.browser.launch_chrome_for_cdp") as mock_launch,
        ):
            launched = browser.ensure_chrome_for_cdp("9222", browser=browser.BROWSER_CHROME)

        self.assertFalse(launched)
        mock_ensure.assert_called_once()
        mock_kill.assert_not_called()
        mock_launch.assert_not_called()

    def test_cdp_without_page_restarts_browser_when_blank_page_cannot_open(self):
        with (
            patch("src.core.browser.is_cdp_available", side_effect=[True, True]),
            patch("src.core.browser._ensure_cdp_page_target", side_effect=[False, True]),
            patch("src.core.browser._is_port_occupied", return_value=False),
            patch("src.core.browser._kill_chrome_on_port") as mock_kill,
            patch("src.core.browser.launch_chrome_for_cdp") as mock_launch,
            patch("src.core.browser.time.sleep"),
        ):
            launched = browser.ensure_chrome_for_cdp("9222", browser=browser.BROWSER_CHROME, wait_seconds=1)

        self.assertTrue(launched)
        mock_kill.assert_called_once()
        mock_launch.assert_called_once_with("9222", browser=browser.BROWSER_CHROME)


if __name__ == "__main__":
    unittest.main(verbosity=2)
