import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.platforms.x_twitter.profile_tweets import DEFAULT_PROFILE_TWEET_LIMIT, run_x_profile_tweets_spider
from src.platforms.x_twitter.windows import XProfileTweetsWindow


class TestXProfileTweetsLogic(unittest.TestCase):
    def test_window_defaults_to_latest_50(self):
        window = XProfileTweetsWindow.__new__(XProfileTweetsWindow)
        defaults = {param.key: param.default for param in window.tool_config_params()}

        self.assertEqual(defaults["max_tweets_per_author"], DEFAULT_PROFILE_TWEET_LIMIT)
        self.assertEqual(defaults["max_scrolls"], 80)

    @patch("src.platforms.x_twitter.profile_tweets.XlsxRowWriter")
    @patch("src.platforms.x_twitter.profile_tweets.connect_existing_chromium")
    @patch("src.platforms.x_twitter.profile_tweets.sync_playwright")
    @patch("src.platforms.x_twitter.profile_tweets.extract_post_count")
    @patch("src.platforms.x_twitter.profile_tweets.collect_profile_tweets")
    def test_default_collects_latest_50_without_time_filter(
        self,
        mock_collect,
        mock_extract_count,
        mock_sync_pw,
        mock_connect,
        mock_writer,
    ):
        mock_context = MagicMock()
        mock_connect.return_value = (MagicMock(), mock_context)
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
        mock_extract_count.assert_not_called()
        self.assertEqual(mock_context.new_page.call_count, 1)

        args, kwargs = mock_collect.call_args
        self.assertIsNone(args[1])
        self.assertFalse(args[4])
        self.assertFalse(args[7])
        self.assertIsNone(kwargs.get("keyword"))
        self.assertEqual(kwargs.get("max_collect"), DEFAULT_PROFILE_TWEET_LIMIT)
        self.assertTrue(any("忽略时间窗口" in msg for msg in log_msgs))
        self.assertTrue(any("忽略补充关键词" in msg for msg in log_msgs))
        self.assertTrue(any("最新推文采集" in msg for msg in log_msgs))

    @patch("src.platforms.x_twitter.profile_tweets.XlsxRowWriter")
    @patch("src.platforms.x_twitter.profile_tweets.connect_existing_chromium")
    @patch("src.platforms.x_twitter.profile_tweets.sync_playwright")
    @patch("src.platforms.x_twitter.profile_tweets.collect_profile_tweets")
    def test_configured_latest_limit_and_scrolls(
        self,
        mock_collect,
        mock_sync_pw,
        mock_connect,
        mock_writer,
    ):
        mock_context = MagicMock()
        mock_connect.return_value = (MagicMock(), mock_context)
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
        args, kwargs = mock_collect.call_args
        self.assertEqual(args[3], 12)
        self.assertFalse(args[4])
        self.assertEqual(kwargs.get("max_collect"), 20)
        self.assertIsNone(kwargs.get("keyword"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
