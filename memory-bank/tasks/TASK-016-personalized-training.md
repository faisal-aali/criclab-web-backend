# TASK-016 — Personalized AI Training (backend)

## Goal

Expose per-user Action delivery trends and an AI-coached training plan through the website API. Code computes; Gemma narrates.

## Done

- `app/coaching/trends.py` — deterministic aggregation over `deliveries` (trend series, averages, bests, deltas, focus-area frequency). Only `status === ok` values are used.
- `app/coaching/recommend.py` — added `allowed_drill_summaries`, `hydrate_recommendations`, `fallback_picks` so backend can verify LLM drill picks against the local catalog.
- `app/agent/training_coach.py` — Gemma plan generator that receives the stats JSON, returns labeled prose + a JSON recommendations block, then hydrates/falls back to real catalog IDs.
- `app/api/training.py` — `GET /training/profile` (fast stats) and `GET /training/plan` (cached plan, regenerates on `?refresh=true`, new deliveries, or 24h staleness).
- `app/db/training_repo.py` + `indexes.py` — `training_plans` collection with unique `user_id` index.
- `app/main.py` — registered `training.router`.
- `criclab-video-service/app/agent/ollama_agent.py` — `generate_report` now returns `"weakness_tags"` so the worker persists them on each delivery.

## Verification

- `python -m compileall app` in `.venv312`.
- `GET /training/profile` and `/training/plan` with real/seeded deliveries.
- Confirm 401/404 ownership behavior and `llm_unavailable` fallback when Ollama is down.
- Cross-check computed trend values against raw delivery docs.

## Notes

- Action deliveries only for v1. Ball-flight trends remain out of scope.
- No new LLM/embedding model — reuses existing `gemma3:4b` (Ollama/Bedrock).
- The old `coaching memory` (FEAT-015) semantic-search vision is still deferred; this task is the deterministic, stats-first step.
