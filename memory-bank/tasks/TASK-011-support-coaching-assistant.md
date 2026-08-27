# TASK-011 — Support ticketing, coaching bookings, RAG assistant

**Feature:** FEAT-023 (support), FEAT-024 (coaching), FEAT-025 (assistant)
**Status:** Done
**Priority:** P0
**Date:** 27 Aug 2026

## What exists now

The three modules TASK-010 left with only collections and indexes in place.

### Support — `app/services/support_service.py`, `app/api/support.py`

* Tickets get a readable reference (`CL-XXXXXX`, no ambiguous characters —
  dictate-safe), not the raw Mongo id.
* Status is **derived**, never set directly: a user reply moves a ticket to
  `awaiting_support`, a staff reply to `awaiting_user`. A reply to a closed
  ticket is refused; a reply to a resolved one reopens it within a 21-day
  window and otherwise stays closed.
* Attachments: filename is a display label only; stored under a server-chosen
  id and path, type decided by an allow-list against the declared content-type.
  Served with `X-Content-Type-Options: nosniff` — never rendered inline
  regardless of what the upload claims to be.
* Every handler scopes by `user_id` in the query filter. Cross-account reads
  return 404, not 403 — no signal that the ticket exists.
* Staff queue (`/support/queue*`) is `AdminUser`-gated and is the only place
  tickets are visible across accounts.

### Coaching — `app/services/booking_service.py`, `app/api/bookings.py`,
`app/coaching/coaches_seed.py`

* Availability is **computed**, not materialised: weekly windows in the coach's
  own timezone, minus live bookings in the horizon. No nightly job to extend
  the calendar or migration when a coach's hours change.
* Double-booking is stopped by the existing unique partial index on
  `(coach_id, starts_at)` (`pending|confirmed` only) — `create_booking` inserts
  and lets Mongo reject the loser. Checking-then-inserting would race.
* Reschedule inserts the new booking **before** cancelling the old one, so a
  failed insert (slot taken between page-load and submit) leaves the original
  intact.
* `_slot_is_offered()` re-derives the calendar and confirms the requested
  instant is actually on it — the client sends a timestamp, not a slot
  reference, so this is what stops a hand-crafted time outside any window.
* Times render in the coach's own timezone with the zone named, plus a "your
  time" hint when it differs from the viewer's — silently converting to viewer
  local is how people turn up an hour late.
* Three starter coach profiles seed once on an empty collection; never
  overwrites an existing one.

### Assistant — `app/assistant/rag.py`, `app/assistant/chat.py`,
`app/api/assistant.py`, `app/assistant/knowledge/*.md`

* Grounded in eight user-facing markdown documents — filming, metrics,
  accounts, support, coaching, plans, troubleshooting. No internal
  documentation exists in the corpus, so there is nothing implementation-level
  for retrieval to surface even if asked.
* Three independent layers enforce the privacy requirement, not one:
  1. A question *about* CricLab's own implementation (stack, database, model,
     "ignore previous instructions", etc.) is pattern-matched and refused
     before it reaches the model.
  2. The system prompt instructs it to answer only from the retrieved
     passages and never discuss internals.
  3. The generated answer is scanned against a denylist of implementation
     terms (mongodb, fastapi, ollama, mediapipe, jwt, bcrypt, react, …) and
     **discarded, not trimmed**, if one appears — falls back to an extractive
     answer built directly from the passage text.
* Retrieval blends rescaled cosine similarity with term overlap. Embeddings are
  cached by content hash in `kb_chunks`, so an edit re-embeds only the changed
  document; the whole path degrades to term-overlap-only, not an error, when
  the local embedding model is unreachable.
* Rate limited per user when signed in, per IP when not — the assistant is
  reachable by a signed-out visitor asking "how do I film this?".

## Bugs the build caught

1. **Off-topic questions got confident wrong answers.** A single similarity
   threshold admitted almost anything: general-purpose embeddings do not push
   unrelated text near zero cosine (two unrelated sentences still score
   ~0.45). "What is the airspeed of an unladen swallow?" returned a fluent
   paragraph about ball speed instead of "I don't know." Fixed by rescaling
   similarity onto the band that actually carries signal (`SEMANTIC_FLOOR`/
   `SEMANTIC_CEILING`) before thresholding, and requiring the *best* passage to
   clear a second, higher bar (`MIN_TOP_RELEVANCE`) or the whole result set is
   discarded — four weak matches are not evidence.
2. **FormData silently sent as JSON.** `authFetch` always set
   `content-type: application/json` whenever a body was present, which breaks
   a multipart upload's own boundary header. Fixed to skip the override when
   the body is a `FormData` instance. Caught by the browser upload test, not
   the API-level end-to-end suite — worth remembering that a passing backend
   test does not exercise the frontend's request construction.

## Frontend

* `src/api/support.ts`, `src/api/coaching.ts`, `src/api/assistant.ts` — new
  API clients; `src/api/auth.ts` gained `authFetchBlob` for authenticated file
  downloads (an attachment needs the bearer token, so it cannot be a plain
  `<a href>`).
* `src/pages/app/SupportPage.tsx`, `TicketPage.tsx`, `CoachingPage.tsx` — new
  workspace screens; `src/components/app/AssistantWidget.tsx` — floating
  popover mounted at the app root (outside `<Suspense>`, so it is reachable
  while a route chunk is still loading).
* Nav entries added to `Layout.tsx`, `AccountMenu.tsx`, `SiteFooter.tsx`.

## A second bug found and fixed in the same pass: workspace dark mode

Not part of this task's brief, but found while verifying it in the browser and
worth recording because it explains a whole class of future "light mode /
dark mode looks wrong" reports.

`Layout.tsx` (the signed-in app shell) was written with **`dark:` variants on
top of light-first tokens** (`text-ink dark:text-chalk`, `bg-mist
dark:bg-night`, …), copying the marketing site's convention. But the workspace
is remapped the other way: `.app-shell` in `index.css` redefines `--color-
chalk`/`--color-charcoal`/etc. in `html.light`, so workspace markup is meant to
be written **once**, dark-first, and light mode falls out of the remap. A
`dark:` pair on that markup is applied *on top of* an already-remapped token
and inverts — `bg-chalk` painted a dark surface, `text-ink` put dark text on
it. Rewrote `Layout.tsx` entirely in dark-first tokens with no `dark:`
variants, and documented the rule at the top of both the component and the
`.app-shell` block in `index.css` so it isn't reintroduced.

Two more instances of the same root cause, in shared CSS rather than markup:
`.glass-light`, `.bg-chalk-gradient` and `.bg-chalk-warm` are plain CSS
classes (not Tailwind utilities), so a `dark:` counterpart placed beside them
in markup does not win by specificity — source order in the stylesheet
decides, and these plain classes are emitted after Tailwind's utilities. Fixed
by giving each an explicit `html.dark` counterpart in `index.css` next to its
light definition, rather than relying on `dark:` at the call site. Added an
`.on-night` escape hatch for the handful of surfaces that are deliberately dark
in both themes (a header over a floodlit photo, a video frame).

## Not built

Nothing outstanding from the original request. Cloudinary credentials the user
referenced were never actually pasted into chat — Cloudinary support already
exists in `app/services/cloudinary_service.py` and degrades to local disk when
unconfigured.
