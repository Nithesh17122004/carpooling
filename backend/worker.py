"""Standalone settlement worker.

Run as its own process -- ``python -m backend.worker`` -- never inside the web
dyno. An in-request or in-web infinite loop starves request handling and is killed
on every deploy, which in a payments system means earnings silently stop being
paid and nobody notices until a driver complains.

One pass does four things, in this order, because each depends on the last:

1. **auto-confirm** expired trip-completion windows, so a passenger who went
   quiet does not hold a driver's earnings forever;
2. **create** payouts for drivers with releasable earnings, on a schedule;
3. **submit** the payouts that are ready to go to the provider;
4. **reconcile** payouts stuck in `processing`, releasing claims a crashed worker
   left behind.

Correctness properties, and the reason each is implemented the way it is:

* **A single pass is idempotent.** Creation is keyed on a deterministic period
  reference with a unique index behind it, so a crash between "create" and
  "remember that I created it" cannot produce two payouts for one period.
* **Claims have a lease, not a lock.** A worker that dies mid-pass releases its
  work after ``SETTLEMENT_CLAIM_TTL_SECONDS`` rather than stranding it forever.
* **Nothing is trusted from the driver's own request.** Every amount comes from
  the ledger via `payouts.outstanding_payable`, which already subtracts held
  (unconfirmed/disputed) earnings and open reservations.
* **Dry run is loud.** With ``PAYOUT_DRY_RUN`` the worker still walks the whole
  state machine but dispatches through a provider stub, and says so on every
  line it logs -- a silent no-op would be indistinguishable from success.
"""

import logging
import os
import signal
import sys
import time
from datetime import timedelta

from .db import get_db, utcnow
from .states import BOOKING_COMPLETED

log = logging.getLogger("backend.worker")

# Set by the signal handler; the loop checks it between steps so a deploy can
# finish the current step and exit rather than being killed mid-transfer.
_stopping = False


def _request_stop(signum, _frame):
    global _stopping
    _stopping = True
    log.info("worker received signal %s; finishing the current pass then exiting",
             signum)


# ------------------------------------------------------------------ step 1
def auto_confirm_pass(limit=100):
    """Close confirmation windows the passenger never answered."""
    from . import completion

    with app_context():
        return completion.auto_confirm_expired(limit=limit)


# ------------------------------------------------------------------ step 2
def eligible_drivers(limit=25, min_age_seconds=0):
    """Drivers with releasable earnings, oldest earnings first.

    Driven from the ledger rather than from a scan of `users`: only accounts
    that actually have a `DRIVER_PAYABLE` entry can ever be eligible, so the
    candidate set is proportional to the number of unpaid drivers instead of to
    the size of the user base. The `limit(1000)`-a-user-table approach would
    silently stop settling anyone past the cap.
    """
    from bson import ObjectId

    from . import onboarding, payouts

    db = get_db()
    cutoff = utcnow() - timedelta(seconds=max(min_age_seconds, 0))

    # Only accounts that have ever been credited a payable.
    account_ids = db.ledger_entries.distinct(
        "account_id", {"account_type": "driver", "entry_type": "DRIVER_PAYABLE"})
    if not account_ids:
        return []

    candidates = []
    for account_id in account_ids:
        try:
            uid = ObjectId(account_id)
        except Exception:  # noqa: BLE001 - a malformed id is not this worker's problem
            log.warning("skipping ledger account with unparseable id %r", account_id)
            continue
        user = db.users.find_one({"_id": uid})
        if not user or not onboarding.is_settled_ready(user):
            continue
        amount = payouts.outstanding_payable(uid)
        if amount <= 0:
            continue
        # Oldest unreleased earnings first: a driver whose trip settled weeks ago
        # has waited longer than one who finished a moment ago.
        oldest = db.bookings.find_one(
            {"owner_id": uid, "status": BOOKING_COMPLETED},
            sort=[("passenger_confirmed_at", 1), ("completed_at", 1)])
        first_payable_at = (oldest or {}).get("passenger_confirmed_at") \
            or (oldest or {}).get("completed_at")
        # SETTLEMENT_MIN_AGE_SECONDS is a grace period on top of confirmation,
        # so a just-confirmed trip is not paid in the same pass.
        if first_payable_at and first_payable_at > cutoff:
            continue
        candidates.append((first_payable_at or utcnow(), uid, amount))

    # Undated candidates (no completed booking row visible) sort last but are
    # still eligible -- they are real money owed.
    candidates.sort(key=lambda row: (row[0] is None, row[0] or utcnow()))
    return [{"user_id": uid, "amount": amount}
            for _seen, uid, amount in candidates[:limit]]


def create_payouts_pass(limit=25, min_age_seconds=0):
    """Create (but do not submit) payouts for eligible drivers.

    Idempotent per driver per period: the period key is stored on the payout and
    backed by a unique index, so a repeated pass finds the existing payout
    instead of creating a second one.
    """
    from . import payouts

    created = []
    period_key = current_period_key()
    for row in eligible_drivers(limit=limit, min_age_seconds=min_age_seconds):
        uid, amount = row["user_id"], row["amount"]
        existing = get_db().payouts.find_one(
            {"user_id": uid, "period.auto_key": period_key,
             "status": {"$ne": payouts.PAYOUT_VOIDED}})
        if existing:
            continue
        try:
            payout = payouts.new_payout(
                uid, amount,
                period={"auto_key": period_key, "auto": True,
                        "min_age_seconds": min_age_seconds})
        except Exception as exc:  # noqa: BLE001 - one bad driver must not stop the pass
            log.warning("auto payout skipped for %s: %s", uid, exc)
            continue
        created.append(str(payout["_id"]))
    if created:
        log.info("created %d payout(s) for period %s", len(created), period_key)
    return created


def current_period_key():
    """Stable key for "this settlement window".

    UTC date + the configured minimum-age bucket. Two passes in the same window
    produce the same key, which is what makes creation idempotent; a new day (or
    a changed grace period) produces a new one, which is what lets a driver be
    paid again.
    """
    from flask import current_app

    min_age = int(current_app.config.get("SETTLEMENT_MIN_AGE_SECONDS", 0) or 0)
    return "%s:%d" % (utcnow().date().isoformat(), min_age)


# ------------------------------------------------------------------ step 3
def submit_pass(limit=25):
    """Send ready payouts to the provider."""
    from . import payouts

    db = get_db()
    submitted = []
    rows = db.payouts.find({"status": payouts.PAYOUT_PENDING}).sort("created_at", 1)
    for payout in rows.limit(limit):
        try:
            payouts.submit_payout(payout["_id"],
                                  idempotency_key=payout.get("reference"))
            submitted.append(str(payout["_id"]))
        except Exception as exc:  # noqa: BLE001 - keep settling the others
            log.warning("payout %s not submitted: %s", payout.get("reference"), exc)
    return submitted


# ------------------------------------------------------------------ step 4
def reconcile_pass(ttl_seconds=300):
    """Recover work abandoned by a crashed worker.

    A payout in `processing` whose claim has expired is not evidence that money
    moved -- only the provider webhook is. So it is returned to `failed` (an
    explicit, visible, retryable state) rather than being optimistically marked
    paid, which would be a double-payment bug.
    """
    from . import payouts

    db = get_db()
    cutoff = utcnow() - timedelta(seconds=max(ttl_seconds, 0))
    released = []
    rows = db.payouts.find({
        "status": payouts.PAYOUT_PROCESSING,
        "$or": [{"processing_at": {"$lte": cutoff}},
                {"processing_at": None}],
    }).limit(100)
    for payout in rows:
        claim = db.payouts.find_one_and_update(
            {"_id": payout["_id"], "status": payouts.PAYOUT_PROCESSING},
            {"$set": {"status": payouts.PAYOUT_FAILED,
                      "failed_at": utcnow(),
                      "failure_reason": "worker_claim_expired",
                      "updated_at": utcnow()}})
        if claim is not None:
            released.append(str(payout["_id"]))
    if released:
        log.warning("released %d payout(s) stuck in processing; "
                    "they need a provider webhook or a manual retry", len(released))
    return released


# ------------------------------------------------------------------- the loop
def run_once():
    """One full pass. Returns a summary dict -- also used directly by tests."""
    return {
        "auto_confirmed": auto_confirm_pass(),
        "created": create_payouts_pass(min_age_seconds=min_age_seconds()),
        "submitted": submit_pass(),
        "released": reconcile_pass(ttl_seconds=claim_ttl()),
    }


def min_age_seconds():
    from flask import current_app

    return int(current_app.config.get("SETTLEMENT_MIN_AGE_SECONDS", 0) or 0)


def claim_ttl():
    from flask import current_app

    return int(current_app.config.get("SETTLEMENT_CLAIM_TTL_SECONDS", 300) or 300)


def poll_seconds():
    from flask import current_app

    return int(current_app.config.get("SETTLEMENT_POLL_SECONDS", 20) or 20)


def app_context():
    """Build the Flask app context the domain modules need.

    The worker is a plain process with no request, so it creates the app itself
    exactly as the web process does. `create_app()` runs `validate_runtime`,
    which means a misconfigured worker refuses to start rather than starting
    and quietly settling nothing.
    """
    from .app import create_app

    return create_app().app_context()


def main(argv=None):
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)
    once = "--once" in argv

    signal.signal(signal.SIGTERM, _request_stop)
    try:
        signal.signal(signal.SIGINT, _request_stop)
    except (AttributeError, ValueError):
        pass  # not available on every platform

    with app_context() as ctx:
        from flask import current_app

        if not current_app.config.get("SETTLEMENT_ENABLED", True):
            log.error("SETTLEMENT_ENABLED is false; refusing to start. Driver "
                      "earnings are owed money and must be settled.")
            return 2
        if current_app.config.get("PAYOUT_DRY_RUN"):
            log.warning("PAYOUT_DRY_RUN is ON: this worker will NOT move real "
                        "money. Every payout it creates is a simulation.")

        log.info("settlement worker started (provider=%s, poll=%ss)",
                 current_app.config.get("PAYOUT_PROVIDER", "manual"),
                 poll_seconds())
        while not _stopping:
            started = time.time()
            failed = False
            try:
                summary = run_once()
                if any(summary.values()):
                    log.info("pass: %s", summary)
            except Exception:  # noqa: BLE001 - a failed pass must not kill the worker
                log.exception("settlement pass failed; retrying next tick")
                failed = True
            if once:
                # `--once` is the CI/one-shot mode, so a failure must be visible
                # in the exit code rather than only in the log.
                return 1 if failed else 0
            # Never busy-spin, and always sleep even after an error.
            elapsed = time.time() - started
            for _ in range(int(max(poll_seconds() - elapsed, 1))):
                if _stopping:
                    break
                time.sleep(1)
    log.info("settlement worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
