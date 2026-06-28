import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.platforms.x_twitter.profile_tweets import (
    DEFAULT_PROFILE_TWEET_LIMIT,
    build_profile_search_url,
    run_x_profile_tweets_spider,
)
from src.platforms.x_twitter.windows import XProfileTweetsWindow, _x_cdp_url, _x_config


class DummyCheckpoint:
    def latest_output_path(self):
        return None

    def add_output_path(self, output_path):
        pass

    def is_completed(self, key):
        return False

    def mark_completed(self, key, meta=None):
        pass


class TestXProfileTweetsLogic(unittest.TestCase):
    def test_window_defaults_to_latest_50(self):
        window = XProfileTweetsWindow.__new__(XProfileTweetsWindow)
        defaults = {param.key: param.default for param in window.tool_config_params()}

        self.assertEqual(defaults["max_tweets_per_author"], DEFAULT_PROFILE_TWEET_LIMIT)
        self.assertEqual(defaults["max_scrolls"], 80)

    def test_profile_search_url_uses_user_search(self):
        self.assertEqual(
            build_profile_search_url("DemoUser"),
            "https://x.com/search?q=%40DemoUser&src=typed_query&f=user",
        )

    def test_x_window_edge_browser_uses_separate_cdp_port(self):
        values = {"browser": "Edge", "max_scrolls": 12}

        self.assertEqual(_x_cdp_url(values), "http://localhost:9223")
        self.assertEqual(_x_config(values, ("max_scrolls",)), {"max_scrolls": 12, "browser": "edge"})

    @patch("src.platforms.x_twitter.profile_tweets.XlsxRowWriter")
    @patch("src.platforms.x_twitter.profile_tweets.open_task_checkpoint", return_value=DummyCheckpoint())
    @patch("src.platforms.x_twitter.profile_tweets.connect_existing_chromium")
    @patch("src.platforms.x_twitter.profile_tweets.sync_playwright")
    @patch("src.platforms.x_twitter.profile_tweets.extract_post_count")
    @patch("src.platforms.x_twitter.profile_tweets.navigate_to_profile_via_search")
    @patch("src.platforms.x_twitter.profile_tweets.collect_profile_tweets")
    def test_default_collects_latest_50_without_time_filter(
        self,
        mock_collect,
        mock_navigate,
        mock_extract_count,
        mock_sync_pw,
        mock_connect,
        mock_checkpoint,
        mock_writer,
    ):
        mock_context = MagicMock()
        mock_connect.return_value = (MagicMock(), mock_context)
        mock_navigate.return_value = True
        mock_collect.return_value = ([], 0, DEFAULT_PROFILE_TWEET_LIMIT)

        log_msgs = []

        run_x_profile_tweets_spider(
            profile_urls_text="https://x.com/user1",
            keywords_text="keyword1\nkeyword2",
            limit_time_str="是",
            start_date="2025-05-06",
            end_date="2026-05-06",
            get_comments_str="是",
            max_comments=100,
            log_callback=log_msgs.append,
        )

        self.assertEqual(mock_collect.call_count, 1)
        mock_navigate.assert_called_once()
        mock_extract_count.assert_not_called()
        self.assertEqual(mock_context.new_page.call_count, 1)

        args, kwargs = mock_collect.call_args
        self.assertIsNone(args[1])
        self.assertFalse(args[4])
        self.assertFalse(args[7])
        self.assertIsNone(kwargs.get("keyword"))
        self.assertTrue(kwargs.get("page_already_loaded"))
        self.assertEqual(kwargs.get("max_collect"), DEFAULT_PROFILE_TWEET_LIMIT)
        self.assertTrue(any("忽略时间窗口" in msg for msg in log_msgs))
        self.assertTrue(any("忽略补充关键词" in msg for msg in log_msgs))
        self.assertTrue(any("最新推文采集" in msg for msg in log_msgs))

    @patch("src.platforms.x_twitter.profile_tweets.XlsxRowWriter")
    @patch("src.platforms.x_twitter.profile_tweets.open_task_checkpoint", return_value=DummyCheckpoint())
    @patch("src.platforms.x_twitter.profile_tweets.connect_existing_chromium")
    @patch("src.platforms.x_twitter.profile_tweets.sync_playwright")
    @patch("src.platforms.x_twitter.profile_tweets.navigate_to_profile_via_search")
    @patch("src.platforms.x_twitter.profile_tweets.collect_profile_tweets")
    def test_configured_latest_limit_and_scrolls(
        self,
        mock_collect,
        mock_navigate,
        mock_sync_pw,
        mock_connect,
        mock_checkpoint,
        mock_writer,
    ):
        mock_context = MagicMock()
        mock_connect.return_value = (MagicMock(), mock_context)
        mock_navigate.return_value = True
        mock_collect.return_value = ([], 0, 20)

        run_x_profile_tweets_spider(
            profile_urls_text="https://x.com/user_large",
            keywords_text="",
            limit_time_str="否",
            start_date="",
            end_date="",
            get_comments_str="否",
            max_comments=100,
            config={"max_tweets_per_author": 20, "max_scrolls": 12},
        )

        self.assertEqual(mock_collect.call_count, 1)
        mock_navigate.assert_called_once()
        args, kwargs = mock_collect.call_args
        self.assertEqual(args[3], 12)
        self.assertFalse(args[4])
        self.assertTrue(kwargs.get("page_already_loaded"))
        self.assertEqual(kwargs.get("max_collect"), 20)
        self.assertIsNone(kwargs.get("keyword"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
