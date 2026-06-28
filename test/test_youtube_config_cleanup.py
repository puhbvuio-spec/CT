import inspect

from src.platforms.youtube.windows import (
    YouTubeChannelWorksWindow,
    YouTubeCommentsWindow,
    YouTubeContextWindow,
    YouTubeKeywordProWindow,
    YouTubeKeywordWindow,
)


def test_channel_works_no_legacy_video_type_scroll_config():
    class_source = inspect.getsource(YouTubeChannelWorksWindow)

    assert "verify_max_scrolls" not in class_source
    assert "验证最大滚动次数" not in class_source


def test_context_exposes_and_passes_video_type_check_config():
    class_source = inspect.getsource(YouTubeContextWindow)
    run_task_source = inspect.getsource(YouTubeContextWindow.run_task)

    assert 'ConfigParam("check_video_type"' in class_source
    assert "check_video_type" in run_task_source


def test_keyword_windows_expose_language_filter_config():
    basic_keys = {param.key: param.kind for param in YouTubeKeywordWindow.tool_config_params(object())}
    pro_keys = {param.key: param.kind for param in YouTubeKeywordProWindow.tool_config_params(object())}

    assert basic_keys["youtube_language_filter"] == "text"
    assert pro_keys["youtube_language_filter"] == "text"


def test_youtube_comment_tools_expose_mode_and_workers_config():
    windows = [
        YouTubeKeywordWindow,
        YouTubeKeywordProWindow,
        YouTubeChannelWorksWindow,
        YouTubeCommentsWindow,
    ]
    for window_cls in windows:
        params = {param.key: param for param in window_cls.tool_config_params(object())}
        assert params["youtube_comment_mode"].kind == "combo"
        assert params["youtube_comment_mode"].default == "快速模式"
        assert params["youtube_comment_workers"].kind == "int"
        assert params["youtube_comment_workers"].default == 5
