from src.platforms.youtube import comments as comments_module
from src.platforms.youtube.comments import (
    COMMENT_MODE_DEEP,
    COMMENT_MODE_FAST,
    CommentFetchTask,
    effective_comment_scan_limit,
    fetch_top_comments_for_videos,
    normalize_comment_workers,
)


def test_effective_comment_scan_limit_fast_and_deep_modes():
    assert effective_comment_scan_limit(500, 100, COMMENT_MODE_FAST) == 100
    assert effective_comment_scan_limit(500, 100, COMMENT_MODE_DEEP) == 500
    assert effective_comment_scan_limit(50, 100, COMMENT_MODE_DEEP) == 100


def test_normalize_comment_workers_bounds_values():
    assert normalize_comment_workers("5") == 5
    assert normalize_comment_workers(0) == 1
    assert normalize_comment_workers(99) == 10


def test_fetch_top_comments_for_videos_keeps_video_mapping_and_failures(monkeypatch):
    calls = []

    class DummyPool:
        def __init__(self, api_keys):
            self.api_keys = api_keys

    def fake_fetch(_pool, video_id, max_scan_comments, *_args, **_kwargs):
        calls.append((video_id, max_scan_comments))
        if video_id == "fail":
            raise RuntimeError("boom")
        return [{"like_count": 1, "text": f"text-{video_id}", "published_at": ""}]

    monkeypatch.setattr(comments_module, "YouTubeClientPool", DummyPool)
    monkeypatch.setattr(comments_module, "fetch_top_level_comments", fake_fetch)

    results = fetch_top_comments_for_videos(
        ["key"],
        [CommentFetchTask("b"), CommentFetchTask("a"), CommentFetchTask("fail")],
        max_scan_comments=500,
        top_comment_limit=100,
        comment_mode=COMMENT_MODE_FAST,
        workers=1,
        log_callback=lambda _msg: None,
    )

    assert calls == [("b", 100), ("a", 100), ("fail", 100)]
    assert results["b"].comments[0]["text"] == "text-b"
    assert results["a"].comments[0]["text"] == "text-a"
    assert results["fail"].status == "error"
