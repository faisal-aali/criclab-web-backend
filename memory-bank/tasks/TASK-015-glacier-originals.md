# TASK-015 — Glacier Flexible Retrieval for finished originals

**Feature:** FEAT-032 Glacier originals  
**Status:** Done  
**Priority:** P1  
**Date:** 3 Sep 2026

## Goal

Move `original/` to Glacier Flexible Retrieval (`StorageClass=GLACIER`) as soon as the job will not run again. Do not use a 7-day lifecycle on the prefix — quota overflow can leave a job queued longer than a week.

## When to archive

- `completed` (worker)
- `failed` (worker, stale `processing`/`analyzing`, API stale-fail)
- `cancelled` only if the job was still `queued`

Never archive `queued` / `claimed` / `processing` / `analyzing`. Stale `claimed` is re-queued. Skip if another live job shares `source_key`.

## Acceptance criteria

- [x] `archive_original` + `head_original` in this API's `s3_service`
- [x] Queued-cancel archives; in-flight cancel does not
- [x] API stale-fail archives; orphan sweep on that path
- [x] POST `/videos` and `/balltrack` reject an already-Glacier `source_key` (400, “Upload the clip again”)
- [x] `videos.source_key` and `balltrack_sessions.source_key` indexes
- [x] Persist `source_storage_class` / `source_archived_at`
- [x] Archive failures do not fail the job
- [x] No user-visible Glacier / S3 wording

## Notes

90-day minimum bill for Glacier Flexible Retrieval. Abandoned presigned PUTs (never POSTed) stay Standard. Playback prefixes stay Standard. RestoreObject is out of scope.
