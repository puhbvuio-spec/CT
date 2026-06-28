import sys
import os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from PyQt5.QtWidgets import QApplication
from src.platforms.tiktok.windows import TikTokKeywordWindow, TikTokProfileVideosWindow
from src.platforms.x_twitter.windows import XKeywordWindow, XTweetMetricsWindow, XProfileTweetsWindow
from src.platforms.youtube.windows import YouTubeKeywordWindow, YouTubeChannelWorksWindow, YouTubeKeywordProWindow

def test_visibility():
    app = QApplication.instance() or QApplication(sys.argv)
    
    windows = [
        ("TikTokKeywordWindow", TikTokKeywordWindow()),
        ("TikTokProfileVideosWindow", TikTokProfileVideosWindow()),
        ("XKeywordWindow", XKeywordWindow()),
        ("XTweetMetricsWindow", XTweetMetricsWindow()),
        ("XProfileTweetsWindow", XProfileTweetsWindow()),
        ("YouTubeKeywordWindow", YouTubeKeywordWindow()),
        ("YouTubeChannelWorksWindow", YouTubeChannelWorksWindow()),
        ("YouTubeKeywordProWindow", YouTubeKeywordProWindow()),
    ]
    
    for name, window in windows:
        print(f"\nTesting {name}...")
        
        limit_combo = window.widgets.get("limit_time")
        if limit_combo:
            start_date_widget = window.widgets.get("start_date")
            print(f"  Default limit_time: {limit_combo.currentText()}")
            print(f"  Start date hidden: {start_date_widget.isHidden()}")

            limit_combo.setCurrentText("否")
            print("  Changed limit_time to '否'")
            print(f"  Start date hidden: {start_date_widget.isHidden()}")

            limit_combo.setCurrentText("是")
        else:
            print("  No limit_time field")
        
        get_comments_combo = window.widgets.get("get_comments")
        if get_comments_combo:
            max_comments_widget = window.widgets.get("max_comments")
            print(f"  Default get_comments: {get_comments_combo.currentText()}")
            print(f"  max_comments hidden: {max_comments_widget.isHidden()}")

            get_comments_combo.setCurrentText("是")
            print("  Changed get_comments to '是'")
            print(f"  max_comments hidden: {max_comments_widget.isHidden()}")
        else:
            print("  No get_comments field")

    print("\nAll tests passed!")

if __name__ == "__main__":
    test_visibility()
