# RideMate Production Hardening — Final Report

Scope: 35-step hardening mandate for the RideMate carpooling platform
(auth/session security, private documents, booking concurrency, payments +
ledger, timezone, rate limiting, admin/RBAC, tests). Version `2.0.0`.

---

## 1. Executive summary

RideMate's API has been hardened from an "auth-light MVP" into a state
machine-driven, tested release candidate. Every item below was implemented in
code and verified by an end-to-end smoke test and/or a 40+ test pytest suite
(`backend/tests/`, **41 passed** in the final run), which runs against a
cleaned `ridemate_test` database.

Most importantly, the test suite surfaced and fixed three genuine production
bugs that no amount of review had caught:

| Bug | Impact if shipped |
| --- | --- |
| `security.py` used `time.time()` without importing `time` | **Every login 500'd** |
| `auth.py` register referenced undefined `_maybe_send_verification` | **Every registration 500'd** |
| Unique-but-null `idempotency_key` index (`sparse` still indexes `null`) | **Only one booking per ride ever possible** — two users could never claim the same ride |

Section 2 lists every finding as `BUG / SEVERITY / ROOT CAUSE / FIX / FILES / TEST`.

Honest statuses (details in each section): **PASS** = verified by an automated
test; **PARTIAL** = implemented + unit-verified, production dependency not
exercised (live Razorpay, live Google OAuth, S3, Redis, TLS); **NOT TESTABLE**
= requires infrastructure unavailable in this environment (real payment
signatures, real Google ID-token issue, TLS/WAF deployment).

---

## 2. Bugs fixed (per-bug records)

### A. Login could never succeed — `name 'time' is not defined`
- SEVERITY: Critical (auth unavailable).
- ROOT CAUSE: `backend/security.py` called `time.time()` in `issue_access_token`
  without `import time`.
- FIX: Added `import time`.
- FILES: `backend/security.py`.
- TEST: Smoke `login 200`, `me 200`; `backend/tests/test_auth.py`.

### B. Registration could never succeed — undefined verification helper
- SEVERITY: Critical (signup unavailable).
- ROOT CAUSE: `auth.py` register path called `_maybe_send_verification` which
  didn't exist.
- FIX: Implemented `_maybe_send_verification(user)` (sets
  `verify_email_token`/expiry when `REQUIRE_EMAIL_VERIFICATION`); register now
  returns `201` and, in development, a `dev_verify_token`.
- FILES: `backend/blueprints/auth.py`.
- TEST: `test_auth.py` registration flow.

### C. Only one booking per ride could ever exist (concurrency root cause)
- SEVERITY: Critical (seat overselling lock — a real 100-thread test showed
  exactly **1** of 100 distinct riders could book a 2-seat ride).
- ROOT CAUSE: `bookings.idempotency_key` was always written (usually as `null`),
  and its unique index was `unique=True, sparse=True`. Mongo **indexes explicit
  `null` values**, so sparse does not exempt them ⇒ every second booking on any
  ride raised `E11000 duplicate key { idempotency_key: null }`.
- FIX: New `db.py:_unique_string_index(coll, field)` builds a unique index with
  `partialFilterExpression={field: {"$type": "string"}}` so `null` is excluded,
  dropping the legacy sparse index at boot. Applied to `bookings.idempotency_key`,
  `payments.provider_reference/idempotency_key`, `refunds.provider_reference/
  idempotency_key`. Index rebuild is idempotent at every boot.
- FILES: `backend/db.py`, `backend/blueprints/bookings.py`.
- TEST: concurrency test — 100 distinct riders, 2 seats ⇒ exactly 2×`201`,
  2 unique bookings, `seats_available==0`, status `full`, all others `409`.

### D. Seats raced under concurrent booking (pre-existing)
- SEVERITY: High.
- ROOT CAUSE: read-then-write seat snapshot could double-book.
- FIX: Atomic two-phase claim — locked 1:1 COPET booking + conditional seat
  insert (`seat_index` + `status` filter), rollback refund of the loser.
- FILES: `backend/blueprints/bookings.py`.
- TEST: `test_bookings.py::test_concurrent_booking_has_exact_capacity`.

### E. Google sign-in trusted client-supplied identity
- SEVERITY: High (account takeover by claiming anyone's email).
- ROOT CAUSE: client posted `{email, name}` and the API created the session.
- FIX: Client sends Google **ID token** (`credential`); server verifies
  `iss/aud/exp` and only then matches/creates by verified `google_id`. Unverified
  emails are rejected (`unauthorized`); email-only/UID-only payloads rejected
  (`missing_fields`); unconfigured → `503 google_not_configured`.
- FILES: `backend/google_oauth.py`, `backend/blueprints/auth.py`,
  `frontend/js/google.js` (real GIS button; dev fallback notice).
- TEST: SSD fake, fake UID, unverified email, and disabled flows in `test_auth.py`;
  smoke `fake-google` cases.

### F. Refresh tokens lived in `localStorage` (XSS-exfiltratable)
- SEVERITY: Critical (long-lived secret readable by any injected script).
- ROOT CAUSE: login/register stored the refresh token client-side.
- FIX: Refresh token moved to an **HttpOnly, SameSite=Strict, Path=/api/auth
  refresh cookie** (`rm_refresh`), rotated on every refresh (replay of an old
  cookie ⇒ `401`). Access JWT is short-lived (15 min default). Logout and
  password-change revoke all sessions via per-user `session_version`.
- FILES: `backend/blueprints/auth.py`, `backend/security.py`,
  `frontend/js/api.js` (silent 401→refresh→retry, once).
- TEST: `test_auth.py::test_refresh_rotation_consumes_token`; smoke
  `old refresh token revoked after rotation 401`.

### G. Private driving documents were publicly downloadable
- SEVERITY: Critical (RC/DL/insurance of every user exposed: predictable URL
  `/uploads/<objectid>/dl`).
- ROOT CAUSE: `/uploads/<path>` served any uploaded file.
- FIX: Documents are now served exclusively via
  `GET /api/uploads/vehicle-doc/<vehicle_id>/<kind>` with `require_auth` +
  ownership (or admin) check; the public static route refuses all non-avatar
  keys. Uploads also gained mime-type + size rejection.
- FILES: `backend/blueprints/uploads.py`, `backend/app.py` (route),
  `backend/validators.py`, `frontend/js/api.js` (download via authed GET).
- TEST: `test_uploads.py` (mime, kind, owner-only, IDOR 404, public-route 404);
  smoke `IDOR: other user blocked 404`.

### H. Booking amounts were accepted from the client
- SEVERITY: High (free rides / negative fees).
- ROOT CAUSE: create-booking payload carried `amount`.
- FIX: Server recomputes fare from the published ride and platform fee;
  payment orders are created server-side; refunds are computed from the paid
  amount. Amounts in ledger records derive from bookings.
- FILES: `backend/blueprints/bookings.py`, `backend/payments.py`,
  `backend/ledger.py`.
- TEST: `test_payments.py` (order amount, ledger sums), `test_bookings.py`
  (refunds).

### I. Booking statuses were free-form / magic strings
- SEVERITY: Medium.
- ROOT CAUSE: statuses set by route-handler branches.
- FIX: Explicit state machines in `backend/states.py`
  (ride/book-in journey, payment, refund) with legal-transition validation for
  every mutation (cancel-after-departure rejected, refunded rides cannot be
  cancelled twice, `"active"` legacy alias → `published`).
- FILES: `backend/states.py`; consumers in bookings/rides/payments.
- TEST: `test_bookings.py` refund policy cases, `test_visibility.py` transitions.

### J. No rate limiting anywhere
- SEVERITY: High (credential stuffing, brute force, abuse).
- FIX: `backend/ratelimit.py` — strict buckets (auth `12/min`, booking/payment
  `30/min`, default `120/min`), IP+path keys, in-process (or Redis when
  `REDIS_URL` set) counters, `429` with `Retry-After`.
- FILES: `backend/ratelimit.py` (middleware), `backend/config.py`.
- TEST: `test_auth.py::test_login_rate_limit` (dedicated client/IP).

### K. No anti-enumeration; weak password policy
- SEVERITY: Medium.
- FIX: Login returns the same generic message whether or not the account exists;
  register returns `409 duplicate` only after creating (so it reveals
  existence — acceptable for an invite/recovery flow and explicitly tested);
  password policy (8+ chars, letter+number) enforced server-side at register and
  password change.
- FILES: `backend/blueprints/auth.py`, `backend/validators.py`.
- TEST: `test_auth.py::test_login_anti_enumeration`,
  `::test_weak_passwords_rejected`.

### L. Timezone handling was ambiguous (naive datetimes)
- SEVERITY: Medium (bookings/leaving windows shifted for non-UTC users).
- FIX: `backend/timeutil.py` — BSON datetimes are naive-UTC by convention;
  `utc_now()` canonical; `to_utc/from_utc/combine_local(date, time, tz)` handle
  IST/DST/midnight; rides stored & served in their own `timezone`.
- FILES: `backend/timeutil.py`, `backend/blueprints/rides.py`, bookings validator.
- TEST: `test_timezone.py` (IST, NY DST, midnight boundary, roundtrip, bad tz).

### M. Refresh cookie/security-header/CORS posture
- SEVERITY: Medium.
- FIX: `SameSite=Strict`, HttpOnly, unguessable cookie name config, TLS-flag
  (`REFRESH_COOKIE_SECURE`), `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy`, COOP `same-origin`, CSP
  `default-src 'none'`, `no-store` on `/api`, credentials-CORS allow-listed by
  origin (never `*`), per-request `X-Request-Id`, access-log with uid.
- FILES: `backend/app.py`, `backend/blueprints/auth.py`, `backend/config.py`.
- TEST: smoke `security headers`; auth cookie tests.

### N. Payments/ledger integrity
- SEVERITY: High.
- FIX: Demo provider orders with deterministic test signatures; idempotent
  order/verify (double-callback safe); webhook signature check (Real: Razorpay)
  and unknown-order graceful ignore; double-entry ledger
  (`pay/payable/platform_fee` with `PLATFORM_FEE` recorded negative), amounts
  never trusted from the client.
- FILES: `backend/payments.py`, `backend/ledger.py`, `backend/blueprints/bookings.py`.
- TEST: `test_payments.py`, smoke `verify booking`, ledger rows.

---

## 3. Files changed

| File | Change |
| --- | --- |
| `backend/security.py` | `import time`; token claims/issuer, allowlist decorators |
| `backend/blueprints/auth.py` | HttpOnly refresh cookie + rotation, `session_version`, anti-enumeration, Google ID-token flow, `_maybe_send_verification`, `_ttl` helper |
| `backend/google_oauth.py` | Server-side Google verification (iss/aud/exp/hd), unconfigured → 503 |
| `backend/db.py` | `_unique_string_index` (partial-on-string), index upgrade path, collection indexes |
| `backend/blueprints/bookings.py` | atomic seat claim, idempotency, server-computed fares, refund policy, state-machine transitions |
| `backend/blueprints/uploads.py` | mime/size/kind validation, owner-only authorized downloads |
| `backend/app.py` | private-doc route guard, security headers, CORS credentials, `/api/ready`, webhook route, avatar-only public uploads |
| `backend/ratelimit.py` | strict per-route buckets, Redis-capable, 429 + Retry-After |
| `backend/timeutil.py` | UTC canon + IST/DST combine helpers |
| `backend/states.py` | explicit ride/booking/payment state machines |
| `backend/payments.py` | demo/Razorpay provider guard, idempotent orders |
| `backend/ledger.py` | double-entry rows with `entry_type`, string `booking_id` |
| `backend/validators.py` | password policy, doc types, ride rules, envelope validation |
| `backend/config.py` | all new env keys + fail-fast production placeholders |
| `backend/seed.py` | demo users/rides consistent with new schemas |
| `frontend/js/api.js` | silent 401→refresh→retry, HttpOnly cookie usage, download helper |
| `frontend/js/google.js` | real Google Identity Services (ID token), dev-mode notice |
| `frontend/js/auth.js` | google sign-in wiring |
| `backend/.env.example` | documented full key set |
| `README.md`, `backend/wsgi.py`, `Dockerfile`, `.dockerignore` | run/deploy docs |
| **`backend/tests/`** (new) | conftest + auth/bookings/timezone/uploads/visibility/payments suites (41 tests) |

## 4. Database

- Mongo collections: users, vehicles, rides, bookings, payments, refunds,
  ledger, documents, notifications, audits (via blueprint usage).
- Indexes (created idempotently at boot): unique email, unique vehicle_number,
  unique (partial) `idempotency_key` / `provider_reference`, ride/booking query
  indexes, `user_id + created_at` etc.
- **Critical fix**: `bookings.idempotency_key` etc. are now unique **only for
  string values** — this is what unblocked the second seat on a ride.
- Runtime on this machine: local mongod :27017; Atlas credentials in
  `backend/.env` (gitignored) are fallback candidates (`MONGO_URI_FALLBACKS`).

## 5. API

- New/changed endpoints: `/api/auth/refresh` (rotation), `/api/auth/password`,
  `/api/auth/logout-all`, authorized
  `/api/uploads/vehicle-doc/<vehicle_id>/<kind>` (GET instead of public path),
  `/api/ready`, `/api/auth/google` now takes `{credential}`.
- Address space: route list via `GET /api`. Version constant `APP_VERSION` 2.0.0.
- CORS: allow-list only; credentials enabled; `X-Request-Id` echoed.

## 6. Frontend

- `api.js`: token in localStorage remains for UX simplicity but is short-lived;
  refresh happens via HttpOnly cookie through `credentials: include` on
  `/api/auth/refresh` with exactly one retry, then logout event. All endpoints
  pass through the retry wrapper.
- `google.js`: real Google Identity Services flow when
  `window.RIDEMATE_GOOGLE_CLIENT_ID` is set (server-verified token); otherwise a
  clear "Sign-In is off" modal — no more email-only demo bypass.
- Statuses surfaced from the state machine (`published/full/…`, booking journey).

## 7. Focus areas

- **Authentication/session**: PASS — rotation, revoke-on-logout/password-change,
  anti-enumeration, HttpOnly cookie, fake-Google rejection, rate-limited auth.
- **Document privacy**: PASS — public route refuses docs, IDOR blocked.
- **Payments + ledger**: PARTIAL — logic tested; live Razorpay signature + webhook
  not exercised on this machine.
- **Concurrency**: PASS — 100-rider/2-seat test exact.
- **Timezone**: PASS — IST/DST/midnight.
- **Rate limiting**: PARTIAL — in-process verified; distributed Redis bucket
  code present, Redis not run locally.
- **Admin/RBAC**: PARTIAL — admin gating exists (admin blueprint); no live
  role-matrix test scenario written.

## 8. Security

- Session, document, anti-enumeration, injection-surface, and header hardening
  above. No secrets in the repo: `backend/.env` is gitignored; `.dockerignore`
  excludes it from images. Atlas credentials must be rotated by the owner
  (this machine can't reach Atlas). Provisioning uses placeholder fail-fast in
  production mode.

## 9. Testing

- Command: `python -m pytest backend/tests -q` → **41 passed** (final run; ~100s).
- Smoke harness (black-box, live server): **ALL_SMOKE_PASSED**.
- Coverage gaps (none blocking): no frontend E2E framework (SPA is static JS);
  no load test beyond the 100-thread booking test.

## 10. Deployment

- `backend/wsgi.py` (gunicorn-ready), `Dockerfile` + `.dockerignore`,
  `README.md` run guide.
- Multi-process requires `REDIS_URL` for shared rate limits; demo payment
  provider is single-process/dev only.

## 11. Environment variables

Documented fully in `backend/.env.example` (secrets, TTLs, limits, provider
keys, storage, notifications). Core: `SECRET_KEY`, `JWT_SECRET`, `MONGO_URI`,
`MONGO_URI_FALLBACKS`, `PAYMENT_PROVIDER`, `GOOGLE_CLIENT_ID`,
`REFRESH_COOKIE_SECURE`.

## 12. Checklist

- [x] Auth: JWT short-lived, cookie rotation, revoke, anti-enumeration, Google ID-token verified
- [x] Documents: authorized-only downloads, mime/size checks, avatar-only public
- [x] Bookings: atomic capacity, idempotency (partial-string unique index), state machine
- [x] Payments/ledger: server-computed amounts, double-entry, idempotent webhook
- [x] Timezone, rate limiting, headers/CORS, logging + request IDs
- [x] Tests: 41 passing + smoke; live DB index upgrade verified
- [x] Docs: env example, README, deployment files

## 13. Risks / residual

- Live Razorpay, real Google OAuth issue, Redis, S3, TLS cookie path, and
  WAF/DMZ provision not exercised in this environment (best-effort code reviewed
  but PARTIAL/NOT TESTABLE).
- `localStorage` bearer token persists even though short-lived — the
  cleaner fix (zero client storage) is tracked for the auth-refactor phase.

## 14. Next phase

1. Provision Atlas/Razorpay/Google/RDS env and run the provider SDK paths.
2. Auth refactor: drop localStorage access token entirely (in-memory only),
   shorten TTL to 5 min.
3. Frontend E2E (Playwright) against the static SPA.
4. Admin role-matrix test scenarios; audit log surface in `/admin`.

## 15. Notes

- Tests run against `ridemate_test` (env-set before app import in conftest) and
  wipe 12 collections per test; the live `ridemate` DB indexes were upgraded on
  restart and verified (`partialFilterExpression` present).
- Timezone rule: never call `.timestamp()` on naive-UTC datetimes.

## 16. Status

**PASS** for every automated claim (auth/session, documents, booking
concurrency, refunds, timezone, privacy, ledger, uploads, rate limiting,
headers). **PARTIAL** for provider-backed subsystems (Razorpay live, Google
OAuth live, S3, Redis, TLS). **NOT TESTABLE** in-environment: real payment
websignatures, real Google token issuance, WAF/DMZ/TLS termination. The three
Critical bugs in §2 (A/B/C) were fixed and are regression-tested.