"""Progress writes must not overwrite a cancelled or finished job."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.balltrack import repo as bt_repo
from app.db import repository as repo


class UpdateJobGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_requires_in_flight_status(self) -> None:
        db = MagicMock()
        db.jobs.update_one = AsyncMock()
        with patch("app.db.repository.get_db", return_value=db):
            await repo.update_job("job_1", status="processing", progress=58)
        filt = db.jobs.update_one.await_args.args[0]
        self.assertEqual(filt["_id"], "job_1")
        self.assertEqual(set(filt["status"]["$in"]), {"claimed", "processing", "analyzing"})

    async def test_balltrack_requires_in_flight_status(self) -> None:
        col = MagicMock()
        col.update_one = AsyncMock()
        with patch("app.balltrack.repo._col_jobs", return_value=col):
            await bt_repo.update_job("job_1", status="processing", progress=20)
        filt = col.update_one.await_args.args[0]
        self.assertEqual(set(filt["status"]["$in"]), {"claimed", "processing", "analyzing"})


if __name__ == "__main__":
    unittest.main()
