"""Index definitions for the account, support, coaching and notification data.

Created once at startup. Every index here exists because a query in the app
would otherwise be a collection scan, or because a uniqueness rule needs to be
enforced by the database rather than by application code that can race.

TTL indexes do the expiry work: one-time codes, refresh sessions and rate-limit
counters delete themselves rather than needing a sweeper job.
"""

from __future__ import annotations

import logging

from pymongo import ASCENDING, DESCENDING, IndexModel

from app.db.mongo import get_db

log = logging.getLogger("criclab.indexes")

# collection -> indexes
INDEXES: dict[str, list[IndexModel]] = {
    "users": [
        # Uniqueness enforced here, not by a read-then-write in the signup route,
        # which two concurrent signups could both pass.
        IndexModel([("email", ASCENDING)], unique=True, name="uniq_email"),
        IndexModel([("created_at", DESCENDING)], name="created_desc"),
    ],
    "sessions": [
        # Refresh tokens are looked up by digest on every renewal.
        IndexModel([("token_digest", ASCENDING)], unique=True, name="uniq_token_digest"),
        IndexModel([("user_id", ASCENDING), ("revoked_at", ASCENDING)], name="user_active"),
        # Mongo removes the row itself once the session is past its expiry.
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl_expiry"),
    ],
    "otps": [
        IndexModel([("user_id", ASCENDING), ("purpose", ASCENDING)], name="user_purpose"),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl_expiry"),
    ],
    "notifications": [
        # The notification centre lists newest-first, filtered by read state.
        IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_recent"),
        IndexModel([("user_id", ASCENDING), ("read_at", ASCENDING)], name="user_unread"),
    ],
    "tickets": [
        IndexModel([("user_id", ASCENDING), ("updated_at", DESCENDING)], name="user_recent"),
        IndexModel([("status", ASCENDING), ("updated_at", DESCENDING)], name="status_recent"),
    ],
    "ticket_messages": [
        IndexModel([("ticket_id", ASCENDING), ("created_at", ASCENDING)], name="thread"),
    ],
    "coaches": [
        IndexModel([("active", ASCENDING), ("display_order", ASCENDING)], name="active_order"),
        IndexModel([("slug", ASCENDING)], unique=True, name="uniq_slug"),
    ],
    "bookings": [
        IndexModel([("user_id", ASCENDING), ("starts_at", DESCENDING)], name="user_upcoming"),
        # Stops the same coach being double-booked for one slot. Partial so that
        # cancelled bookings free the slot again.
        IndexModel(
            [("coach_id", ASCENDING), ("starts_at", ASCENDING)],
            unique=True,
            name="uniq_coach_slot",
            partialFilterExpression={"status": {"$in": ["confirmed", "pending"]}},
        ),
    ],
    "rate_limits": [
        IndexModel([("key", ASCENDING)], unique=True, name="uniq_key"),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl_expiry"),
    ],
    "security_events": [
        IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_recent"),
        # Audit rows are kept for 180 days, then removed automatically.
        IndexModel([("created_at", ASCENDING)], expireAfterSeconds=60 * 60 * 24 * 180, name="ttl_180d"),
    ],
    "kb_chunks": [
        IndexModel([("source", ASCENDING)], name="by_source"),
    ],
    # Existing analysis collections — these were previously unindexed and the
    # history listing scanned every delivery.
    "deliveries": [
        IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_recent"),
        IndexModel([("created_at", DESCENDING)], name="created_desc"),
    ],
    "jobs": [
        IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_recent"),
    ],
}


async def ensure_indexes() -> None:
    """Create every index. Safe to run on each boot — existing ones are no-ops."""
    db = get_db()
    for collection, models in INDEXES.items():
        try:
            await db[collection].create_indexes(models)
        except Exception as exc:  # never block startup on an index conflict
            log.warning("index setup failed for %s: %s", collection, exc)
