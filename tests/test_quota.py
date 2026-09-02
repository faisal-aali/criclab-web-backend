from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.pipeline.quota import (
    QueueItem,
    assign_schedule,
    format_expected_when,
    next_utc_midnight,
    queued_eligible_filter,
    utc_day_id,
    utc_midnight,
)


def _dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _item(
    i: int,
    *,
    kind: str = "action",
    created: datetime,
    scheduled: str | None = None,
    collection: str | None = None,
) -> QueueItem:
    col = collection or ("balltrack_jobs" if kind == "ballflight" else "jobs")
    return QueueItem(
        id=f"job_{i}",
        collection=col,
        kind=kind,
        user_id=f"user_{i}",
        created_at=created,
        scheduled_date=scheduled,
    )


class AssignScheduleTests(unittest.TestCase):
    def test_today_has_capacity_all_same_day(self) -> None:
        now = _dt(2026, 9, 2, 10, 0)
        created = _dt(2026, 9, 2, 9, 0)
        queued = [_item(i, created=created + timedelta(seconds=i)) for i in range(3)]
        out = assign_schedule(
            now=now,
            quota=60,
            started_today=10,
            queued=queued,
            in_flight_remaining_seconds=0,
            duration_seconds={"action": 180.0, "ballflight": 120.0},
        )
        self.assertEqual(len(out), 3)
        for a, item in zip(out, queued, strict=True):
            self.assertEqual(a.scheduled_date, "2026-09-02")
            self.assertEqual(a.available_at, item.created_at)
        self.assertEqual(out[0].expected_start_at, now)
        self.assertEqual(out[1].expected_start_at, now + timedelta(seconds=180))
        self.assertEqual(out[2].expected_start_at, now + timedelta(seconds=360))

    def test_overflow_to_next_days(self) -> None:
        now = _dt(2026, 9, 2, 8, 0)
        created = _dt(2026, 9, 2, 7, 0)
        queued = [_item(i, created=created + timedelta(seconds=i)) for i in range(5)]
        out = assign_schedule(
            now=now,
            quota=2,
            started_today=0,
            queued=queued,
            duration_seconds={"action": 60.0, "ballflight": 60.0},
        )
        dates = [a.scheduled_date for a in out]
        self.assertEqual(dates, ["2026-09-02", "2026-09-02", "2026-09-03", "2026-09-03", "2026-09-04"])
        self.assertEqual(out[2].available_at, utc_midnight("2026-09-03"))
        self.assertEqual(out[2].expected_start_at, utc_midnight("2026-09-03"))
        self.assertEqual(out[4].scheduled_date, "2026-09-04")

    def test_started_today_reduces_remaining_slots(self) -> None:
        now = _dt(2026, 9, 2, 12, 0)
        created = _dt(2026, 9, 2, 11, 0)
        queued = [_item(i, created=created) for i in range(3)]
        full = assign_schedule(now=now, quota=2, started_today=2, queued=queued)
        self.assertTrue(all(a.scheduled_date == "2026-09-03" for a in full[:2]))
        self.assertEqual(full[2].scheduled_date, "2026-09-04")

        freed = assign_schedule(now=now, quota=2, started_today=1, queued=queued)
        self.assertEqual(freed[0].scheduled_date, "2026-09-02")
        self.assertEqual([a.scheduled_date for a in freed], ["2026-09-02", "2026-09-03", "2026-09-03"])

    def test_fail_releases_slot_and_fifo_moves_up(self) -> None:
        now = _dt(2026, 9, 2, 9, 0)
        created = [_dt(2026, 9, 2, 8, i) for i in range(3)]
        queued = [_item(i, created=created[i], scheduled="2026-09-03") for i in range(3)]
        after_fail = assign_schedule(
            now=now,
            quota=1,
            started_today=0,
            queued=queued,
            duration_seconds={"action": 60.0, "ballflight": 60.0},
        )
        self.assertEqual(after_fail[0].scheduled_date, "2026-09-02")
        self.assertEqual(after_fail[0].id, "job_0")
        self.assertEqual(after_fail[1].id, "job_1")
        self.assertEqual(after_fail[1].scheduled_date, "2026-09-03")

    def test_interleave_action_and_ballflight_by_created_at(self) -> None:
        now = _dt(2026, 9, 2, 10, 0)
        queued = [
            _item(1, kind="ballflight", created=_dt(2026, 9, 2, 9, 1)),
            _item(2, kind="action", created=_dt(2026, 9, 2, 9, 0)),
            _item(3, kind="action", created=_dt(2026, 9, 2, 9, 2)),
        ]
        queued = sorted(queued, key=lambda i: (i.created_at, i.id))
        out = assign_schedule(
            now=now,
            quota=10,
            started_today=0,
            queued=queued,
            duration_seconds={"action": 100.0, "ballflight": 50.0},
        )
        self.assertEqual([a.id for a in out], ["job_2", "job_1", "job_3"])
        self.assertEqual(out[0].kind, "action")
        self.assertEqual(out[1].kind, "ballflight")
        self.assertEqual(out[1].expected_start_at, now + timedelta(seconds=100))
        self.assertEqual(out[2].expected_start_at, now + timedelta(seconds=150))

    def test_clock_rolls_to_next_day_even_if_slots_remain(self) -> None:
        now = _dt(2026, 9, 2, 23, 50)
        queued = [_item(i, created=now) for i in range(3)]
        out = assign_schedule(
            now=now,
            quota=10,
            started_today=0,
            queued=queued,
            in_flight_remaining_seconds=0,
            duration_seconds={"action": 600.0, "ballflight": 600.0},
        )
        self.assertEqual(out[0].scheduled_date, "2026-09-02")
        # 23:50 + 10 min = 00:00 next day → second job cannot start today
        self.assertEqual(out[1].scheduled_date, "2026-09-03")
        self.assertEqual(out[2].scheduled_date, "2026-09-03")

    def test_in_flight_remaining_pushes_first_start(self) -> None:
        now = _dt(2026, 9, 2, 10, 0)
        queued = [_item(0, created=now)]
        out = assign_schedule(
            now=now,
            quota=5,
            started_today=1,
            queued=queued,
            in_flight_remaining_seconds=90,
            duration_seconds={"action": 180.0, "ballflight": 120.0},
        )
        self.assertEqual(out[0].expected_start_at, now + timedelta(seconds=90))
        self.assertEqual(out[0].scheduled_date, "2026-09-02")

    def test_utc_day_helpers(self) -> None:
        self.assertEqual(utc_day_id(_dt(2026, 9, 2, 23, 59)), "2026-09-02")
        self.assertEqual(utc_midnight("2026-09-02"), _dt(2026, 9, 2))
        self.assertIn("UTC", format_expected_when(_dt(2026, 9, 2, 14, 20)))
        self.assertEqual(next_utc_midnight(_dt(2026, 9, 2, 23, 59)), _dt(2026, 9, 3))
        self.assertEqual(next_utc_midnight(_dt(2026, 9, 3, 0, 0)), _dt(2026, 9, 4))


class QueuedEligibleFilterTests(unittest.TestCase):
    def test_includes_missing_and_due_available_at(self) -> None:
        now = _dt(2026, 9, 2, 10, 0)
        filt = queued_eligible_filter(now)
        self.assertEqual(filt["status"], "queued")
        self.assertIn({"available_at": {"$lte": now}}, filt["$or"])
        self.assertIn({"available_at": {"$exists": False}}, filt["$or"])
        self.assertIn({"available_at": None}, filt["$or"])


class HasClaimableJobTests(unittest.TestCase):
    def test_false_when_quota_is_full(self) -> None:
        from app.pipeline import quota

        db = MagicMock()
        with (
            patch("app.pipeline.quota.started_on", new_callable=AsyncMock, return_value=60),
            patch(
                "app.pipeline.quota.get_settings",
                return_value=SimpleNamespace(daily_video_quota=60),
            ),
            patch("app.pipeline.quota.get_db", return_value=db),
        ):
            self.assertFalse(asyncio.run(quota.has_claimable_job(_dt(2026, 9, 2, 12, 0))))
        db.jobs.find_one.assert_not_called()

    def test_true_when_action_job_is_eligible(self) -> None:
        from app.pipeline import quota

        db = MagicMock()
        db.jobs.find_one = AsyncMock(return_value={"_id": "job_1"})
        db.balltrack_jobs.find_one = AsyncMock(return_value=None)
        with (
            patch("app.pipeline.quota.started_on", new_callable=AsyncMock, return_value=3),
            patch(
                "app.pipeline.quota.get_settings",
                return_value=SimpleNamespace(daily_video_quota=60),
            ),
            patch("app.pipeline.quota.get_db", return_value=db),
        ):
            self.assertTrue(asyncio.run(quota.has_claimable_job(_dt(2026, 9, 2, 12, 0))))

    def test_true_when_only_ballflight_is_eligible(self) -> None:
        from app.pipeline import quota

        db = MagicMock()
        db.jobs.find_one = AsyncMock(return_value=None)
        db.balltrack_jobs.find_one = AsyncMock(return_value={"_id": "btj_1"})
        with (
            patch("app.pipeline.quota.started_on", new_callable=AsyncMock, return_value=0),
            patch(
                "app.pipeline.quota.get_settings",
                return_value=SimpleNamespace(daily_video_quota=60),
            ),
            patch("app.pipeline.quota.get_db", return_value=db),
        ):
            self.assertTrue(asyncio.run(quota.has_claimable_job(_dt(2026, 9, 2, 12, 0))))


class ScheduleQueuedJobsWakeTests(unittest.TestCase):
    def test_maybe_wake_runs_even_if_schedule_fails(self) -> None:
        from app.pipeline import quota

        with (
            patch("app.pipeline.quota.get_settings", side_effect=RuntimeError("boom")),
            patch("app.services.ec2_worker.maybe_wake_worker") as wake,
        ):
            out = asyncio.run(quota.schedule_queued_jobs())
        self.assertEqual(out, [])
        wake.assert_called_once_with()


class MidnightWakeLoopTests(unittest.TestCase):
    def test_ticks_on_boot_then_sleeps(self) -> None:
        from app.pipeline import quota

        async def _sleep(*_a, **_k):
            raise asyncio.CancelledError()

        with (
            patch("app.pipeline.quota.schedule_queued_jobs", new_callable=AsyncMock) as sched,
            patch("app.pipeline.quota.asyncio.sleep", side_effect=_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(quota.midnight_wake_loop(slice_seconds=30))
        sched.assert_awaited()


if __name__ == "__main__":
    unittest.main()
