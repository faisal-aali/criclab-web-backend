# Agent Rules — Cric-Lab Backend Memory Bank

How the agent should behave in the **criclab-web-backend** repo.

## Session start (mandatory)

1. Read **all** Memory Bank files before non-trivial work:
   - `productBrief.md`
   - `techContext.md`
   - `systemPatterns.md` (highest priority for architecture)
   - `roadmap.md`
   - Relevant `tasks/`
   - These `agent-rules/`
2. Summarize goals, constraints, and gaps **before** coding when the task is large.
3. Do **not** invent Notera notes/PWA patterns, Hybrid CRM, or Next.js-as-frontend defaults — this product is **Cric-Lab** (website FastAPI; UI is `criclab-web-frontend`; CV workers are `criclab-video-service`).

## Context over prompts

- Prefer Memory Bank over assumptions
- If a prompt conflicts with `systemPatterns.md`, follow `systemPatterns.md` and say so
- UX inspiration may reference SpinLab AI; domain remains cricket **bowling** for v1

## Planning before coding

For non-trivial work:

1. Restate requirements against `productBrief.md` + task
2. Outline approach against `systemPatterns.md` (pipeline stages)
3. List files to touch under `app/` (api / coaching catalog I/O / assistant / db). Pose, PDF, and drill matching belong in `criclab-video-service`.
4. Implement

## Implementation rules

- This repo: FastAPI queue insert + reads; catalog HTTP; chat assistant; daily quota schedule/notify; claimable worker EC2 wake + UTC midnight catch-up
- Video CV / overlay / PDF / Gemma video notes / drill matching: `criclab-video-service`
- LLM here: Ollama `gemma3:4b` for the **chat assistant**; `nomic-embed-text` for RAG
- Persist analyses in MongoDB (workers write deliveries; this API reads them)
- Label physical metrics as estimates unless calibrated + validated
- Prefer reliable bowling MVP over multi-sport / batting features
- Keep the three-repo split — do not add Vite/React or MediaPipe pipeline sources here

## Demo prompts

| Prompt | Expected behavior |
|--------|-------------------|
| `Read all memory bank files.` | Explain Cric-Lab goals, stack, pipeline, risks — no code yet |
| Implement upload → metrics | This API queues the job; worker runs CV; no LLM measuring frames |
| Add a new bowling metric | Extend worker metrics + API payload; UI cards live in criclab-web-frontend |

## Updates

When asked to **update the memory bank**:

- Sync `roadmap.md` status and `tasks/` with reality
- Record architecture decisions in `systemPatterns.md`
- Keep files concise — this is the agent’s persistent project memory
