"""Driver payout settlement.

`DRIVER_PAYABLE` is an accounting entry, not a transfer: it says what the
platform owes a driver. Moving that money out of the platform is a separate,
provider-confirmed act, and this module is the only place that does it.

State machine (mirrored by `_PAYOUT_TRANSITIONS`):
    pending    -> processing -> paid
                       |         ^
                       v         |  (a provider webhook may confirm a payout
                      failed ----+  that was left in processing)
                       |
                       v
                     voided  (failed only: reservation released back to the
                               driver via a reversing ledger entry)

The one invariant that matters most: **only a verified provider confirmation may
set `paid`.** There is deliberately no admin/API path that flips a payout to
paid, because a human "I clicked send" is not evidence that money moved. An
admin can retry or void a failed payout, but the gateway is the sole authority
on whether a payout actually landed.
"""

import uuid

from flask import current_app

from .db import get_db, utcnow
from .errors import APIError
from .states import BOOKING_AWAITING_COMPLETION, BOOKING_DISPUTED

PAYOUT_PENDING = "pending"
PAYOUT_PROCESSING = "processing"
PAYOUT_PAID = "paid"
PAYOUT_FAILED = "failed"
PAYOUT_VOIDED = "voided"

# Payout rows that still hold a DRIVER_PAYOUT debit against the driver's
# balance. A debit is written when the payout is created and is only undone by
# `void_payout`, so EVERY state except `voided` keeps holding the money --
# including `failed`. A failed transfer is not money in the driver's pocket,
# but the platform still owes it, and the only ways to release it are retry or
# void. Counting only `paid` here (or excluding `failed`) would let a second
# payout be created for the same earnings while the first debit still stands.
_RESERVING_STATES = (PAYOUT_PENDING, PAYOUT_PROCESSING, PAYOUT_PAID, PAYOUT_FAILED)

PAYOUT_STATES = {PAYOUT_PENDING, PAYOUT_PROCESSING, PAYOUT_PAID,
                 PAYOUT_FAILED, PAYOUT_VOIDED}

_PAYOUT_TRANSITIONS = {
    PAYOUT_PENDING: {PAYOUT_PROCESSING, PAYOUT_FAILED},
    PAYOUT_PROCESSING: {PAYOUT_PAID, PAYOUT_FAILED},
    PAYOUT_FAILED: {PAYOUT_PENDING, PAYOUT_VOIDED},   # retry, or give up
    PAYOUT_VOIDED: set(),                             # terminal
    PAYOUT_PAID: set(),                               # terminal, money already moved
}

# Payouts are a manual, low-volume, high-consequence action. Anything above this
# is refused rather than silently split, because a partial transfer that is not
# modelled in the ledger is how money goes missing.
DEFAULT_MAX_PAYOUT_AMOUNT = 1_000_000.0


def _paise(value):
    """Exact integer paise for a rupee amount. Money comparisons are done here
    so binary-float rounding can never create or hide a one-paise gap."""
    return int(round(float(value or 0.0) * 100))


def _attempts(payout):
    """How many times this payout has been handed to a provider."""
    try:
        return int(payout.get("dispatch_attempts") or 0)
    except (TypeError, ValueError):
        return 0


def payout_provider():
    return getattr(current_app, "config", {}).get("PAYOUT_PROVIDER", "manual")


def can_transition_payout(source, target):
    return target in _PAYOUT_TRANSITIONS.get(source, set())


def new_payout(user_id, amount, *, currency="INR", period=None, booking_ids=None):
    """Create a payout in `pending`. Does NOT move money.

    The driver balance is debited immediately (the reservation is held from the
    moment of creation, not from submission), which is what stops the same
    earnings from being paid out twice by two concurrent requests. The cap is
    enforced HERE rather than only in the admin route, so no caller can bypass
    the invariant that a payout never exceeds what the driver has earned and not
    already reserved.

    Payout onboarding is checked before anything is written. The gate lives here
    rather than in the admin route for the same reason the cap does: a route that
    forgets to call it is an unbounded payout path, and money moving into an
    unverified account is not recoverable by asking nicely afterwards.

    The gate runs *after* argument validation on purpose. A caller that sent a
    zero or absurd amount deserves to be told that, not to be told about their
    onboarding status; and a caller that sent a valid amount deserves the
    onboarding error rather than a confusing amount error.
    """
    from . import onboarding
    from .ledger import assert_currency, business_key as ledger_business_key, post_entry

    # A payout in a currency the service does not account in would be summed
    # against INR driver earnings, so it is refused before any money is reserved.
    currency = assert_currency(currency, field="payout currency")

    amount = round(float(amount), 2)
    if amount <= 0:
        raise APIError("Payout amount must be greater than zero.", 422,
                       code="invalid_payout_amount")
    limit = float(getattr(current_app, "config", {}).get(
        "MAX_PAYOUT_AMOUNT", DEFAULT_MAX_PAYOUT_AMOUNT) or DEFAULT_MAX_PAYOUT_AMOUNT)
    if amount > limit:
        raise APIError("Payout exceeds the single-transfer limit.", 422,
                       code="payout_too_large")

    db = get_db()
    driver = db.users.find_one({"_id": user_id})
    if driver is None:
        raise APIError("Driver not found.", 404, code="not_found")
    onboarding.assert_settlement_allowed(driver, action="create_payout")

    outstanding = outstanding_payable(user_id)
    # Compare in integer paise: a float tolerance would quietly permit an
    # overdraw of a single paisa, which is exactly the kind of rounding gap
    # that lets the same rupee be paid twice.
    if _paise(amount) > _paise(max(outstanding, 0.0)):
        raise APIError(
            "Payout exceeds the driver's outstanding balance.", 422,
            code="payout_exceeds_balance",
            details={"requested": amount, "outstanding": round(max(outstanding, 0.0), 2),
                     "held_pending_completion": held_payable(user_id)})

    doc = {
        "user_id": user_id,
        "amount": amount,
        "currency": currency,
        "status": PAYOUT_PENDING,
        "provider": payout_provider(),
        "reference": "PO-" + uuid.uuid4().hex[:12].upper(),
        "provider_reference": None,
        "period": period or {},
        "booking_ids": [str(b) for b in (booking_ids or [])],
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "processing_at": None,
        "paid_at": None,
        "failed_at": None,
        "failure_reason": None,
    }
    doc["_id"] = db.payouts.insert_one(doc).inserted_id
    # The business key is derived from the payout id, so even if the insert
    # above were somehow replayed the ledger effect can only be posted once.
    post_entry(account_id=user_id, account_type="driver", entry_type="DRIVER_PAYOUT",
               amount=-amount, reference=doc["reference"],
               meta={"payout_id": str(doc["_id"])},
               business_key=ledger_business_key("payout", doc["_id"]))
    return doc


def _claim(payout_id, from_states, to_state, **extra):
    """Atomically move a payout between states, or return None if lost the race."""
    fields = {"status": to_state, "updated_at": utcnow()}
    fields.update(extra)
    return get_db().payouts.find_one_and_update(
        {"_id": payout_id, "status": {"$in": list(from_states)}},
        {"$set": fields})


def submit_payout(payout_id, *, idempotency_key=None):
    """Send a payout to the provider. pending -> processing.

    Idempotent: re-submitting a payout that is already `processing` returns it
    unchanged instead of creating a second transfer. This is the property that
    makes a retry after a timeout safe.
    """
    from . import payout_providers

    db = get_db()
    payout = db.payouts.find_one({"_id": payout_id})
    if not payout:
        raise APIError("Payout not found.", 404, code="not_found")
    if payout["status"] in (PAYOUT_PROCESSING, PAYOUT_PAID):
        return payout                       # already on its way / already done
    if payout["status"] == PAYOUT_FAILED:
        raise APIError("This payout failed; retry it before submitting.", 409,
                       code="payout_failed")
    if not can_transition_payout(payout["status"], PAYOUT_PROCESSING):
        raise APIError(f"A payout cannot move from '{payout['status']}' to processing.",
                       409, code="invalid_transition")

    claimed = _claim(payout_id, [PAYOUT_PENDING], PAYOUT_PROCESSING,
                     processing_at=utcnow())
    if claimed is None:
        # a concurrent submit won; return whatever it produced
        return db.payouts.find_one({"_id": payout_id})

    provider_ref = None
    try:
        provider_ref = _dispatch(claimed)
    except payout_providers.PayoutProviderError as exc:
        # Transient failures stay retryable. A timeout does not mean the
        # transfer failed -- it means we do not know. Failing the payout here
        # would tell the worker to give up on money that may already be on its
        # way, and a retry must be safe in exactly this situation, which is why
        # the provider sends a deterministic idempotency key.
        #
        # Permanent failures fail the payout. Retrying a rejected beneficiary
        # account forever hides a real onboarding problem behind a queue that
        # never drains.
        if exc.transient:
            db.payouts.update_one({"_id": payout_id, "status": PAYOUT_PROCESSING},
                                  {"$set": {"status": PAYOUT_PENDING,
                                            "processing_at": None,
                                            "dispatch_attempts": _attempts(claimed) + 1,
                                            "last_dispatch_error": exc.reason,
                                            "last_dispatch_attempt_at": utcnow(),
                                            "updated_at": utcnow()}})
        else:
            db.payouts.update_one({"_id": payout_id, "status": PAYOUT_PROCESSING},
                                  {"$set": {"status": PAYOUT_FAILED,
                                            "failed_at": utcnow(),
                                            "dispatch_attempts": _attempts(claimed) + 1,
                                            "failure_reason": exc.reason,
                                            "last_dispatch_error": exc.reason,
                                            "last_dispatch_attempt_at": utcnow(),
                                            "updated_at": utcnow()}})
        if exc.transient:
            raise APIError("The provider could not be reached. The payout is still "
                           "pending and will be retried.", 503,
                           code="payout_provider_unavailable",
                           details={"reason": exc.reason}) from None
        raise APIError("The payout was rejected by the provider.", 502,
                       code="payout_submit_failed",
                       details={"reason": exc.reason}) from None
    except APIError:
        # Release the claim so the payout is retryable rather than stuck.
        db.payouts.update_one({"_id": payout_id, "status": PAYOUT_PROCESSING},
                              {"$set": {"status": PAYOUT_PENDING,
                                        "processing_at": None, "updated_at": utcnow()}})
        raise
    except Exception as exc:  # noqa: BLE001
        db.payouts.update_one({"_id": payout_id, "status": PAYOUT_PROCESSING},
                              {"$set": {"status": PAYOUT_FAILED,
                                        "failed_at": utcnow(),
                                        "dispatch_attempts": _attempts(claimed) + 1,
                                        "failure_reason": type(exc).__name__,
                                        "last_dispatch_attempt_at": utcnow(),
                                        "updated_at": utcnow()}})
        raise APIError("The payout could not be sent to the provider.", 502,
                       code="payout_submit_failed", details={"reason": type(exc).__name__})

    if provider_ref:
        db.payouts.update_one({"_id": payout_id},
                              {"$set": {"provider_reference": provider_ref}})
        claimed["provider_reference"] = provider_ref
    return db.payouts.find_one({"_id": payout_id}) or claimed


def _dispatch(payout):
    """Send the money through the payout's provider. Returns its reference.

    The destination is resolved from the driver's own verified onboarding
    record by the provider, never from configuration. The old
    `RAZORPAY_X_ACCOUNT_NUMBER` fallback is gone: a single global account meant
    one driver received everyone's earnings, or everyone's earnings vanished
    into a platform account.
    """
    from . import payout_providers

    provider = payout_providers.provider_for(payout.get("provider", "manual"))
    driver = get_db().users.find_one({"_id": payout["user_id"]})
    if driver is None:
        raise APIError("Driver not found for this payout.", 404, code="not_found")
    return provider.send(payout, driver)


def confirm_payout(payout_id, provider_reference, *, source="provider"):
    """Mark a payout PAID. This is the ONLY path to `paid`.

    Called from the signed provider webhook (and from a manual-confirmation
    endpoint that records the bank reference). It refuses a payout that is not
    in `processing`, which stops a stale or forged callback from marking a
    never-sent payout as paid.
    """
    db = get_db()
    payout = db.payouts.find_one({"_id": payout_id})
    if not payout:
        raise APIError("Payout not found.", 404, code="not_found")
    if payout["status"] == PAYOUT_PAID:
        return payout                       # idempotent
    if payout["status"] != PAYOUT_PROCESSING:
        raise APIError(f"A payout can only be confirmed while processing "
                       f"(this one is '{payout['status']}').", 409,
                       code="payout_not_processing")

    paid = _claim(payout_id, [PAYOUT_PROCESSING], PAYOUT_PAID,
                  paid_at=utcnow(), provider_reference=provider_reference,
                  confirmed_by=source)
    if paid is None:
        return db.payouts.find_one({"_id": payout_id})
    return db.payouts.find_one({"_id": payout_id})


def fail_payout(payout_id, reason, *, source="provider"):
    """processing -> failed. Retryable, and the ledger entry is retained so the
    money is still accounted for as owed-and-attempted."""
    db = get_db()
    payout = db.payouts.find_one({"_id": payout_id})
    if not payout:
        raise APIError("Payout not found.", 404, code="not_found")
    if payout["status"] == PAYOUT_PAID:
        raise APIError("A paid payout cannot be failed.", 409, code="payout_paid")
    if payout["status"] == PAYOUT_FAILED:
        return payout
    failed = _claim(payout_id, [PAYOUT_PENDING, PAYOUT_PROCESSING], PAYOUT_FAILED,
                    failed_at=utcnow(), failure_reason=str(reason)[:200],
                    confirmed_by=source)
    if failed is None:
        return db.payouts.find_one({"_id": payout_id})
    return db.payouts.find_one({"_id": payout_id})


def retry_payout(payout_id):
    """failed -> pending, so it can be submitted again.

    The original DRIVER_PAYOUT ledger entry is left in place: the platform still
    owes the driver this money, and a fresh submission re-uses the same ledger
    row rather than double-debiting.
    """
    db = get_db()
    payout = db.payouts.find_one({"_id": payout_id})
    if not payout:
        raise APIError("Payout not found.", 404, code="not_found")
    if payout["status"] != PAYOUT_FAILED:
        raise APIError("Only a failed payout can be retried.", 409, code="invalid_transition")
    retried = _claim(payout_id, [PAYOUT_FAILED], PAYOUT_PENDING,
                     failed_at=None, failure_reason=None, processing_at=None)
    if retried is None:
        return db.payouts.find_one({"_id": payout_id})
    return db.payouts.find_one({"_id": payout_id})


def settled_payable(user_id):
    """Amount the provider has confirmed it sent (payouts that reached `paid`)."""
    db = get_db()
    rows = db.payouts.find({"user_id": user_id, "status": PAYOUT_PAID})
    return round(sum(float(r.get("amount", 0.0) or 0.0) for r in rows), 2)


def _earned(user_id):
    """Lifetime driver payables: gross earned, net of reversals.

    Payout debits are deliberately excluded. A payable is money the platform
    owes regardless of whether it has been sent yet, so it is tracked
    separately from the reservation and the settlement.
    """
    db = get_db()
    rows = db.ledger_entries.find({"account_id": str(user_id),
                                   "account_type": "driver",
                                   "entry_type": {"$in": ["DRIVER_PAYABLE",
                                                           "DRIVER_PAYABLE_REVERSAL"]}})
    return round(sum(float(r.get("amount", 0.0) or 0.0) for r in rows), 2)


earned_payable = _earned


def reserved_payable(user_id):
    """Amount already debited against the driver by an un-voided payout.

    Counts every payout that has not been voided -- pending, processing, paid
    AND failed -- because all of them still carry a DRIVER_PAYOUT debit. Only
    `void_payout` posts the reversing entry that makes the money available
    again, so this figure and the ledger can never disagree.
    """
    db = get_db()
    rows = db.payouts.find({"user_id": user_id, "status": {"$in": list(_RESERVING_STATES)}})
    return round(sum(float(r.get("amount", 0.0) or 0.0) for r in rows), 2)


# Booking states whose earnings must not be paid out yet. Defined here, in
# payout terms, because "which bookings are these" is a question the money path
# asks -- `states.booking_blocks_settlement` is the wider set that also covers
# refunds, which have their own separate flow.
_HELD_BOOKING_STATES = (BOOKING_AWAITING_COMPLETION, BOOKING_DISPUTED)


def _driver_net_of(booking):
    """The driver's share of one booking, read from the commission rate frozen
    onto its payment at order creation.

    Read from the payment rather than recomputed from config: a later commission
    change must not change what a historical trip owes, and this figure is
    subtracted from a real balance.
    """
    db = get_db()
    payment = db.payments.find_one({"booking_id": booking["_id"]})
    if not payment:
        # No payment row means no DRIVER_PAYABLE was ever posted for this trip,
        # so there is nothing held to subtract.
        return 0.0
    try:
        return float(payment.get("driver_net") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def held_payable(user_id):
    """Earned money that is NOT yet releasable to a payout.

    This is the bridge between the trip-completion state machine and the money
    path. A booking's `DRIVER_PAYABLE` ledger entry is posted when the fare is
    collected, which is *before* anybody knows whether the trip happened. The
    ledger alone would therefore let a driver be paid for a trip the passenger
    never confirmed, or one under active dispute.

    Rather than withholding the ledger entry (which would break
    `gross == fee + net` reconciliation for a legitimate fare), the rupees stay
    owed and are reported here as held. `outstanding_payable` subtracts them, so
    a held rupee is visible in the balance, excluded from payout, and released
    automatically the moment the trip is confirmed.
    """
    from . import completion

    db = get_db()
    total = 0.0
    for booking in db.bookings.find({"owner_id": user_id,
                                     "status": {"$in": list(_HELD_BOOKING_STATES)}}):
        if completion.is_payable(booking):
            # Already confirmed or auto-confirmed: the row is stale in this
            # query's window and must not be withheld.
            continue
        total += _driver_net_of(booking)
    return round(total, 2)


def outstanding_payable(user_id):
    """Earned by the driver and NOT yet committed to any payout attempt.

    This is the only figure a new payout is allowed to consume. Using
    `settled_payable` here instead would be a double-payment bug: a payout that
    is merely `processing` has already debited the driver account, so treating
    it as still-available would let a second payout be created for the same
    rupees.

    Held earnings (unconfirmed or disputed trips) are subtracted, so the payout
    cap cannot be used to pay out a trip the passenger has not confirmed.
    """
    return round(_earned(user_id) - reserved_payable(user_id) - held_payable(user_id), 2)


def void_payout(payout_id, reason):
    """Cancel a FAILED payout and return its rupees to the driver's balance.

    This is the only way reserved money becomes payable again. It posts an
    explicit reversing ledger entry (the original DRIVER_PAYOUT is immutable),
    so the books stay auditable: earned, reserved, and settled can always be
    reconciled against each other. A paid payout can never be voided.
    """
    from .ledger import post_entry

    db = get_db()
    payout = db.payouts.find_one({"_id": payout_id})
    if not payout:
        raise APIError("Payout not found.", 404, code="not_found")
    if payout["status"] == PAYOUT_PAID:
        raise APIError("A paid payout cannot be voided; the money already moved.",
                       409, code="payout_paid")
    if payout["status"] != PAYOUT_FAILED:
        raise APIError("Only a failed payout can be voided.", 409, code="invalid_transition")

    voided = _claim(payout_id, [PAYOUT_FAILED], PAYOUT_VOIDED,
                    voided_at=utcnow(), void_reason=str(reason)[:200])
    if voided is None:
        return db.payouts.find_one({"_id": payout_id})

    post_entry(account_id=payout["user_id"], account_type="driver",
               entry_type="DRIVER_PAYOUT_REVERSAL", amount=round(float(payout["amount"]), 2),
               reference=payout.get("reference"),
               meta={"payout_id": str(payout_id), "reason": str(reason)[:200]})
    return db.payouts.find_one({"_id": payout_id})
