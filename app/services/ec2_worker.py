from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.config import get_settings

log = logging.getLogger("criclab")
_pending: set[asyncio.Task[Any]] = set()

_STOPPING_STATES = frozenset({"stopping", "shutting-down"})
_WAKE_WHILE_STOPPING_SECONDS = 120.0
_WAKE_WHILE_STOPPING_INTERVAL = 5.0


def schedule_wake_worker() -> None:
    """Start the worker EC2 if it is stopped. No-op locally and when already up."""
    settings = get_settings()
    if not settings.is_production or not (settings.worker_ec2_instance_id or "").strip():
        log.debug("worker wake skipped APP_ENV=%s", settings.app_env)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_wake_worker())
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def maybe_wake_worker() -> None:
    """Start the worker only when a clip can actually begin now."""
    settings = get_settings()
    if not settings.is_production or not (settings.worker_ec2_instance_id or "").strip():
        log.debug("worker wake skipped APP_ENV=%s", settings.app_env)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_maybe_wake_worker())
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _maybe_wake_worker() -> None:
    from app.pipeline import quota

    try:
        if not await quota.has_claimable_job():
            return
    except Exception as exc:
        log.warning("claimable-job check failed: %s: %s", type(exc).__name__, exc)
        return
    await _wake_worker()


async def _wake_worker() -> None:
    deadline = time.monotonic() + _WAKE_WHILE_STOPPING_SECONDS
    while True:
        try:
            state = await asyncio.to_thread(_start_if_stopped)
        except Exception as exc:
            log.warning("worker EC2 wake failed: %s: %s", type(exc).__name__, exc)
            return
        if state != "stopping":
            return
        if time.monotonic() >= deadline:
            log.warning(
                "worker EC2 still stopping after %.0fs; giving up",
                _WAKE_WHILE_STOPPING_SECONDS,
            )
            return
        await asyncio.sleep(_WAKE_WHILE_STOPPING_INTERVAL)


def _start_if_stopped() -> str:
    """Start the worker instance if stopped. Returns the EC2 state we acted on."""
    settings = get_settings()
    instance_id = (settings.worker_ec2_instance_id or "").strip()
    kwargs: dict[str, str] = {"region_name": settings.worker_ec2_region}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    import boto3

    client = boto3.client("ec2", **kwargs)
    reservations = client.describe_instances(InstanceIds=[instance_id]).get("Reservations") or []
    instances = (reservations[0].get("Instances") or []) if reservations else []
    if not instances:
        log.warning("worker EC2 %s not found in %s", instance_id, settings.worker_ec2_region)
        return "missing"
    state = ((instances[0].get("State") or {}).get("Name") or "").lower()
    if state in _STOPPING_STATES:
        log.info("worker EC2 %s state=%s; waiting to start", instance_id, state)
        return "stopping"
    if state != "stopped":
        log.info("worker EC2 %s state=%s; not starting", instance_id, state)
        return state
    client.start_instances(InstanceIds=[instance_id])
    log.info("worker EC2 %s start requested", instance_id)
    return "started"
