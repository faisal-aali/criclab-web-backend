# TASK-017 — Cross-system audit fixes (website API)

**Feature:** FEAT-035
**Status:** Done
**Date:** 10 Sep 2026

Part of one audit across `criclab-web-frontend`, this API, `criclab-video-service`
and the Expo app (`~/Desktop/cric-lab-ai`). Pending items the product has
marked `For Future` (hidden nav entries, batting copy, coach CRUD UI, booking
calendar, signed artifact URLs) were left alone on purpose.

## Bugs fixed here

| # | Where | What was wrong | Fix |
|---|---|---|---|
| 1 | `api/auth.py` `POST /auth/resend-otp` | `body: EmailIn` + `purpose: Body(embed=True)` → FastAPI wanted `{"body": {...}, "purpose": ...}`; web and phone send `{email, purpose}` → **422 on every resend** (verified with `TestClient`) | `ResendIn(EmailIn)` carries `purpose` |
| 2 | `api/deps.py` `optional_user` | Ignored `disabled` and `pwd_at` | Same rules as `current_user`, returns `None` |
| 3 | `db/auth_repo.py` `hit_rate_limit` | `find_one` then `update_one` — concurrent attempts all read the same count | Atomic `find_one_and_update` `$inc` on a live window; upsert with `DuplicateKeyError` fallback |
| 4 | `api/balltrack.py` `POST /balltrack/detect-stumps` | Anonymous, uncapped image → OpenCV in-process | `CurrentUser` + 15 MB cap |
| 5 | `api/balltrack.py` `POST /balltrack/sessions` | Multipart streamed with no ceiling; `source_key` never size-checked | 180 MB cap on both paths (worker's `MAX_BYTES`), same reject sentence |
| 6 | `api/balltrack.py` `_public_session` / `_public_delivery` | `**art` spread returned `clip_path` (worker filesystem path) and raw S3 keys | Named fields only |
| 7 | `api/videos.py`, `api/balltrack.py` | `limit` was a bare `int` | `Query(50, ge=1, le=200)` |
| 8 | `services/admin_service.py` | "processing" counts and the in-progress list omitted `claimed`; `list_analyses.total` was the size of the over-fetched window | `claimed` added; `total` from `count_documents` per collection |
| 9 | `.github/workflows/ci.yml`, `tests/test_ec2_worker.py` | CI compiled on 3.11 and never ran tests; the EC2 fixture lacked `app_env` (3 errors); `cryptography` missing locally | 3.12, `pip install`, `unittest discover`; fixture fixed |

`python -m unittest discover -s tests` → 77 tests OK. `TestClient` checks:
flat resend → 200, bad purpose → 400, `/deliveries?limit=999` → 422 after
auth, anonymous `detect-stumps` → 401.

## Found, not changed (deferred or a decision for the owner)

- `GET /artifacts/{job_id}/{filename}`, `GET /media/videos/{filename}`,
  `GET /balltrack/media/{job_id}/{filename}` serve files without an ownership
  check. `<video>`/`<img>` cannot send a bearer header; the proper fix is
  signed, expiring URLs, listed as deferred in TASK-012. In production the
  same files come from CloudFront signed GETs.
- `POST/PUT/DELETE /admin/drills` write `app/coaching/drills.json` inside the
  git working tree; `deploy/pull.sh` merges `--ff-only`, so an admin edit on
  the box blocks the next deploy. The worker keeps its own snapshot.
- `vercel.json` / `.vercel/project.json` describe a serverless deploy the
  lifespan loops (quota recompute, midnight wake) cannot survive; production
  is EC2 + PM2 (`DEPLOY.md`).
- `Settings.worker_ec2_instance_id` defaults to a live instance id.
- `PUT /coaching/admin/coaches` takes a raw dict (coach CRUD UI is deferred).
- `requirements.txt` and `pyproject.toml` list the same dependencies twice.
