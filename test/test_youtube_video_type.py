import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from src.platforms.youtube.video_type import (
    NORMAL_VIDEO,
    SHORTS,
    UNKNOWN,
    check_video_type,
    check_video_type_bulk,
)


class TestYouTubeVideoType(unittest.TestCase):
    def test_check_video_type_200_is_shorts(self):
        opener = MagicMock()
        opener.open.return_value.status = 200

        self.assertEqual(check_video_type("abc123", opener=opener), SHORTS)

    def test_check_video_type_redirect_is_regular_video(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            url="https://www.youtube.com/shorts/abc123",
            code=302,
            msg="Found",
            hdrs={},
            fp=None,
        )

        self.assertEqual(check_video_type("abc123", opener=opener), NORMAL_VIDEO)

    def test_check_video_type_repeated_error_is_unknown(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("offline")

        with patch("src.platforms.youtube.video_type.time.sleep"):
            self.assertEqual(check_video_type("abc123", opener=opener, max_attempts=3), UNKNOWN)
        self.assertEqual(opener.open.call_count, 3)

    @patch("src.platforms.youtube.video_type.check_video_type")
    def test_check_video_type_bulk_deduplicates_ids(self, mock_check):
        mock_check.side_effect = lambda vid: SHORTS if vid == "a" else NORMAL_VIDEO

        result = check_video_type_bulk(["a", "b", "a", "", "b"], max_workers=2)

        self.assertEqual(result, {"a": SHORTS, "b": NORMAL_VIDEO})
        self.assertEqual(mock_check.call_count, 2)


if __name__ == "__main__":
    unittest.main()
