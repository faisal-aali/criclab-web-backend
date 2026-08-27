# Memory Bank — Cric-Lab (Backend)

Structured source of truth the AI reads first for the **Cric-Lab FastAPI backend** (CV pipeline, agent, PDF, MongoDB).

Sibling frontend repo: `../criclab-web-frontend` (Vite + React UI + its own trimmed memory-bank).

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
| `techContext.md` | FastAPI Python BE, MongoDB, Ollama Gemma/nomic, MediaPipe |
| `systemPatterns.md` | CV metrics first, then agent; modular pipeline; PDF reports |
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
