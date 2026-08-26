# TASK-010 — Accounts platform: auth, email, notifications

**Feature:** FEAT-020 (auth), FEAT-021 (email), FEAT-022 (notifications)
**Status:** Done (auth + email + notifications). Support / coaching / assistant not started.
**Priority:** P0
**Date:** 26 Aug 2026

## What exists now

CricLab had no accounts at all — every endpoint was open and every delivery was
anonymous. This adds the account layer the rest of the platform will hang off.

### Security core — `app/core/security.py`

* Passwords: bcrypt, cost 12, input bounded to 72 bytes (bcrypt truncates
  silently past that, which would make two long passwords equivalent).
* `verify_password` runs a bcrypt round **even when there is no stored hash**, so
  "no such account" is not measurably faster than "wrong password".
* Access tokens are short-lived JWTs. Refresh tokens are **not** JWTs — opaque
  random strings stored only as SHA-256 digests, so a database leak yields no
  usable sessions and any single token can be revoked.
* OTPs likewise stored as digests only.

### Data — `app/db/auth_repo.py`, `app/db/indexes.py`

Collections: `users`, `sessions`, `otps`, `notifications`, `rate_limits`,
`security_events` (plus `tickets`, `bookings`, `coaches`, `kb_chunks` indexed
ready for the modules not yet built).

* Email uniqueness is enforced by a **unique index**, not a read-then-write —
  two simultaneous signups would both pass an application-level check.
* TTL indexes expire sessions, OTPs, rate-limit counters and 180-day audit rows,
  so nothing needs a sweeper job.
* `public_user()` is an **allow-list**. A field added to the stored document is
  not exposed until it is named there.

### Auth API — `app/api/auth.py`, guards in `app/api/deps.py`

signup · verify-email · resend-otp · login · refresh · logout · logout-all ·
forgot-password · reset-password · change-password · me (GET/PATCH) · sessions.

* **No account enumeration.** Signup, sign-in and recovery answer identically
  whether or not the address exists. A duplicate signup silently mails the real
  owner instead.
* **Refresh rotation**: every renewal issues a new refresh token and invalidates
  the old one, so a stolen token is usable at most once.
* **Rate limits**, Mongo-backed so they hold across workers: login 8/15min per
  address and 40/15min per IP, signup 10/hr per IP, OTP resend 5/15min plus a
  45-second floor, reset 20/15min.
* OTPs: 10-minute TTL, 5 attempts, destroyed on use or on exhausting attempts.
  Issuing a new code deletes the old one, so "resend" cannot build a pool of
  valid codes.
* Email sending never decides request success — a signup completes with the mail
  server down.

### Email — `app/services/email_service.py`

The brief asked for Nodemailer; this backend is Python, so it is `aiosmtplib`
over STARTTLS reading the **same environment variables** (`SMTP_HOST`,
`SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`). One branded table-based
shell; twelve messages (verification, welcome, reset, password changed, new
sign-in, ticket created/updated/resolved, booking confirmed/cancelled/
rescheduled/reminder). Credentials are never logged, returned or put in an
exception; recipient addresses are masked in logs.

## Frontend

* `src/api/auth.ts` — the access token lives **in memory only**; just the refresh
  token is persisted, so an XSS cannot lift a bearer token out of storage.
  Concurrent 401s share one in-flight refresh, otherwise the first rotation
  invalidates the token the others are retrying with.
* `src/auth/AuthProvider.tsx` — three-valued status (`loading`/`authenticated`/
  `anonymous`); without the third state every reload flashes the sign-in page.
* `src/auth/guards.tsx` — `RequireAuth`, `RequireVerified`, `RequireAdmin`,
  `RedirectIfAuthenticated`. Guards hide UI; the API is the actual control.
* Auth screens use the CricLab design system (split layout, floodlit panel),
  with a real OTP input (paste fills the row, backspace retreats) and live
  password rules mirroring the server's.
* Notification centre polls only the unread **count** on a timer, pauses while
  the tab is hidden, and fetches the list only when opened.

## Three bugs the tests caught

1. **Naive-datetime token invalidation.** `password_changed_at.timestamp()` on a
   Mongo-returned naive datetime is read as *local* time — on this UTC+5 machine
   that shifted the comparison five hours and let pre-change tokens through, so
   "sign out everywhere" did not end sessions.
2. **Same-second collision.** Flooring both sides to whole seconds then fixed the
   old token but rejected the *replacement* minted in the same second. Replaced
   with an exact `pwd_at` marker carried inside the token and compared against
   the account's current value — resolution-independent.
3. **`tz_aware` client.** The driver returned naive datetimes for every
   timestamp, so the browser parsed them as local time and a just-created
   notification rendered "5h ago". Fixed globally on the Motor client.

## Verified

25/25 end-to-end checks against a live API and Mongo (`/tmp/e2e_auth.py`):
signup → digest storage → OTP issue/verify/burn → token issue → rotation → old
refresh dead → password change → old access dead, new access live → recovery →
reset code single-use → audit rows with no secrets. Rate limiting confirmed
cutting in at attempt 9 of 8. SMTP credentials confirmed authenticating against
Gmail (connect + AUTH only, no message sent). Browser: signup → verify → landed
in the workspace; guards confirmed in both directions.

## Not built

Support ticketing, coaching bookings and the RAG assistant. Their collections
and indexes exist and the guard/notification/email primitives they need are in
place, but no services, endpoints or UI.
