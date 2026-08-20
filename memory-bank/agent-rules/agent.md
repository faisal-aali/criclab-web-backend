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
3. Do **not** invent Notera notes/PWA patterns, Hybrid CRM, or Next.js-as-frontend defaults — this product is **Cric-Lab** (FastAPI BE; UI is the sibling `criclab-web-frontend` repo).

## Context over prompts

- Prefer Memory Bank over assumptions
- If a prompt conflicts with `systemPatterns.md`, follow `systemPatterns.md` and say so
- UX inspiration may reference SpinLab AI; domain remains cricket **bowling** for v1

## Planning before coding

For non-trivial work:

1. Restate requirements against `productBrief.md` + task
2. Outline approach against `systemPatterns.md` (pipeline stages)
3. List files to touch under `app/` (api / pipeline / balltrack / agent / pdf / db)
4. Implement

## Implementation rules

- Backend: FastAPI orchestrates; CV/metrics in modular `pipeline/`
- LLM: Ollama `gemma3:4b` for reasoning only; `nomic-embed-text` for memory search
- Persist analyses in MongoDB
- Label physical metrics as estimates unless calibrated + validated
- Ship PDF generation as part of the analysis completion path
- Prefer reliable bowling MVP over multi-sport / batting features
- Keep the frontend/backend repo split — do not add Vite/React sources here

## Demo prompts

| Prompt | Expected behavior |
|--------|-------------------|
| `Read all memory bank files.` | Explain Cric-Lab goals, stack, pipeline, risks — no code yet |
| Implement upload → metrics | Modular pipeline; no LLM measuring frames |
| Add a new bowling metric | Extend metrics stage + API payload; UI cards live in criclab-web-frontend |

## Updates

When asked to **update the memory bank**:

- Sync `roadmap.md` status and `tasks/` with reality
- Record architecture decisions in `systemPatterns.md`
- Keep files concise — this is the agent’s persistent project memory
