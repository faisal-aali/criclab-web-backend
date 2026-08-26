"""Starter coach profiles.

Written once, when the collection is empty, so a fresh install has a working
calendar instead of an empty page. Anything an administrator changes afterwards
survives — this never overwrites an existing profile.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.mongo import get_db
from app.services import booking_service

log = logging.getLogger("criclab.coaching")

# Evening and weekend windows, in each coach's own timezone. Weekday numbers
# follow date.weekday(): Monday is 0.
SEED: list[dict[str, Any]] = [
    {
        "slug": "asif-mehmood",
        "name": "Asif Mehmood",
        "title": "Fast bowling coach",
        "headline": "Run-up rhythm, front-arm timing and repeatable release.",
        "bio": (
            "Twelve years coaching seam bowling at academy and first-class level. "
            "Asif works from your own footage, one change at a time, and is "
            "unusually good at making a longer run-up quieter rather than faster."
        ),
        "specialities": ["Seam", "Run-up", "Release", "Injury-safe workload"],
        "languages": ["English", "Urdu"],
        "accent": "lime",
        "timezone": "Asia/Karachi",
        "display_order": 10,
        "session_types": [
            {"id": "review-30", "label": "Clip review", "minutes": 30,
             "description": "One delivery, looked at properly. Best first session."},
            {"id": "session-60", "label": "Full session", "minutes": 60,
             "description": "Footage, drills and a plan for the next four weeks."},
        ],
        "availability": [
            {"weekday": 1, "start": "16:00", "end": "20:00"},
            {"weekday": 3, "start": "16:00", "end": "20:00"},
            {"weekday": 5, "start": "09:00", "end": "13:00"},
        ],
    },
    {
        "slug": "hana-qureshi",
        "name": "Hana Qureshi",
        "title": "Spin and biomechanics",
        "headline": "Wrist position, pivot and keeping an action legal under load.",
        "bio": (
            "Biomechanics background, now coaching spin full time. Hana reads "
            "elbow and shoulder data carefully and will tell you plainly when a "
            "single camera cannot answer the question you are asking."
        ),
        "specialities": ["Off spin", "Leg spin", "Elbow extension", "Pivot"],
        "languages": ["English", "Urdu", "Punjabi"],
        "accent": "seam",
        "timezone": "Asia/Karachi",
        "display_order": 20,
        "session_types": [
            {"id": "review-30", "label": "Clip review", "minutes": 30,
             "description": "A close read of one spell, frame by frame."},
            {"id": "session-45", "label": "Technical session", "minutes": 45,
             "description": "Action work with drills you can do in a net alone."},
        ],
        "availability": [
            {"weekday": 0, "start": "17:00", "end": "21:00"},
            {"weekday": 2, "start": "17:00", "end": "21:00"},
            {"weekday": 6, "start": "10:00", "end": "14:00"},
        ],
    },
    {
        "slug": "daniel-okafor",
        "name": "Daniel Okafor",
        "title": "Performance and workload",
        "headline": "Turning a season of sessions into a plan you can hold.",
        "bio": (
            "Strength and conditioning coach who works with bowlers specifically. "
            "Daniel is the one to book if the problem is not the action but how "
            "much of it you are doing, and when."
        ),
        "specialities": ["Workload", "Strength", "Return to bowling", "Season planning"],
        "languages": ["English"],
        "accent": "warm",
        "timezone": "Europe/London",
        "display_order": 30,
        "session_types": [
            {"id": "consult-30", "label": "Consultation", "minutes": 30,
             "description": "Where you are now and what the next block should be."},
            {"id": "plan-60", "label": "Season plan", "minutes": 60,
             "description": "A written plan for the next twelve weeks."},
        ],
        "availability": [
            {"weekday": 1, "start": "07:00", "end": "10:00"},
            {"weekday": 4, "start": "07:00", "end": "10:00"},
            {"weekday": 5, "start": "14:00", "end": "18:00"},
        ],
    },
]


async def seed_coaches() -> int:
    """Insert the starter profiles if there are none. Returns how many were added."""
    try:
        if await get_db().coaches.count_documents({}, limit=1):
            return 0
        added = 0
        for profile in SEED:
            try:
                await booking_service.upsert_coach(profile)
                added += 1
            except booking_service.BookingError as exc:
                log.warning("skipped seed coach %s: %s", profile.get("slug"), exc)
        if added:
            log.info("seeded %d coach profiles", added)
        return added
    except Exception as exc:  # never block startup
        log.warning("coach seeding skipped: %s", type(exc).__name__)
        return 0
