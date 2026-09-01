"""Before · your clip must be an H.264 MP4 URL, not a raw iPhone .mov."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.api.videos import _original_video_url
from app.services.cloudinary_service import PLAYBACK_TRANSFORMATION, browser_playback_url


MOV = "https://res.cloudinary.com/demo/video/upload/v1700000000/criclab/incoming/clip.mov"
MP4 = (
    "https://res.cloudinary.com/demo/video/upload/"
    f"{PLAYBACK_TRANSFORMATION}/v1700000000/criclab/incoming/clip.mp4"
)


class BrowserPlaybackUrlTests(unittest.TestCase):
    def test_iphone_mov_becomes_h264_mp4(self) -> None:
        out = browser_playback_url(MOV)
        self.assertIn(PLAYBACK_TRANSFORMATION, out)
        self.assertTrue(out.endswith(".mp4"))
        self.assertEqual(out, MP4)

    def test_already_transformed_url_is_left_alone(self) -> None:
        self.assertEqual(browser_playback_url(MP4), MP4)

    def test_non_cloudinary_url_is_left_alone(self) -> None:
        local = "/media/videos/vid_abc.mp4"
        self.assertEqual(browser_playback_url(local), local)


class OriginalVideoUrlTests(unittest.TestCase):
    def test_cloudinary_source_wins_over_missing_mac_path(self) -> None:
        video = {
            "source_url": MOV,
            "path": "/Users/macbookpro/.local/share/criclab/videos/vid_abc.mov",
        }
        self.assertEqual(_original_video_url(video), MP4)

    def test_missing_source_and_missing_file_is_none(self) -> None:
        video = {"source_url": None, "path": "/no/such/vid_abc.mov"}
        self.assertIsNone(_original_video_url(video))

    def test_local_file_when_source_url_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            videos = Path(tmp) / "videos"
            videos.mkdir()
            clip = videos / "vid_local.mp4"
            clip.write_bytes(b"not-a-real-mp4")
            video = {"source_url": None, "path": str(clip)}

            class _Settings:
                storage_path = Path(tmp)

            with patch("app.api.videos.get_settings", return_value=_Settings()):
                self.assertEqual(_original_video_url(video), "/media/videos/vid_local.mp4")


if __name__ == "__main__":
    unittest.main()
