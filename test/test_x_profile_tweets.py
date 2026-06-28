import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.platforms.x_twitter.profile_tweets import (
    DEFAULT_PROFILE_TWEET_LIMIT,
    XTransientProfileSkipped,
    build_profile_search_url,
    collect_profile_tweets,
    handle_empty_profile_tweets_recovery,
    make_x_transient_skip_state,
    normalize_scroll_delay_range,
    run_x_profile_tweets_spider,
    use_profile_search_entry,
)
from src.platforms.x_twitter.windows import XProfileTweetsWindow, _x_cdp_url, _x_config


class DummyCheckpoint:
    run_id = "dummy-run"

    def latest_output_path(self):
        return None

    def has_other_active_runs(self):
        return False

    def add_output_path(self, output_path):
        pass

    def is_completed(self, key):
        return False

    def claim_item(self, key, positive_count_fields=()):
        return True, "claimed"

    def release_item(self, key):
        pass

    def mark_completed(self, key, meta=None):
        pass


class TestXProfileTweetsLogic(unittest.TestCase):
    def test_window_defaults_to_latest_50(self):
        window = XProfileTweetsWindow.__new__(XProfileTweetsWindow)
        defaults = {param.key: param.default for param in window.tool_config_params()}

        self.assertEqual(defaults["max_tweets_per_author"], DEFAULT_PROFILE_TWEET_LIMIT)
        self.assertEqual(defaults["max_scrolls"], 80)
        self.assertEqual(defaults["scroll_interval_min"], 2.4)
        self.assertEqual(defaults["scroll_interval_max"], 5.6)
        self.assertEqual(defaults["profile_entry_mode"], "直接打开")

    def test_profile_entry_mode_defaults_to_direct(self):
        self.assertFalse(use_profile_search_entry({}))
        self.assertFalse(use_profile_search_entry({"profile_entry_mode": "直接打开"}))
        self.assertTrue(use_profile_search_entry({"profile_entry_mode": "搜索页进入"}))

    def test_scroll_delay_range_supports_old_and_swapped_config(self):
        self.assertEqual(normalize_scroll_delay_range({"scroll_interval": 3.2}), (3.2, 3.2))
        self.assertEqual(normalize_scroll_delay_range({"scroll_interval_min": 6.0, "scroll_interval_max": 2.0}), (2.0, 6.0))

    def test_profile_search_url_uses_user_search(self):
        self.assertEqual(
            build_profile_search_url("DemoUser"),
            "https://x.com/search?q=%40DemoUser&src=typed_query&f=user",
        )

    @patch("src.platforms.x_twitter.profile_tweets.interruptible_sleep", return_value=False)
    @patch("src.platforms.x_twitter.profile_tweets.wait_for_x_page_recovery")
    @patch("src.platforms.x_twitter.profile_tweets.extract_visible_profile_tweets")
    def test_recovery_is_not_triggered_when_tweets_are_visible(self, mock_extract, mock_recovery, mock_sleep):
        page = MagicMock()
        page.wait_for_selector.return_value = None
        page.locator.return_value.all.return_value = []
        page.evaluate.return_value = 1000
        mock_extract.return_value = [
            {
                "postId": "1",
                "publishedAt": "2026-06-01T00:00:00Z",
                "content": "visible tweet",
                "url": "https://x.com/demo/status/1",
            }
        ]

        tweets = collect_profile_tweets(
            page,
            None,
            "https://x.com/demo",
            max_scrolls=1,
            limit_time_bool=False,
            start_dt=None,
            end_dt=None,
            get_comments_bool=False,
            max_comments=0,
            log_callback=None,
            page_timeout=100,
            page_already_loaded=True,
            max_collect=1,
        )

        self.assertEqual(len(tweets), 1)
        mock_recovery.assert_not_called()

    @patch("src.platforms.x_twitter.profile_tweets.interruptible_sleep", return_value=False)
    @patch("src.platforms.x_twitter.profile_tweets.wait_for_x_page_recovery")
    @patch("src.platforms.x_twitter.profile_tweets.extract_visible_profile_tweets", return_value=[])
    def test_empty_tweets_without_x_error_do_not_trigger_recovery(self, mock_extract, mock_recovery, mock_sleep):
        page = MagicMock()
        page.wait_for_selector.return_value = None
        page.locator.return_value.all.return_value = []
        page.evaluate.return_value = 1000

        tweets = collect_profile_tweets(
            page,
            None,
            "https://x.com/demo",
            max_scrolls=1,
            limit_time_bool=False,
            start_dt=None,
            end_dt=None,
            get_comments_bool=False,
            max_comments=0,
            log_callback=None,
            page_timeout=100,
            page_already_loaded=True,
        )

        self.assertEqual(tweets, [])
        mock_recovery.assert_not_called()

    @patch("src.platforms.x_twitter.profile_tweets.check_network_reachable", return_value=(True, "HTTP 204"))
    @patch("src.platforms.x_twitter.profile_tweets.wait_for_x_page_recovery")
    @patch("src.platforms.x_twitter.profile_tweets.extract_visible_profile_tweets", return_value=[])
    def test_transient_x_error_defers_profile_once(self, mock_extract, mock_recovery, mock_network):
        page = MagicMock()
        page.wait_for_selector.return_value = None
        page.locator.return_value.all.return_value = []
        page.evaluate.return_value = "Something went wrong. Try reloading."

        with self.assertRaises(XTransientProfileSkipped) as raised:
            collect_profile_tweets(
                page,
                None,
                "https://x.com/demo",
                max_scrolls=1,
                limit_time_bool=False,
                start_dt=None,
                end_dt=None,
                get_comments_bool=False,
                max_comments=0,
                log_callback=None,
                page_timeout=100,
                page_already_loaded=True,
                transient_skip_state=make_x_transient_skip_state({"x_transient_skip_before_wait": 2}),
            )

        self.assertTrue(raised.exception.retry_after_success)
        mock_network.assert_called()
        mock_recovery.assert_not_called()

    @patch("src.platforms.x_twitter.profile_tweets.wait_for_x_page_recovery", return_value=True)
    @patch("src.platforms.x_twitter.profile_tweets.detect_x_transient_error", return_value="Something went wrong")
    def test_second_transient_skip_waits_and_marks_retry_final(self, mock_detect, mock_recovery):
        state = make_x_transient_skip_state({"x_transient_skip_before_wait": 2})
        page = MagicMock()

        with self.assertRaises(XTransientProfileSkipped) as first:
            handle_empty_profile_tweets_recovery(
                page,
                "demo",
                recovery_config={"x_network_check_enabled": True},
                transient_skip_state=state,
                network_checker=lambda _config: (True, "HTTP 204"),
            )
        self.assertTrue(first.exception.retry_after_success)
        mock_recovery.assert_not_called()

        with self.assertRaises(XTransientProfileSkipped) as second:
            handle_empty_profile_tweets_recovery(
                page,
                "demo",
                recovery_config={"x_network_check_enabled": True},
                transient_skip_state=state,
                network_checker=lambda _config: (True, "HTTP 204"),
                transient_retry=True,
            )
        self.assertFalse(second.exception.retry_after_success)
        mock_recovery.assert_called_once()

    def test_x_window_edge_browser_uses_separate_cdp_port(self):
        values = {"browser": "Edge", "max_scrolls": 12}

        self.assertEqual(_x_cdp_url(values), "http://localhost:9223")
        self.assertEqual(_x_config(values, ("max_scrolls",)), {"max_scrolls": 12, "browser": "edge"})

    @patch("src.platforms.x_twitter.profile_tweets.XlsxRowWriter")
    @patch("src.platforms.x_twitter.profile_tweets.open_task_checkpoint", return_value=DummyCheckpoint())
    @patch("src.platforms.x_twitter.profile_tweets.connect_existing_chromium")
    @patch("src.platforms.x_twitter.profile_tweets.sync_playwright")
    @patch("src.platforms.x_twitter.profile_tweets.extract_post_count")
    @patch("src.platforms.x_twitter.profile_tweets.navigate_to_profile")
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
        self.assertFalse(mock_navigate.call_args.kwargs.get("use_search_entry"))
        self.assertTrue(any("忽略时间窗口" in msg for msg in log_msgs))
        self.assertTrue(any("忽略补充关键词" in msg for msg in log_msgs))
        self.assertTrue(any("最新推文采集" in msg for msg in log_msgs))

    @patch("src.platforms.x_twitter.profile_tweets.XlsxRowWriter")
    @patch("src.platforms.x_twitter.profile_tweets.open_task_checkpoint", return_value=DummyCheckpoint())
    @patch("src.platforms.x_twitter.profile_tweets.connect_existing_chromium")
    @patch("src.platforms.x_twitter.profile_tweets.sync_playwright")
    @patch("src.platforms.x_twitter.profile_tweets.navigate_to_profile")
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

    @patch("src.platforms.x_twitter.profile_tweets.XlsxRowWriter")
    @patch("src.platforms.x_twitter.profile_tweets.open_task_checkpoint", return_value=DummyCheckpoint())
    @patch("src.platforms.x_twitter.profile_tweets.connect_existing_chromium")
    @patch("src.platforms.x_twitter.profile_tweets.sync_playwright")
    @patch("src.platforms.x_twitter.profile_tweets.navigate_to_profile")
    @patch("src.platforms.x_twitter.profile_tweets.collect_profile_tweets")
    def test_search_entry_mode_is_optional(
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
        mock_collect.return_value = ([], 0, 5)

        run_x_profile_tweets_spider(
            profile_urls_text="https://x.com/user_search",
            keywords_text="",
            limit_time_str="否",
            start_date="",
            end_date="",
            get_comments_str="否",
            max_comments=100,
            config={"profile_entry_mode": "搜索页进入"},
        )

        self.assertTrue(mock_navigate.call_args.kwargs.get("use_search_entry"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
