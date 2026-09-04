# Memory Bank — Cric-Lab (Website API)

Structured source of truth the AI reads first for the **Cric-Lab website FastAPI** (accounts, upload, job queue, catalog HTTP, chat, MongoDB reads).

Sibling UI: `../criclab-web-frontend`. Sibling workers: `../criclab-video-service` (pose, overlay, PDF, drill matching).

## Layout

```text
memory-bank/
├── productBrief.md      # What Cric-Lab is — users, features, success
├── techContext.md       # FastAPI · MongoDB · Ollama · CV stack
├── systemPatterns.md    # Architecture rules — MOST IMPORTANT FILE
├── roadmap.md           # Features FEAT-001 …
├── tasks/               # Implementation / maintenance tasks
└── agent-rules/         # How the agent should behave
```

## Core files

| File | Role |
|------|------|
| `productBrief.md` | Cricket bowling lab: upload → analyze → metrics → PDF |
| `techContext.md` | FastAPI website API, MongoDB, chat assistant; CV is the sibling worker |
| `systemPatterns.md` | Queue vs worker ownership; two film modes; catalog HTTP vs matching |
| `roadmap.md` | MVP bowling workflow + phased expansion |

## Why this exists

Vague prompts force guessing. The Memory Bank encodes product intent and architecture so the agent behaves like a teammate who already knows Cric-Lab.

**Key lesson:** context changes output more than prompts.

## How to use

1. Ask: `Read all memory bank files.` — understanding before coding
2. Implement from a task under `tasks/` while obeying `systemPatterns.md`
3. To change architecture for the same feature, update `systemPatterns.md` first, then re-run the prompt

## Not Notera / SpinLab product code

This folder previously described **Notera**. It now describes **Cric-Lab only**. UX inspiration comes from SpinLab AI; domain is cricket bowling, not baseball/throw sports generally (unless later expanded).
