"""POST /videos Action suffix and size gates (not Ball flight)."""

from __future__ import annotations

import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

from app.api import videos
from app.pipeline import clip_spec


class ActionSuffixTests(unittest.TestCase):
    def test_accepts_mp4_and_mov(self) -> None:
        videos._assert_action_suffix("delivery.MP4")
        videos._assert_action_suffix("clip.mov")

    def test_rejects_webm_mkv_avi(self) -> None:
        for name in ("a.webm", "a.mkv", "a.avi", "a.m4v", "clip.mp4.exe"):
            with self.assertRaises(HTTPException) as ctx:
                videos._assert_action_suffix(name)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.detail, clip_spec.MSG_EXTENSION)


class ActionObjectSizeTests(unittest.TestCase):
    def test_oversize_deletes_and_rejects(self) -> None:
        with (
            patch("app.api.videos.s3_service.s3_configured", return_value=True),
            patch(
                "app.api.videos.s3_service.head_original",
                return_value={
                    "key": "original/u1/a.mp4",
                    "storage_class": "STANDARD",
                    "content_length": clip_spec.MAX_BYTES + 1,
                },
            ),
            patch("app.api.videos.s3_service.delete_original") as delete,
        ):
            with self.assertRaises(HTTPException) as ctx:
                videos._assert_action_object_size("original/u1/a.mp4")
        self.assertEqual(ctx.exception.detail, clip_spec.MSG_SIZE)
        delete.assert_called_once_with("original/u1/a.mp4")

    def test_empty_deletes_and_rejects(self) -> None:
        with (
            patch("app.api.videos.s3_service.s3_configured", return_value=True),
            patch(
                "app.api.videos.s3_service.head_original",
                return_value={
                    "key": "original/u1/a.mp4",
                    "storage_class": "STANDARD",
                    "content_length": 12,
                },
            ),
            patch("app.api.videos.s3_service.delete_original") as delete,
        ):
            with self.assertRaises(HTTPException) as ctx:
                videos._assert_action_object_size("original/u1/a.mp4")
        self.assertEqual(ctx.exception.detail, clip_spec.MSG_EMPTY)
        delete.assert_called_once()


class StoreIncomingActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_multipart_rejects_webm(self) -> None:
        upload = UploadFile(filename="nets.webm", file=BytesIO(b"x" * 4096))
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "vid"
            with self.assertRaises(HTTPException) as ctx:
                await videos._store_incoming_video(
                    dest=dest, file=upload, source_key=None, source_url=None, original_name=None
                )
        self.assertEqual(ctx.exception.detail, clip_spec.MSG_EXTENSION)

    async def test_multipart_caps_size(self) -> None:
        class _Fat:
            def read(self, _n: int) -> bytes:
                return b"x" * 512

        upload = UploadFile(filename="huge.mp4", file=_Fat())  # type: ignore[arg-type]
        with (
            patch.object(clip_spec, "MAX_BYTES", 1024),
            TemporaryDirectory() as tmp,
        ):
            dest = Path(tmp) / "vid"
            with self.assertRaises(HTTPException) as ctx:
                await videos._store_incoming_video(
                    dest=dest, file=upload, source_key=None, source_url=None, original_name=None
                )
        self.assertEqual(ctx.exception.detail, clip_spec.MSG_SIZE)

    async def test_source_key_rejects_webm_suffix(self) -> None:
        with (
            patch("app.api.videos.s3_service.s3_configured", return_value=True),
            patch("app.api.videos.s3_service.incoming_original_key", return_value="original/u1/a.webm"),
            patch("app.api.videos.s3_service.head_original", return_value={"storage_class": "STANDARD"}),
        ):
            with TemporaryDirectory() as tmp:
                dest = Path(tmp) / "vid"
                with self.assertRaises(HTTPException) as ctx:
                    await videos._store_incoming_video(
                        dest=dest,
                        file=None,
                        source_key="original/u1/a.webm",
                        source_url=None,
                        original_name=None,
                    )
        self.assertEqual(ctx.exception.detail, clip_spec.MSG_EXTENSION)


if __name__ == "__main__":
    unittest.main()
