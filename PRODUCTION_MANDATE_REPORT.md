# RideMate - Production Mandate: 48-Part Final Report

Companion to `HARDENING_REPORT.md`. This report covers the 48-part production
mandate: the financial path (commission, checkout, settlement, payouts), the
trip lifecycle, driver KYC, and the privacy/concurrency controls around them.

**Verdict: PRODUCTION CANDIDATE - NOT PRODUCTION READY.**

Every item implemented in this report is verified by an automated test that
passes in the full suite (**217 passed / 217 collected**, no skips, no xfails).
The remaining blockers are environmental, not code: a real Razorpay TEST
checkout has never been executed, no compliant KYC provider is connected, no
browser E2E run exists, and no release artifact has been produced. Those are
recorded as `NOT EXECUTED` in section 6 rather than being quietly marked done.

---

## 1. Evidence

| Check | Command | Result |
|---|---|---|
| Full backend suite | `python -m pytest backend/tests -q -p no:logging` | **217 passed** in 4:16 |
| Commission invariants | `python -m pytest backend/tests/test_commission.py -q` | 11 passed |
| Gateway checkout | `python -m pytest backend/tests/test_checkout.py -q` | 9 passed |
| Trip lifecycle + no-show | `python -m pytest backend/tests/test_trip_lifecycle.py -q` | 17 passed |
| Payout settlement | `python -m pytest backend/tests/test_payout_settlement.py -q` | 27 passed |
| Order concurrency | `python -m pytest backend/tests/test_order_concurrency.py -q` | 4 passed |
| Driver KYC + privacy | `python -m pytest backend/tests/test_kyc.py -q` | 20 passed |
| Dependency integrity | `pip check` | No broken requirements |
| Frontend syntax | `node --check` on all 13 files in `frontend/js` | clean |
| Secret scan | regex over `.env.example`, `frontend/index.html`, this report | 0 real matches |

Environment: Windows, Python 3.12, Flask 3.0.3, pymongo 4.7.2, razorpay 1.4.1,
pytest 9.1.1, MongoDB 7.0.14 (portable, local, database `ridemate_test`).

Per-file test counts:

```
test_admin.py 5            test_payout_settlement.py 27
test_auth.py 10            test_production_hardening.py 32
test_bookings.py 13        test_ratings.py 7
test_checkout.py 9         test_realtime.py 6
test_commission.py 11      test_safety.py 4
test_config.py 19          test_security.py 6
test_kyc.py 20             test_timezone.py 6
test_order_concurrency.py 4  test_trip_lifecycle.py 17
test_payments.py 7         test_uploads.py 7
                            test_visibility.py 7
```

## 2. The financial path

### Commission is immutable and exactly 30%

`PLATFORM_FEE_PERCENT=30` with `MIN_PLATFORM_FEE=0`. A non-zero floor would make
low-fare bookings deviate from exact 30% and break reconciliation, so the zero
is a load-bearing invariant, not a default.

The rate **and** the resulting rupee amounts are computed once, at order
creation, and frozen onto the payment document (`platform_fee`, `driver_net`,
`commission_rate_percent`, `commission_frozen_at`). Settlement, refund reversal,
the driver earnings statement, and admin reconciliation all read those stored
values. A later config change therefore cannot retroactively rewrite a completed
transaction - verified by `test_commission.py`, which re-quotes at a different
rate and asserts the historical split is unchanged.

### Checkout reaches a real gateway

`GET /api/bookings` responses and the payment flow expose a `checkout` object
carrying the publishable key id, order id, server-computed gross, and the
commission breakdown. The key **secret** is never sent. Gateway configuration is
validated *before* any external call (`assert_checkout_ready`), so a
half-configured deployment cannot strand a real order it cannot hand to a
browser.

`frontend/js/checkout.js` loads the gateway script, opens checkout, and verifies
the result server-side. The UI shows the honest split; the "demo checkout"
wording is gone.

### Order creation is race-free (real defect, fixed)

The previous shape called the gateway and inserted the payment row afterwards,
and `booking_id` was a **non-unique** index. Two concurrent requests - a
double-tap, a retry, two tabs - both reached Razorpay and both inserted, leaving
a live orphan order at the gateway that nobody would ever pay or cancel.

Fixed by:
- a **unique** index on `payments.booking_id` (with a dedupe pass that keeps the
  most advanced row for any pre-existing duplicates), and
- claiming the booking in the database **before** the gateway call, with a
  60-second stale-claim takeover so a crash mid-call cannot block a booking
  forever, and explicit release on gateway failure so retries are immediate.

`test_order_concurrency.py` drives 8 threads at one booking and asserts the
gateway was called exactly once.

### Payouts: only the provider can declare money moved

`DRIVER_PAYABLE` is an accounting entry, not a transfer. Moving it out of the
platform is a separate, provider-confirmed act.

There is deliberately **no** API path that sets a payout to `paid` for the
`razorpayx` provider - `PATCH /api/admin/payouts/{id}` with `status: paid`
returns `409 payout_confirmation_required`. `paid` is reachable only from a
correctly signed provider webhook that confirms a payout already in
`processing`. This is what stops a stale, replayed, or forged callback from
marketing phantom money as sent.

The `manual` provider (out-of-band bank transfer) has one narrow human
equivalent, `POST /api/admin/payouts/{id}/confirm`, which requires a bank
reference (UTR), refuses a reference already used by another payout, refuses
non-manual providers, and audits both acceptances and refusals.

**Accounting invariants** (each has a test):

- A `DRIVER_PAYOUT` debit is written when the payout is created, so a
  reservation cannot be spent twice.
- `outstanding = earned - reserved`, where `reserved` counts every payout that
  still holds a debit: `pending`, `processing`, `paid` **and `failed`**. Using
  only `paid` (the original bug) would have let a merely `processing` payout look
  available, permitting a second payout for the same rupees.
- A `failed` payout still holds its money. It is released only by an explicit
  `void`, which posts a reversing `DRIVER_PAYOUT_REVERSAL` entry - the original
  debit is never rewritten, so the books stay auditable.
- A `paid` payout is terminal: it can never be voided, failed, or re-sent.
- The cap is enforced **inside** `new_payout`, using integer paise, so no caller
  can overdraw and no float rounding can hide a one-paise gap.

Concurrency is tested with real threads through the real endpoints: 12
simultaneous payout creations can consume the balance exactly once; 10
concurrent submits send one transfer; 8 concurrent confirmations settle once.

### Trip lifecycle is server-asserted

The client proposes a coarse phase (`start`, `boarding`, `depart`, `complete`);
the server decides the exact state via `can_transition_ride`. There is no
endpoint that accepts a status string verbatim, so a client cannot publish
straight to `completed` and release payables for a trip that never drove.
Completing a trip is what converts "collected" into "earned", and it is
idempotent with a conditional per-booking close so two concurrent completes
cannot double-close or re-notify.

**No-show is atomic with its refund** (real defect, fixed). The booking was
previously closed and the seat released *before* the refund ran; if the gateway
rejected the refund, the booking was left as a terminal no-show that refunded
nothing, and the idempotent early-return made it unrecoverable. The no-show now
rolls back both the booking and the seat when the refund fails, so the driver can
simply retry. A no-show also can never be overturned into a completed trip.

### Driver KYC

`backend/kyc.py` implements the workflow
`unverified -> submitted -> verified | rejected -> submitted`.

- A new vehicle is always `unverified`; there is **no client-settable field**,
  on create or update, that can reach `verified`.
- Submission requires the licence number, insurance number, and **both**
  documents, so a reviewer is never handed an incomplete packet.
- A rejection requires a reason - a driver told nothing can fix nothing.
- **Any document re-upload revokes verification.** Without this rule a driver
  could get a vehicle approved once and then swap in different papers, which is
  the obvious way to defeat the control. Tested directly.
- Publishing a ride is gated on `verified`, checked before any ride document is
  written, so an unverified driver cannot build a schedule and activate it later.
  The gate is **on by default** (`KYC_ENFORCE_PUBLISH`); it is left enabled
  across the entire 217-test suite rather than disabled to keep old tests green.

### Privacy

KYC documents are not reachable from the public `/uploads` route (that serves
avatars only) and require the owning user via
`GET /api/uploads/vehicle-doc/{id}/{kind}`. A rider, another driver, or an
anonymous caller gets 403/404. The privacy tests assert the document key is
never classified as a public avatar, so a future refactor that leaked a key into
that route would fail the suite.

## 3. Defects found and fixed in this cycle

Each of these passed the pre-existing test suite; they were found by writing
tests against the intended behaviour.

| # | Defect | Impact | Fix |
|---|---|---|---|
| 1 | `payments.booking_id` index was non-unique, and the gateway was called before the insert | duplicate payment rows + orphan live gateway orders on any double-tap | unique index + claim-before-gateway + stale-claim takeover |
| 2 | `outstanding_payable` subtracted only *paid* payouts | a `processing` payout still held its debit but looked available, allowing a **double payout** | `reserved_payable` counts every non-voided payout; cap enforced in `new_payout` with integer paise |
| 3 | No way to release a failed payout's reservation | a driver's balance could be stranded permanently | `void_payout` posts an explicit reversing ledger entry |
| 4 | No-show closed the booking before the refund, with no rollback | rider's money kept, unrecoverable, booking terminally closed | refund failure rolls back booking and seat; retry is immediate |
| 5 | `BOOKING_NO_SHOW` / `BOOKING_CONFIRMED` not imported at module scope in `rides.py` | `POST .../no-show` raised `NameError` -> **500 on every call** | imports moved to module scope; also fixed an `RIDE_BOARDABLE`/`RIDE_BOOKABLE` import typo |
| 6 | 0.01 float tolerance in the payout cap | permitted a one-paise overdraw | integer-paise comparison |
| 7 | Rate limiter used an absolute 60s window read at request time | `test_login_rate_limit` failed whenever it straddled a minute boundary (intermittent CI failure) | window is configurable; test pins a wide window and clears state |
| 8 | `manual` payouts could never reach `paid` | admin-created payouts were stuck in `processing` forever | audited `/confirm` endpoint requiring a unique UTR |
| 9 | `_unsettled_bookings` counted voided payouts as settled | a voided trip would never reappear for a replacement payout | voided payouts excluded |

## 4. Deliberate refusals

These were considered and rejected, and the reason is recorded so the next
person does not "fix" them into a defect:

- **No client-supplied status.** Coarse phase in, canonical state out.
- **No admin path to `paid` for gateway payouts.** A human clicking "send" is
  not evidence that money moved.
- **No float arithmetic on money.** Integer paise at the comparison boundary.
- **No `paid` in the generic status field.** A gateway that is merely reachable
  is not a gateway that is configured, and the failure mode is silently
  swallowing a transfer request.
- **No disabling the KYC gate to keep tests green.** The shared vehicle fixture
  is marked verified instead, so the gate stays live for all 217 tests.

## 5. Configuration

`backend/.env.example` ships placeholders only. It documents
`PLATFORM_FEE_PERCENT=30`, `MIN_PLATFORM_FEE=0`, `PAYOUT_PROVIDER`,
`RAZORPAY_X_ACCOUNT_NUMBER`, `MAX_PAYOUT_AMOUNT`, and `KYC_ENFORCE_PUBLISH=1`,
each with a comment explaining the consequence of getting it wrong. The
hardcoded Google client id was removed from `frontend/index.html`; the browser
reads provider config from `GET /api/auth/providers`.

## 6. NOT EXECUTED (blockers to a production-ready verdict)

These are honest gaps, not silent ones. None of them is a code defect.

1. **Real Razorpay TEST checkout has never been run.** Every gateway test uses a
   mocked SDK. Signature verification, webhook delivery, and order creation are
   proven against the documented contract, not against Razorpay. Required: a
   real order, a real `payment.captured` webhook, and a real refund.
2. **RazorpayX payout settlement unverified.** The transfer path is tested with a
   fake client; no linked account exists here, so no real `payout.processed` or
   `payout.failed` event has been observed.
3. **No KYC verification provider.** Document upload, review workflow, and the
   publish gate are complete and tested, but nothing automates the *review*; a
   human does it. No third-party document-authentication service is integrated.
4. **No browser E2E and no mobile screenshots.** The checkout flow is verified
   at the API layer and by `node --check`; it has not been driven in a real
   browser, so the gateway script load and the prefill path are unproven end to
   end.
5. **No release artifact or release secret scan.** `.gitignore`, `.dockerignore`,
   and the non-root Dockerfile were inspected, but no ZIP/image was built and
   scanned.
6. **The repository has no commits.** Every file is untracked, so nothing is
   deployable until an owner creates the initial commit.
7. **Atlas is unreachable** and any credential previously exposed in a template
   must be rotated by the owner.

## 7. To close the remaining gaps

1. With TEST keys and a webhook secret in `backend/.env`, run one real checkout
   end to end and confirm the `payment.captured` webhook settles the booking
   exactly once.
2. Link a RazorpayX account and observe a real `payout.processed` event moving a
   payout to `paid` without any admin action.
3. Drive the checkout in a real browser; capture the mobile and desktop evidence.
4. Build the release artifact, run the secret scan over it, then create the
   initial commit.
5. Decide the KYC policy: keep the current human review, or integrate a document
   verification provider.

## 8. Reproducing this report

```powershell
python -m pytest backend/tests -q -p no:logging     # 217 passed
pip check                                          # no broken requirements
Get-ChildItem frontend\js\*.js | ForEach-Object { node --check $_.FullName }
```

Requires a MongoDB instance on `localhost:27017`. The suite uses the
`ridemate_test` database and the `demo` payment provider, so no real money can
move while running it.
