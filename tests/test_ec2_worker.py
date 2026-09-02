from __future__ import annotations

import asyncio
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import ec2_worker


def _settings(
    *,
    production: bool = True,
    instance_id: str = "i-0c2341cb9422fdaa0",
    region: str = "ap-south-1",
    key: str | None = "AKIAEXAMPLE",
    secret: str | None = "secret",
) -> SimpleNamespace:
    return SimpleNamespace(
        is_production=production,
        worker_ec2_instance_id=instance_id,
        worker_ec2_region=region,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
    )


def _describe(state: str, instance_id: str = "i-0c2341cb9422fdaa0") -> dict:
    return {
        "Reservations": [
            {"Instances": [{"InstanceId": instance_id, "State": {"Name": state}}]}
        ]
    }


@contextmanager
def _boto3_client(client: MagicMock) -> Iterator[MagicMock]:
    module = MagicMock()
    module.client.return_value = client
    with patch.dict("sys.modules", {"boto3": module}):
        yield module.client


class StartIfStoppedTests(unittest.TestCase):
    def test_starts_only_when_stopped(self) -> None:
        client = MagicMock()
        client.describe_instances.return_value = _describe("stopped")
        with (
            patch("app.services.ec2_worker.get_settings", return_value=_settings()),
            _boto3_client(client) as make_client,
        ):
            self.assertEqual(ec2_worker._start_if_stopped(), "started")
        make_client.assert_called_once_with(
            "ec2",
            region_name="ap-south-1",
            aws_access_key_id="AKIAEXAMPLE",
            aws_secret_access_key="secret",
        )
        client.describe_instances.assert_called_once_with(InstanceIds=["i-0c2341cb9422fdaa0"])
        client.start_instances.assert_called_once_with(InstanceIds=["i-0c2341cb9422fdaa0"])

    def test_does_not_start_when_running(self) -> None:
        client = MagicMock()
        client.describe_instances.return_value = _describe("running")
        with (
            patch("app.services.ec2_worker.get_settings", return_value=_settings()),
            _boto3_client(client),
        ):
            self.assertEqual(ec2_worker._start_if_stopped(), "running")
        client.start_instances.assert_not_called()

    def test_does_not_start_when_pending(self) -> None:
        client = MagicMock()
        client.describe_instances.return_value = _describe("pending")
        with (
            patch("app.services.ec2_worker.get_settings", return_value=_settings()),
            _boto3_client(client),
        ):
            self.assertEqual(ec2_worker._start_if_stopped(), "pending")
        client.start_instances.assert_not_called()

    def test_does_not_start_when_stopping(self) -> None:
        client = MagicMock()
        client.describe_instances.return_value = _describe("stopping")
        with (
            patch("app.services.ec2_worker.get_settings", return_value=_settings()),
            _boto3_client(client),
        ):
            self.assertEqual(ec2_worker._start_if_stopped(), "stopping")
        client.start_instances.assert_not_called()

    def test_shutting_down_is_treated_as_stopping(self) -> None:
        client = MagicMock()
        client.describe_instances.return_value = _describe("shutting-down")
        with (
            patch("app.services.ec2_worker.get_settings", return_value=_settings()),
            _boto3_client(client),
        ):
            self.assertEqual(ec2_worker._start_if_stopped(), "stopping")
        client.start_instances.assert_not_called()

    def test_does_not_start_when_instance_missing(self) -> None:
        client = MagicMock()
        client.describe_instances.return_value = {"Reservations": []}
        with (
            patch("app.services.ec2_worker.get_settings", return_value=_settings()),
            _boto3_client(client),
        ):
            self.assertEqual(ec2_worker._start_if_stopped(), "missing")
        client.start_instances.assert_not_called()

    def test_uses_default_credential_chain_without_keys(self) -> None:
        client = MagicMock()
        client.describe_instances.return_value = _describe("stopped")
        with (
            patch(
                "app.services.ec2_worker.get_settings",
                return_value=_settings(key=None, secret=None),
            ),
            _boto3_client(client) as make_client,
        ):
            self.assertEqual(ec2_worker._start_if_stopped(), "started")
        make_client.assert_called_once_with("ec2", region_name="ap-south-1")
        client.start_instances.assert_called_once()


class ScheduleWakeWorkerTests(unittest.TestCase):
    def test_local_env_does_not_create_task(self) -> None:
        with (
            patch("app.services.ec2_worker.get_settings", return_value=_settings(production=False)),
            patch("app.services.ec2_worker._wake_worker", new_callable=AsyncMock) as wake,
        ):
            async def _run() -> None:
                ec2_worker.schedule_wake_worker()
                await asyncio.sleep(0)

            asyncio.run(_run())
            wake.assert_not_called()

    def test_empty_instance_id_does_not_create_task(self) -> None:
        with (
            patch(
                "app.services.ec2_worker.get_settings",
                return_value=_settings(instance_id="  "),
            ),
            patch("app.services.ec2_worker._wake_worker", new_callable=AsyncMock) as wake,
        ):
            async def _run() -> None:
                ec2_worker.schedule_wake_worker()
                await asyncio.sleep(0)

            asyncio.run(_run())
            wake.assert_not_called()

    def test_production_schedules_wake_without_awaiting(self) -> None:
        started = asyncio.Event()

        async def _wake() -> None:
            started.set()

        with patch("app.services.ec2_worker.get_settings", return_value=_settings()):
            async def _run() -> None:
                with patch("app.services.ec2_worker._wake_worker", new=_wake):
                    ec2_worker.schedule_wake_worker()
                    self.assertFalse(started.is_set())
                    await started.wait()

            asyncio.run(_run())

    def test_wake_swallows_aws_errors(self) -> None:
        with patch(
            "app.services.ec2_worker._start_if_stopped",
            side_effect=RuntimeError("ec2 down"),
        ):
            asyncio.run(ec2_worker._wake_worker())

    def test_wake_retries_while_stopping_then_starts(self) -> None:
        states = ["stopping", "started"]

        with (
            patch(
                "app.services.ec2_worker._start_if_stopped",
                side_effect=lambda: states.pop(0),
            ),
            patch("app.services.ec2_worker.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            asyncio.run(ec2_worker._wake_worker())
        sleep.assert_awaited()
        self.assertEqual(states, [])

    def test_wake_gives_up_if_still_stopping(self) -> None:
        with (
            patch(
                "app.services.ec2_worker._start_if_stopped",
                return_value="stopping",
            ),
            patch("app.services.ec2_worker.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("app.services.ec2_worker._WAKE_WHILE_STOPPING_SECONDS", 0),
        ):
            asyncio.run(ec2_worker._wake_worker())
        sleep.assert_not_called()


class MaybeWakeWorkerTests(unittest.TestCase):
    def test_local_env_does_not_create_task(self) -> None:
        with (
            patch("app.services.ec2_worker.get_settings", return_value=_settings(production=False)),
            patch("app.services.ec2_worker._maybe_wake_worker", new_callable=AsyncMock) as wake,
        ):
            async def _run() -> None:
                ec2_worker.maybe_wake_worker()
                await asyncio.sleep(0)

            asyncio.run(_run())
            wake.assert_not_called()

    def test_skips_wake_when_nothing_claimable(self) -> None:
        with (
            patch("app.pipeline.quota.has_claimable_job", new_callable=AsyncMock, return_value=False),
            patch("app.services.ec2_worker._wake_worker", new_callable=AsyncMock) as wake,
        ):
            asyncio.run(ec2_worker._maybe_wake_worker())
        wake.assert_not_called()

    def test_wakes_when_a_job_is_claimable(self) -> None:
        with (
            patch("app.pipeline.quota.has_claimable_job", new_callable=AsyncMock, return_value=True),
            patch("app.services.ec2_worker._wake_worker", new_callable=AsyncMock) as wake,
        ):
            asyncio.run(ec2_worker._maybe_wake_worker())
        wake.assert_awaited_once()


class InsertJobDoesNotWakeTests(unittest.TestCase):
    def test_action_insert_does_not_wake(self) -> None:
        from app.db import repository as repo

        db = MagicMock()
        db.jobs.insert_one = AsyncMock()
        with (
            patch("app.db.repository.get_db", return_value=db),
            patch("app.services.ec2_worker.schedule_wake_worker") as wake,
            patch("app.services.ec2_worker.maybe_wake_worker") as maybe,
        ):
            job_id = asyncio.run(repo.insert_job({"_id": "job_1"}))
        self.assertEqual(job_id, "job_1")
        db.jobs.insert_one.assert_awaited_once_with({"_id": "job_1"})
        wake.assert_not_called()
        maybe.assert_not_called()

    def test_action_insert_does_not_wake_if_mongo_fails(self) -> None:
        from app.db import repository as repo

        db = MagicMock()
        db.jobs.insert_one = AsyncMock(side_effect=RuntimeError("mongo down"))
        with (
            patch("app.db.repository.get_db", return_value=db),
            patch("app.services.ec2_worker.maybe_wake_worker") as maybe,
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(repo.insert_job({"_id": "job_1"}))
        maybe.assert_not_called()

    def test_balltrack_insert_does_not_wake(self) -> None:
        from app.balltrack import repo as bt_repo

        col = MagicMock()
        col.insert_one = AsyncMock()
        db = MagicMock()
        db.__getitem__.return_value = col
        with (
            patch("app.balltrack.repo.get_db", return_value=db),
            patch("app.services.ec2_worker.schedule_wake_worker") as wake,
            patch("app.services.ec2_worker.maybe_wake_worker") as maybe,
        ):
            job_id = asyncio.run(bt_repo.insert_job({"_id": "btj_1"}))
        self.assertEqual(job_id, "btj_1")
        col.insert_one.assert_awaited_once_with({"_id": "btj_1"})
        wake.assert_not_called()
        maybe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
