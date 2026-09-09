from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, Query

from app.agent.training_coach import generate_training_plan
from app.api.deps import CurrentUser, VerifiedUser
from app.coaching.recommend import allowed_drill_summaries
from app.coaching.trends import build_training_profile
from app.db import repository as repo
from app.db import training_repo

router = APIRouter(prefix="/training", tags=["training"])

PLAN_STALE_HOURS = 24
GENERATION_STALE_SECONDS = 120

log = logging.getLogger("criclab.api.training")

# Process-local locks prevent two concurrent handlers for the same user from both
# starting a generation before the Mongo state is updated. This covers the single
# always-on EC2 deployment; a multi-process farm would need a distributed lock.
_generation_locks: dict[str, asyncio.Lock] = {}


def _is_stale(generated_at: datetime | Any) -> bool:
    if not isinstance(generated_at, datetime):
        return True
    return datetime.now(timezone.utc) - generated_at > timedelta(hours=PLAN_STALE_HOURS)


def _lock(user_id: str) -> asyncio.Lock:
    return _generation_locks.setdefault(user_id, asyncio.Lock())


async def _generate_plan(
    user_id: str,
    profile: dict[str, Any],
    allowed_drills: list[dict[str, Any]],
    delivery_count: int,
) -> None:
    """Background task: run the LLM and persist the result."""
    try:
        plan = await generate_training_plan(profile, allowed_drills=allowed_drills)
        await training_repo.mark_ready(user_id, plan, delivery_count)
        log.info("Training plan ready for user %s", user_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("Training plan generation failed for user %s", user_id)
        await training_repo.mark_failed(user_id, str(exc))


@router.get("/profile")
async def get_training_profile(user: CurrentUser):
    """Deterministic stats for the current user's Action deliveries."""
    deliveries = await repo.list_deliveries(limit=50, user_id=user["_id"])
    profile = build_training_profile(deliveries)
    return profile


@router.get("/plan")
async def get_training_plan(
    user: VerifiedUser,
    refresh: bool = Query(False),
):
    """Fast endpoint that returns the latest profile and a plan that may be cached,
    generating, or failed. New generations are scheduled in the background.
    """
    deliveries = await repo.list_deliveries(limit=50, user_id=user["_id"])
    profile = build_training_profile(deliveries)

    focus_tags = [f["tag"] for f in profile.get("focus_areas", [])]
    bowling_style = (profile.get("player_profile") or {}).get("bowling_style")
    allowed_drills = allowed_drill_summaries(focus_tags, bowling_style=bowling_style)

    delivery_count = len(deliveries)

    async with _lock(str(user["_id"])):
        cached = await training_repo.get_plan(user["_id"])
        cached_plan = cached.get("plan") if cached else None
        cached_plan_status = cached_plan.get("status") if isinstance(cached_plan, dict) else None

        is_ready = (
            cached
            and cached.get("generation_status") == "ready"
            and cached_plan_status == "ok"
            and cached.get("delivery_count") == delivery_count
            and not _is_stale(cached.get("generated_at"))
        )

        # 1. Best case: a good plan that matches the current data. Even an explicit
        #    refresh is a no-op here, because regenerating on identical inputs is
        #    just burning tokens for the same answer.
        if is_ready:
            return {
                "profile": profile,
                "plan": cached_plan,
                "generation_status": "ready",
                "generated_at": cached.get("generated_at"),
                "cached": True,
                "needs_refresh": False,
            }

        # 2. A generation is currently running and has not gone stale.
        if training_repo.is_generation_active(cached, stale_seconds=GENERATION_STALE_SECONDS):
            return {
                "profile": profile,
                "plan": cached_plan,
                "generation_status": "generating",
                "generated_at": cached.get("generated_at"),
                "cached": False,
                "needs_refresh": False,
            }

        # 3. Start a fresh generation only when it actually makes sense:
        #    - the user explicitly asked for a refresh, or
        #    - there is no cached plan at all, or
        #    - the previous generation failed or was abandoned (stale "generating").
        #    We do NOT auto-retry an `llm_unavailable` plan unless the user clicks
        #    refresh, to avoid burning tokens when the model is down.
        has_plan = cached_plan is not None
        is_abandoned = (
            cached
            and cached.get("generation_status") == "generating"
            and not training_repo.is_generation_active(cached, stale_seconds=GENERATION_STALE_SECONDS)
        )
        should_start = (
            refresh
            or not cached
            or not has_plan
            or cached.get("generation_status") == "failed"
            or is_abandoned
        )
        if should_start:
            await training_repo.mark_generating(user["_id"], delivery_count)
            asyncio.create_task(
                _generate_plan(
                    str(user["_id"]),
                    profile,
                    allowed_drills,
                    delivery_count,
                )
            )

            return {
                "profile": profile,
                "plan": cached_plan,
                "generation_status": "generating",
                "generated_at": cached.get("generated_at") if cached else None,
                "cached": False,
                "needs_refresh": False,
            }

        # 4. A plan exists but is out of date (new deliveries or >24h old) and the
        #    user has not asked for a refresh yet. Return the cached plan and ask
        #    the UI to show the refresh prompt. If the plan is not ok (e.g.
        #    llm_unavailable), we do not flag it as "needs_refresh"; the UI can
        #    still offer a retry through the header refresh button.
        needs_refresh = (
            has_plan
            and cached_plan_status == "ok"
            and (
                cached.get("delivery_count") != delivery_count
                or _is_stale(cached.get("generated_at"))
            )
        )

        return {
            "profile": profile,
            "plan": cached_plan,
            "generation_status": "ready",
            "generated_at": cached.get("generated_at") if cached else None,
            "cached": True,
            "needs_refresh": needs_refresh,
        }
