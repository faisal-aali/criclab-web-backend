"""Glacier archive guards: skip when a live job still needs the original."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import original_archive


class MaybeArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_live_job_shares_key(self) -> None:
        with (
            patch("app.services.original_archive.s3_service.s3_configured", return_value=True),
            patch(
                "app.services.original_archive.source_key_has_live_jobs",
                new=AsyncMock(return_value=True),
            ),
            patch("app.services.original_archive.s3_service.archive_original") as copy,
        ):
            ok = await original_archive.maybe_archive_original("original/u1/a.mp4")
        self.assertFalse(ok)
        copy.assert_not_called()

    async def test_no_op_when_s3_off(self) -> None:
        with patch("app.services.original_archive.s3_service.s3_configured", return_value=False):
            self.assertFalse(await original_archive.maybe_archive_original("original/u1/a.mp4"))

    async def test_archives_when_no_live_job(self) -> None:
        with (
            patch("app.services.original_archive.s3_service.s3_configured", return_value=True),
            patch(
                "app.services.original_archive.source_key_has_live_jobs",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.services.original_archive.s3_service.archive_original",
                return_value="GLACIER",
            ),
            patch(
                "app.services.original_archive._mark_archived",
                new=AsyncMock(),
            ) as mark,
        ):
            ok = await original_archive.maybe_archive_original("original/u1/a.mp4")
        self.assertTrue(ok)
        mark.assert_awaited_once()

    async def test_live_job_query_sees_action_and_ballflight(self) -> None:
        db = MagicMock()
        db.videos.find.return_value.to_list = AsyncMock(return_value=[{"_id": "vid_1"}])
        db.balltrack_sessions.find.return_value.to_list = AsyncMock(return_value=[])
        db.jobs.find_one = AsyncMock(return_value={"_id": "job_live"})
        with patch("app.services.original_archive.get_db", return_value=db):
            self.assertTrue(
                await original_archive.source_key_has_live_jobs("original/u1/a.mp4")
            )
        filt = db.jobs.find_one.await_args.args[0]
        self.assertEqual(filt["video_id"]["$in"], ["vid_1"])
        self.assertEqual(
            set(filt["status"]["$in"]),
            {"queued", "claimed", "processing", "analyzing"},
        )


if __name__ == "__main__":
    unittest.main()
