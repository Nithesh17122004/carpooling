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


def payout_provider():
    return getattr(current_app, "config", {}).get("PAYOUT_PROVIDER", "manual")


def _razorpayx_account():
    from .config import Config

    return (current_app.config.get("RAZORPAY_X_ACCOUNT_NUMBER", "")
            or getattr(Config, "RAZORPAY_X_ACCOUNT_NUMBER", ""))


def _razorpayx_client():
    """RazorpayX client, or None when the account is not linked/configured."""
    from .config import Config

    if not Config.RAZORPAY_KEY_ID or not Config.RAZORPAY_KEY_SECRET:
        return None
    if not _razorpayx_account():
        return None
    try:
        import razorpay

        return razorpay.Client(auth=(Config.RAZORPAY_KEY_ID, Config.RAZORPAY_KEY_SECRET))
    except ImportError:
        return None


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
    """
    from .ledger import post_entry

    amount = round(float(amount), 2)
    if amount <= 0:
        raise APIError("Payout amount must be greater than zero.", 422,
                       code="invalid_payout_amount")
    limit = float(getattr(current_app, "config", {}).get(
        "MAX_PAYOUT_AMOUNT", DEFAULT_MAX_PAYOUT_AMOUNT) or DEFAULT_MAX_PAYOUT_AMOUNT)
    if amount > limit:
        raise APIError("Payout exceeds the single-transfer limit.", 422,
                       code="payout_too_large")
    outstanding = outstanding_payable(user_id)
    # Compare in integer paise: a float tolerance would quietly permit an
    # overdraw of a single paisa, which is exactly the kind of rounding gap
    # that lets the same rupee be paid twice.
    if _paise(amount) > _paise(max(outstanding, 0.0)):
        raise APIError(
            "Payout exceeds the driver's outstanding balance.", 422,
            code="payout_exceeds_balance",
            details={"requested": amount, "outstanding": round(max(outstanding, 0.0), 2)})

    db = get_db()
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
    post_entry(account_id=user_id, account_type="driver", entry_type="DRIVER_PAYOUT",
               amount=-amount, reference=doc["reference"],
               meta={"payout_id": str(doc["_id"])})
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
                                        "failure_reason": type(exc).__name__,
                                        "updated_at": utcnow()}})
        raise APIError("The payout could not be sent to the provider.", 502,
                       code="payout_submit_failed", details={"reason": type(exc).__name__})

    if provider_ref:
        db.payouts.update_one({"_id": payout_id},
                              {"$set": {"provider_reference": provider_ref}})
        claimed["provider_reference"] = provider_ref
    return db.payouts.find_one({"_id": payout_id}) or claimed


def _dispatch(payout):
    """Ask the configured provider to move the money. Returns its reference."""
    provider = payout.get("provider", "manual")
    if provider == "razorpayx":
        client = _razorpayx_client()
        if client is None:
            raise APIError("RazorpayX is not configured on the server.", 503,
                           code="payouts_unconfigured")
        account = _razorpayx_account()
        try:
            resp = client.payout.create({
                "account_number": account,
                "amount": int(round(float(payout["amount"]) * 100)),
                "currency": payout.get("currency", "INR"),
                "mode": "UPI",
                "purpose": "payout",
                "reference_id": payout["reference"],
            })
        except Exception as exc:  # noqa: BLE001
            raise APIError("The payout was rejected by the provider.", 502,
                           code="payout_submit_failed", details={"reason": type(exc).__name__})
        return (resp or {}).get("id")
    # `manual` provider: staff settle out of band and confirm via webhook/UI.
    # The payout still sits in `processing` until a confirmation arrives.
    return None


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


def outstanding_payable(user_id):
    """Earned by the driver and NOT yet committed to any payout attempt.

    This is the only figure a new payout is allowed to consume. Using
    `settled_payable` here instead would be a double-payment bug: a payout that
    is merely `processing` has already debited the driver account, so treating
    it as still-available would let a second payout be created for the same
    rupees.
    """
    return round(_earned(user_id) - reserved_payable(user_id), 2)


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
