# TASK-014 — Daily video-processing quota

**Feature:** FEAT-031 Daily video quota  
**Status:** Done  
**Priority:** P1

## Goal

Cap video starts at `DAILY_VIDEO_QUOTA` (default 60) per UTC day, overflow FIFO to later days, expose `expected_start_at`, and notify when the promised UTC date moves.

## Acceptance criteria

- [x] `DAILY_VIDEO_QUOTA` in Settings / `.env.example` (default 60)
- [x] Website API assigns `available_at`, `scheduled_date`, `expected_start_at` on insert (Action + Ball-flight share one FIFO)
- [x] Job GET / `/jobs/active` return those fields; `eta_seconds` still for running jobs
- [x] Queued cancel: `POST /jobs/{id}/cancel` and `POST /balltrack/jobs/{id}/cancel`
- [x] In-app `kind=analysis` on insert; email when deferred to a later UTC day; date-change notify on cancel/fail/capacity
- [x] Lifespan loop drains `quota_state.dirty_at` (worker fail/complete/stale-requeue)
- [x] Pure scheduler tests in `tests/test_quota.py`
- [x] Poke worker EC2 only when a clip is claimable now; lifespan boot + 00:00 UTC catch-up (no cron)

## Notes

The worker owns `quota_days.started`. This API never increments it. Slots are released on fail and stale re-queue, not on complete, queued-cancel, or in-flight cancel (the start already counted). `insert_job` does not start the worker; `schedule_queued_jobs` may, via `maybe_wake_worker`.
