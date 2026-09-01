from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import get_settings

log = logging.getLogger("criclab")
_pending: set[asyncio.Task[Any]] = set()


def schedule_wake_worker() -> None:
    settings = get_settings()
    if not settings.is_production or not (settings.worker_ec2_instance_id or "").strip():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_wake_worker())
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _wake_worker() -> None:
    try:
        await asyncio.to_thread(_start_if_stopped)
    except Exception as exc:
        log.warning("worker EC2 wake failed: %s: %s", type(exc).__name__, exc)


def _start_if_stopped() -> None:
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
        return
    state = ((instances[0].get("State") or {}).get("Name") or "").lower()
    if state != "stopped":
        log.info("worker EC2 %s state=%s; not starting", instance_id, state)
        return
    client.start_instances(InstanceIds=[instance_id])
    log.info("worker EC2 %s start requested", instance_id)
