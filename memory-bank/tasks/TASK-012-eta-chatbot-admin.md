# TASK-012 — Analysis ETA, chatbot overhaul, admin panel

**Feature:** FEAT-026 (progress ETA), FEAT-027 (chatbot v2), FEAT-028 (admin panel)
**Status:** Done. ETA, chatbot, admin backend, and admin frontend (all six pages + routing + nav entry) are in place. Coach profile CRUD UI and booking calendar view remain deferred — see the last section.
**Priority:** P0
**Date:** 27 Aug 2026

## 1. Video analysis progress — estimated time remaining

`app/pipeline/eta.py` — `estimate_eta_seconds(*, collection, pipeline, job)`.

Blends two signals rather than trusting either alone:

* **This job's own pace** (`elapsed / (progress/100)`) — near-useless at low
  progress (the queued/extract stage is quick relative to pose estimation, so
  a naive extrapolation wildly overshoots early on).
* **A historical average** of the last 20 completed jobs for the same
  pipeline (`jobs` or `balltrack_jobs`), read fresh each call — this is real
  processing information (video length, machine load), not a guess.

The blend weight shifts from "trust the average" to "trust this job's own
pace" as progress advances (`live_weight = min(1, progress/60)`). Falls back
to a static default (95s action / 70s ball-flight) when there is no
completed-job history yet (a cold cache should not show nothing).

Wired into `GET /jobs/{id}` (action) and `GET /balltrack/jobs/{id}`
(ball-flight) as `eta_seconds` (nullable int). Frontend: `src/lib/eta.ts`
(`formatEta`), rendered in both `ProcessingPage.tsx` and
`BallFlightProcessingPage.tsx`.

## 2. Chatbot overhaul

### Intent routing — `app/assistant/intent.py`

`classify(message)` returns `greeting | thanks | farewell | query` via regex,
conservative on purpose: only a short (<24 char), whole-message match counts
as casual — "hi, why wasn't my ball tracked?" falls through to `query` because
pattern-matching there would answer the greeting and ignore the real question.
`direct_reply(intent)` returns a canned response with no RAG, no LLM call.
Wired into both `chat.answer()` and `chat.answer_stream()` as the very first
check.

### Streaming — `generate_text_stream()` in `app/agent/ollama_agent.py`,
`chat.answer_stream()` in `app/assistant/chat.py`, `POST /assistant/ask/stream`

Ollama's `/api/generate` with `stream: true` yields one JSON object per line;
`generate_text_stream` re-yields just the `response` text deltas. `answer_stream`
buffers ~48 chars before flushing (`STREAM_FLUSH_CHARS`) — small enough to feel
live, large enough for the leak-scan and link-repair regexes (below) to see
enough context per check. The stream protocol is newline-delimited JSON, not
SSE — one line, one event (`meta` → `delta`* → `done`, or `redacted` replacing
the rest of the turn if a leak check fires mid-stream). `/assistant/ask`
(non-streaming) is unchanged and still used as the fallback path.

**Four real bugs found only by testing the actual streamed output, not by
inspection** — the model is a local 4B (`gemma3:4b`), and none of these would
show up in a single non-streaming test:

1. **A bracket/paren pair split across two flush chunks.** `[Open Coaching]`
   landed in one flush, `(/app/coaching)` in the next — neither half matches a
   markdown-link regex alone, so it rendered as broken raw syntax. Fixed by
   `_split_before_open_bracket()`: never flush text that ends mid-`[...]` or
   mid-`(...)`; hold the tail back for the next chunk.
2. **The model appended the same link to every answer regardless of
   relevance** ("Open Coaching" on a question about a forgotten password) —
   because the prompt's *own example* used a concrete real link
   (`[Open Coaching](/app/coaching)`), and the model latched onto it verbatim.
   Fixed by rewriting the prompt to use a placeholder pattern instead of a
   real example, and adding an explicit per-topic hint to each row of the
   `PAGE_LINKS` table (`"Account Settings" — for questions about password,
   profile, email, signed-in devices`) so the model has a concrete "why this
   page" signal instead of one memorised example.
3. **The model wrote the raw path as the link's visible text** —
   `[Open /app/support](/app/support)` and `[/app/settings](/app/settings)` —
   which is exactly the raw-URL-as-link-text the assistant is meant to avoid.
   Fixed by `_humanize_link_text()`: if the href appears literally inside the
   label, replace the label with the proper `"Open <Page Name>"` text from
   `_HREF_TO_LABEL`.
4. **The model duplicated a just-written href immediately after finishing the
   link** — `[Open Account Settings](/app/settings)(/app/settings)` — a
   token-repetition quirk, not a formatting choice. `_collapse_duplicate_href()`
   catches this within one flush; `_strip_leading_duplicate()` +
   `_last_link_href()` catch the same thing when the duplicate lands in the
   *next* flush instead (the common case in practice, since the model tends
   to finish a chunk right at the closing `)`).

None of these four are hypothetical — each was caught by actually curling
`/assistant/ask/stream` and reading the raw NDJSON, then fixed and re-tested
until three consecutive runs came back clean. **Verification note:** the
project's Browser-pane (CDP-driven) intermittently failed to deliver streamed
fetch bodies to page JS during manual testing, while `curl -N` against the
same endpoint (through the Vite proxy and directly) worked every time — this
is a tooling artifact of automated browser network capture, not a product bug;
don't trust an apparent stream stall observed only through that pane without
also checking via `curl -N`. A client-side per-read timeout
(`READ_TIMEOUT_MS` in `src/api/assistant.ts`) falls back to the non-streaming
endpoint regardless, as real defensive engineering for a genuinely slow or
dropped connection.

### Grounded navigation links — `PAGE_LINKS` in `app/assistant/chat.py`

A `list[tuple[label, path, topic_hint]]`, not a bare dict — the topic hint is
what fixed bug #2 above. `_ALLOWED_HREFS` is the allow-list `_sanitize_links()`
checks every generated link against; anything not in it is stripped (link text
kept, href discarded) rather than shown. This is the same "grounded, not
guessed" rule the knowledge-base retrieval already applied to prose, now
applied to links too.

### Markdown formatting

System prompt now requires short paragraphs, numbered steps, `**bold**` for
emphasis, a heading only when genuinely long enough to need one, and forbids
code formatting unless the content is actually code/a path/a field name.
Rendered by a new hand-rolled renderer, not a library — see frontend section.

## 3. A foundational gap closed: analyses had no owner

Found while building the admin panel's user-analysis-usage requirement — it
could not be built honestly without this fix, so it went first.

* `src/api/client.ts` (frontend) sent every video/job/delivery request with
  **no Authorization header at all** — the workspace route was gated by
  `RequireAuth` on the frontend, but the API calls themselves carried no
  identity. Fixed: `request()` now goes through `authFetch`, gaining renewal
  and 401 retry for free.
* `POST /videos` and `POST /balltrack/sessions` now require `VerifiedUser` and
  store `user_id` on the video/session doc and the job doc; `user_id` is
  threaded through to `run_analysis_job`/`run_balltrack_job` so the resulting
  delivery carries it too (ball-flight deliveries carry `session_id` instead
  and resolve ownership through their parent session, rather than duplicating
  `user_id` onto every delivery row).
* `GET /jobs/{id}`, `GET /deliveries`, `GET /deliveries/{id}` and their
  ball-flight equivalents (`GET /balltrack/sessions`,
  `GET /balltrack/sessions/{id}`, `GET /balltrack/jobs/{id}`,
  `GET /balltrack/deliveries/{id}`) now require `CurrentUser` and scope every
  query to `user_id == caller`. A mismatch returns 404, matching the existing
  house rule (TASK-010/011): never a 403 that would confirm a resource with
  that id exists at all.
* **Known consequence, not a bug**: every video/delivery/session created
  *before* this change has no `user_id` and is now invisible to its original
  uploader (and visible to nobody except the admin analyses list, which is
  unscoped by design). This is pre-launch test data; nothing was migrated.
* **Still open, out of scope for this pass**: `GET /artifacts/{job_id}/{file}`
  and `GET /media/videos/{file}` remain unauthenticated — they're referenced
  directly from `<img>`/`<video>` tags, which cannot carry a bearer header.
  Access control today is the unguessable random id in the URL, not a real
  check. A proper fix would need signed, expiring URLs; flagged, not built.

## 4. Admin panel — backend (complete)

`app/services/admin_service.py` (all cross-account queries) +
`app/api/admin.py` (every route behind `AdminUser`, confirmed 403 for a
signed-in non-admin and 401 for anonymous — see verification below).

* `GET /admin/dashboard?range=today|7d|30d|90d|custom&date_from=&date_to=` —
  user counts (total/new/active-24h/inactive/disabled), video counts
  (total/today/week/month/range, split by pipeline, plus failed-in-range),
  coaching counts (total/upcoming/completed/cancelled), ticket counts
  (open/resolved), plus `signups_trend`/`analyses_trend` daily series for the
  dashboard's line charts. "Active" = signed in within 24h via the new
  `users.last_login_at` field (stamped on every successful `/auth/login`,
  indexed `last_login_desc`).
* `GET /admin/users` — paginated, search by name/email regex, filter by
  active/disabled/unverified, each row carrying analysis/booking/ticket counts
  computed via one small `$group` aggregate per activity type across just
  that page's ids (cheap at admin list-page scale; deliberately not a
  `$lookup` across three differently-shaped collections).
* `GET /admin/users/{id}` — full detail + last 10 analyses/bookings/tickets.
* `PATCH /admin/users/{id}/status` — `{disabled: bool}`; refuses to let an
  admin disable their own account (400); logs `admin_user_disabled` /
  `admin_user_enabled` to the existing `security_events` audit trail via
  `auth_repo.log_security_event`.
* `GET /admin/analyses` — `jobs` + `balltrack_jobs` combined into one list,
  tagged `pipeline: "action" | "ball_flight"`, filterable by pipeline/status,
  searchable by player name, each row resolved against its owning user (name
  + email) via a small batched lookup, `null` when the job predates this
  task's ownership fix (see section 3).
* `GET /admin/bookings` — every user's coaching bookings, filterable by
  upcoming/completed/cancelled/pending/confirmed.
* `GET /admin/tickets/metrics` — counts per `support_service.STATUSES`, plus
  average resolution time in hours over the last 100 resolved tickets.
* `POST /admin/notifications/broadcast` — `{title, body, user_ids: string[] |
  null}`; `null` = every user. Reuses `notification_service.notify_many`.
  Records itself to a new `admin_broadcasts` collection (indexed
  `created_desc`) so `GET /admin/notifications/history` has something to
  show; logs `admin_broadcast_sent` to the audit trail.

**Verified**: signed-in admin can reach every route above with real data
(3 users, 78 analyses, 11 bookings, 8 tickets, one broadcast sent to 3
users); a signed-in non-admin gets 403 on `/admin/dashboard`; an anonymous
caller gets 401. Disable → re-enable round-tripped correctly; self-disable
correctly refused with 400.

## 5. Admin panel — frontend

### Built

* `src/api/admin.ts` — typed client for every endpoint above.
* `src/components/admin/AdminLayout.tsx` — its own sidebar+topbar shell, not
  shared with the user workspace `Layout.tsx` (the spec asked for a
  completely separate experience). Written dark-first, no `dark:` variants —
  same convention as the workspace shell (see "Workspace theming" in the
  frontend memory bank); do not copy the marketing site's light-first
  convention onto anything under `src/pages/admin/`.
* `src/components/admin/charts.tsx` — `TrendLine` (line + area fill),
  `Donut`, `BarList`. Hand-rolled SVG/div, no charting library — three shapes
  didn't justify a dependency.
* `src/pages/admin/AdminDashboardPage.tsx` — full metrics + both trend charts
  + donut + bar list, date-range picker including custom from/to.
* `src/pages/admin/AdminUsersPage.tsx` — search, status filter, pagination,
  click-through detail panel with disable/re-enable (behind a confirm
  dialog for disabling — see `ConfirmDialog` in the frontend memory bank).
* `src/pages/admin/AdminAnalysesPage.tsx` — pipeline/status filters, search
  by player name, pagination, shows the attributed user or "Unattributed".

All three typecheck clean (`npx tsc --noEmit`) as of this task's pause point.

### Frontend completed (this pass)

1. **`AdminCoachingPage.tsx`** — paginated list over `GET /admin/bookings` with
   upcoming/completed/cancelled filters; read-only roster from
   `GET /coaching/admin/coaches`. Calendar view and coach profile CRUD remain
   deferred (see below).
2. **`AdminTicketsPage.tsx`** — metric cards from `GET /admin/tickets/metrics`;
   queue/reply/resolve reuse the existing staff APIs (`/support/queue*`) so
   there is one queue, not a duplicate that would drift.
3. **`AdminNotificationsPage.tsx`** — compose via `admin.broadcast()`, history
   via `admin.broadcastHistory()`. Audience is everyone or a picked set of
   accounts; sending is behind the same confirm dialog used for disabling a
   user.
4. **Routing** — `/admin`, `/admin/users`, `/admin/analyses`, `/admin/coaching`,
   `/admin/tickets`, `/admin/notifications` wrapped in `RequireAdmin` +
   `AdminLayout`, lazy-loaded like every other route.
5. **Entry point** — `AccountMenu.tsx` shows "Admin panel" only when
   `user.role === 'admin'`. The floating assistant is hidden on `/admin/*`.

## Not built (deferred, not forgotten)

* Coach profile CRUD admin UI (backend exists — TASK-011).
* Calendar view for bookings (spec asks for "calendar and list views";
  list view is what's built).
* Settings/"Admin profile" section from the original spec — lowest priority.
* Signed/expiring artifact URLs (see section 3's "still open" note).
