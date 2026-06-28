import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.platforms.x_twitter.profile_tweets import run_x_profile_tweets_spider

class TestXProfileTweetsLogic(unittest.TestCase):
    
    @patch('src.platforms.x_twitter.profile_tweets.XlsxRowWriter')
    @patch('src.platforms.x_twitter.profile_tweets.MultiSheetXlsxWriter')
    @patch('src.platforms.x_twitter.profile_tweets.connect_existing_chromium')
    @patch('src.platforms.x_twitter.profile_tweets.sync_playwright')
    @patch('src.platforms.x_twitter.profile_tweets.extract_post_count')
    @patch('src.platforms.x_twitter.profile_tweets.collect_profile_tweets')
    def test_routing_less_than_1000(self, mock_collect, mock_extract_count, mock_sync_pw, mock_connect, mock_multi_writer, mock_writer):
        """测试：帖文数量 <= 1000 时，应该走全量采集，且忽略关键字。"""
        # Mock 提取出来的帖文数为 500
        mock_extract_count.return_value = 500
        
        # Mock Playwright 环境
        mock_context = MagicMock()
        mock_connect.return_value = (MagicMock(), mock_context)
        
        # Mock collect 返回值 (tweets, row_offset, written_count)
        mock_collect.return_value = ([], 0, 500)
        
        log_msgs = []
        def log_callback(msg):
            log_msgs.append(msg)
            
        run_x_profile_tweets_spider(
            profile_urls_text="https://x.com/user1",
            keywords_text="keyword1\nkeyword2",
            limit_time_str="否",
            start_date="2025-05-06",
            end_date="2026-05-06",
            get_comments_str="否",
            max_comments=100,
            log_callback=log_callback
        )
        
        # 验证调用了一次 collect_profile_tweets（全量采集）
        self.assertEqual(mock_collect.call_count, 1)
        
        # 验证传递给 collect_profile_tweets 的参数 max_collect=None, keyword=None
        _, kwargs = mock_collect.call_args
        self.assertIsNone(kwargs.get('max_collect'))
        self.assertIsNone(kwargs.get('keyword'))
        
        # 验证日志输出包含了相关信息
        self.assertTrue(any("博主帖文数量：500" in msg for msg in log_msgs))
        self.assertTrue(any("全量采集" in msg for msg in log_msgs))

    @patch('src.platforms.x_twitter.profile_tweets.XlsxRowWriter')
    @patch('src.platforms.x_twitter.profile_tweets.connect_existing_chromium')
    @patch('src.platforms.x_twitter.profile_tweets.sync_playwright')
    @patch('src.platforms.x_twitter.profile_tweets.extract_post_count')
    @patch('src.platforms.x_twitter.profile_tweets.collect_profile_tweets')
    def test_routing_greater_than_1000_with_keywords(self, mock_collect, mock_extract_count, mock_sync_pw, mock_connect, mock_writer):
        """测试：帖文数量 > 1000 且提供了关键词时，应该先截断采集1000，再补充采集关键词。"""
        # Mock 提取出来的帖文数为 15000
        mock_extract_count.return_value = 15000
        
        mock_context = MagicMock()
        mock_connect.return_value = (MagicMock(), mock_context)
        mock_collect.return_value = ([], 0, 100)
        
        log_msgs = []
        def log_callback(msg):
            log_msgs.append(msg)
            
        run_x_profile_tweets_spider(
            profile_urls_text="https://x.com/user_large",
            keywords_text="apple\nbanana",
            limit_time_str="否",
            start_date="",
            end_date="",
            get_comments_str="否",
            max_comments=100,
            log_callback=log_callback
        )
        
        # 一次截断采集 + 两个关键词补充采集 = 3 次调用
        self.assertEqual(mock_collect.call_count, 3)
        
        calls = mock_collect.call_args_list
        # 第 1 次调用：截断采集 max_collect=1000, keyword=None
        self.assertEqual(calls[0][1]['max_collect'], 1000)
        self.assertIsNone(calls[0][1]['keyword'])
        
        # 第 2 次调用：关键词补充 keyword="apple", max_collect=None
        self.assertIsNone(calls[1][1]['max_collect'])
        self.assertEqual(calls[1][1]['keyword'], "apple")
        
        # 第 3 次调用：关键词补充 keyword="banana", max_collect=None
        self.assertIsNone(calls[2][1]['max_collect'])
        self.assertEqual(calls[2][1]['keyword'], "banana")
        
        self.assertTrue(any("博主帖文数量：15000" in msg for msg in log_msgs))
        self.assertTrue(any("帖文数大于 1000，首先采集前 1000 条帖子" in msg for msg in log_msgs))

    @patch('src.platforms.x_twitter.profile_tweets.XlsxRowWriter')
    @patch('src.platforms.x_twitter.profile_tweets.connect_existing_chromium')
    @patch('src.platforms.x_twitter.profile_tweets.sync_playwright')
    @patch('src.platforms.x_twitter.profile_tweets.extract_post_count')
    @patch('src.platforms.x_twitter.profile_tweets.collect_profile_tweets')
    def test_routing_greater_than_1000_no_keywords(self, mock_collect, mock_extract_count, mock_sync_pw, mock_connect, mock_writer):
        """测试：帖文数量 > 1000 但未提供关键词，只截断1000即停止。"""
        mock_extract_count.return_value = 5000
        mock_context = MagicMock()
        mock_connect.return_value = (MagicMock(), mock_context)
        mock_collect.return_value = ([], 0, 100)
        
        log_msgs = []
        def log_callback(msg):
            log_msgs.append(msg)
            
        run_x_profile_tweets_spider(
            profile_urls_text="https://x.com/user_large",
            keywords_text="",
            limit_time_str="否",
            start_date="",
            end_date="",
            get_comments_str="否",
            max_comments=100,
            log_callback=log_callback
        )
        
        # 只有一次截断调用
        self.assertEqual(mock_collect.call_count, 1)
        self.assertEqual(mock_collect.call_args[1]['max_collect'], 1000)
        self.assertIsNone(mock_collect.call_args[1]['keyword'])
        
        self.assertTrue(any("未提供补充搜索关键词，跳过补充采集阶段" in msg for msg in log_msgs))

if __name__ == '__main__':
    unittest.main(verbosity=2)
