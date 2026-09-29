# RideMate QA Pass 2 — Final Report & Production Gate

Scope: the 42-point internal-security harness pass 2 (P0–P2 hardening of
auth/session, payments + ledger, bookings, realtime, uploads, admin/RBAC,
visibility, rate limiting, config gate, deployment). API version `2.1.0`.

Verdict: **PRODUCTION CANDIDATE** on P0/P1 criteria below; the single hard
blocker is infrastructure credentials that only the owner can rotate.

Statuses: **PASS** = verified by automated test / run; **PARTIAL** = implemented
+ unit-verified, live provider not exercised in this environment; **NOT
TESTABLE** = requires infrastructure absent here.

---

## 1. Executive summary

Pass 1 shipped 35 hardening steps (`HARDENING_REPORT.md`, 41 tests). Pass 2
re-audited the same surfaces and close the known gaps, and the audit found and
fixed **five more production-grade bugs** plus added missing product-critical
paths:

| Bug / gap | Impact if shipped |
| --- | --- |
| Rate-limit wrapper crashed on anonymous `optional_auth` endpoints (`g.user is None`) | Every unauthenticated `/api/rides/search` 500'd |
| Realtime SSE generator ran outside app context on stream close | RuntimeError on every realtime stream teardown |
| `2dsphere` indexes targeted dead `origin.geo` fields | Geo radius search silently returned nothing |
| Booking uniqueness was permanent | Legitimate rebooking after cancel/refund impossible; duplicate ACTIVE booking race allowed |
| Payouts had no lifecycle endpoint | Driver payouts could never move past `pending` |

Delivered additionally: proportional refund ledger reversal, pending-payment
seat expiry, past-departure verify guard, vehicle-plate privacy, geographic
search with destination radius, ratings (post-trip, parties-only, once),
safety (trusted contacts / reports / SOS / blocks), admin analytics, payout
advance, frontend payment-verification wiring, Docker/Gunicorn hardening.

**Test result: 93 passed, 0 failed** (`python -m pytest backend/tests -q`).
Live smoke suite: `ALL_SMOKE_PASSED` on the rebuilt server.

---

## 2. Bugs fixed in this pass (per-bug records)

### P2-01. Rate limiting crashed unauth'd requests
- SEVERITY: High (every anonymous search/detail 500'd under the decorator).
- ROOT CAUSE: `ratelimit.rate_limit` read `g.user["_id"]` unconditionally; on
  routes with `optional_auth` there is no `g.user`.
- FIX: Wrapper now uses `g.get("user")` and swallows key errors.
- FILES: `backend/ratelimit.py`.
- TEST: `test_visibility.py` detail/search paths (previously KeyError),
  `test_security.py` XFF/429 suite. RESULT: PASS.

### P2-02. Realtime SSE stream context error
- SEVERITY: High (stream teardown RuntimeError on close).
- ROOT CAUSE: generator closed outside request context.
- FIX: Return `stream_with_context(generator())`; strict trip tracking bounds +
  sampled history + Redis fan-out; authz: driver or CONFIRMED rider only.
- FILES: `backend/blueprints/realtime.py`.
- TEST: `test_realtime.py` (7 cases). RESULT: PASS.

### P2-03. Dead geospatial indexes
- SEVERITY: High (geo search returned nothing near any co-ordinate).
- ROOT CAUSE: indexes built on `origin.geo` / `destination.geo`; data stores
  GeoJSON Points at `origin_location` / `destination_location`.
- FIX: Rebuild indexes on the real fields; search combines origin + optional
  destination radius via explicit `$and`; `sort=nearest` via `$geoNear`.
- FILES: `backend/db.py`, `backend/blueprints/rides.py`.
- TEST: `test_visibility.py::test_search_returns_distance` + geo tests.
  RESULT: PASS.

### P2-04. Booking uniqueness blocked legitimate rebooking
- SEVERITY: Medium.
- ROOT CAUSE: permanent unique `(ride_id, rider_id)` index.
- FIX: Partial-unique index over ACTIVE states only
  (`pending_payment`/`payment_failed`/`confirmed`), dropped legacy index at
  boot; lazy sweep expires stale `pending_payment` and releases seats.
- FILES: `backend/db.py`, `backend/blueprints/bookings.py`, `backend/config.py`
  (`PAYMENT_TTL_MINUTES`).
- TEST: `test_bookings.py` (13 cases: rebook after cancel, TTL release, cancel
  cascade nets-to-zero, proportional fee reversal, past-departure refund).
  RESULT: PASS.

### P2-05. Proportional refunds distorted the ledger
- SEVERITY: Medium.
- ROOT CAUSE: partial refunds posted driver/passenger lines for the full
  gross while only a portion was returned; platform fee was not reversed.
- FIX: `record_refund_ledger` reads back the recorded
  `PLATFORM_FEE`/`DRIVER_PAYABLE` and reverses them in proportion; adds
  `PLATFORM_FEE_REVERSAL`/`DRIVER_PAYABLE_REVERSAL`/`PASSENGER_REFUND`.
- FILES: `backend/ledger.py`.
- TEST: `test_bookings.py::test_partial_refund_reverses_fee_proportionally`
  (gross 50, min fee 5 → fee_rev 2.5, net_rev −22.5, rider −25; no drift),
  driver-cancel cascade nets every account to 0. RESULT: PASS.

### P2-06. Verify-after-departure could board a sailed ride
- SEVERITY: High (financial exposure + trust).
- FIX: `verify()` past-departure guard auto-refunds and returns 409
  `ride_departed`; ride-unavailable guard also refunds.
- FILES: `backend/blueprints/bookings.py`.
- TEST: `test_bookings.py` verify-after-departure. RESULT: PASS.

### P2-07. Registration plate leaked to strangers
- SEVERITY: Medium (PII privacy).
- FIX: `clean_ride_public(include_plate=)` strips `vehicle.number` unless the
  caller is the owner or a CONFIRMED rider; search/list/detail all use it.
- FILES: `backend/blueprints/rides.py`.
- TEST: `test_visibility.py` plate matrix. RESULT: PASS.

### P2-08. Payouts could never advance
- SEVERITY: Medium.
- FIX: `PATCH /api/admin/payouts/<id>` idempotent lifecycle
  (`pending→processing→paid|failed`) with `paid_at`, audit, and RBAC gating.
- FILES: `backend/blueprints/admin.py`.
- TEST: `test_admin.py` (create/advance/idempotent/invalid/404/RBAC).
  RESULT: PASS.

---

## 3. New capability added (with tests)

- **Ratings** `POST /api/ratings`: post-departure only, parties-only, no
  self/duplicate (partial-unique index), aggregate recompute. `test_ratings.py`
  (7). PASS.
- **Safety** `/api/safety/*`: trusted contacts (≤5, validated), moderation
  reports (→ admin_flag), SOS (ride flag + audit), blocks/unblocks (unique).
  `test_safety.py` (6). PASS.
- **Analytics** `GET /api/admin/analytics`: DAU/WAU/MAU, ride pipeline,
  seat fill-rate, GMV, payouts. `test_admin.py`. PASS.
- **Frontend** `verifyBooking` wired into checkout (demo orders previously
  stayed `pending_payment` forever); transparent price breakdown
  (seats × fare = you pay now). `frontend/js/api.js`, `discover.js`. PASS
  (static, no live browser).

## 4. Rate limiting applied end-to-end

`auth` (register/login/google/verify-email ×2), `strict` (booking create/
verify/cancel, ride create/delete, payout create, refresh, document upload,
realtime POST, notify stream), `default` (search/detail/list/vehicle-doc GET,
avatar, notify, admin, ratings, safety, profile stats). `_client_ip()` trusts
only `remote_addr`. `test_security.py` XFF-spoof → 429 (isolated clients) and
per-endpoint bucket isolation. PASS.

## 5. Config / production gate

`create_app` calls `validate_runtime` for `production`/`staging`: rejects
placeholder secrets, wildcard CORS, insecure cookies, `demo` payments,
missing razorpay webhook secret/ATLAS creds, expired secrets. `test_config.py`
(13 parametrized rejects). PASS.

## 6. Database

- `origin_location`/`destination_location` `2dsphere`.
- Booking partial-unique ACTIVE index (`db._ACTIVE_BOOKING_STATES` keeps in
  sync with `bookings._SEAT_BOOKING_DB`).
- `locations` TTL (`LOCATION_HISTORY_TTL_HOURS=168`).
- `ratings` partial-unique; `blocks` unique; `reports` lookup indexes.
- Indexes (re)created at boot; restart both `ridemate_test` and live
  `ridemate` to apply. PASS.

## 7. API surface

New routes: `POST/GET /api/ratings…`, `/api/safety/*`, `PATCH
/api/admin/payouts/<id>`, `GET /api/admin/analytics`. All mutations carry
`X-Request-Id` (sanitized), `no-store`, security headers; 429 → `Retry-After`.

## 8. Security

- Spoofed `X-Forwarded-For` cannot rotate rate-limit keys (`TRUSTED_PROXY_COUNT=0`
  default).
- Plate privacy enforced server-side, owner + confirmed rider only.
- Admin RBAC (403 forbidden) and payouts ledger balance enforced.
- Webhook HMAC-validated, unknown orders ignored, duplicate settle idempotent.
- Rating/report/SOS/block self-targeting and unauthorized parties rejected.

## 9. Testing

`93 passed in ~3 min` across `test_config, test_security, test_visibility,
test_bookings, test_payments, test_realtime, test_auth, test_uploads,
test_timezone, test_ratings, test_safety, test_admin`. Live smoke:
`ALL_SMOKE_PASSED` (auth/refresh rotation/search/demo pay/verify/cancel/
refunds/uploads/IDOR/notifications/health).

## 10. Deployment

- `gunicorn.conf.py` (repo root) wired into `Dockerfile` CMD.
- Dockerfile: non-root user + HEALTHCHECK on `/api/health`.
- `.env.example` synced with all pass-2 variables (`PAYMENT_TTL_MINUTES`,
  `LOCATION_HISTORY_TTL_HOURS`, `TRUSTED_PROXY_COUNT`, `LOG_LEVEL`,
  `REQUEST_ID_HEADER`, `CORS_ALLOW_CREDENTIALS`, `PAYMENT_WEBHOOK_PATH`).
- Repo still has **zero commits** — commit before deploy.

## 11. Residual risks (owner actions)

- **Rotation required:** `backend/.env` contains a live Atlas URI (unreachable
  here; local fallback in use) — **rotate the credential** before release.
- Razorpay / Google OAuth / S3 / Redis: integration code + unit tests only;
  live gateways never exercised here (PARTIAL).
- Frontend mobile responsive + E2E unverified in-browser (static review only).
- `payments.py::_razorpay_client` reads `Config` directly; `PRIVATE_KEY`
  handling is standard.

## 12. Gate

| Criterion | Status |
| --- | --- |
| All P0/P1 fixed and tested | PASS |
| Full suite green (93) | PASS |
| Live smoke green | PASS |
| No placeholder secrets reachable in production boot | PASS |
| Owner actions (credential rotation, live-provider checks) | PENDING |

**Verdict: PRODUCTION CANDIDATE — gated on credential rotation and a clean
live-provider dry run.**