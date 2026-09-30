"""Authoritative payment reconciliation against Razorpay.

Razorpay is the source of truth for whether money moved. This module is the only
place that is allowed to say a payment was captured, and it reaches that
conclusion the same way from both entry points:

    booking -> internal payment -> internal order_id -> Razorpay payment
                                                -> Razorpay webhook event

Three properties the rest of the system relies on:

**Nothing from the browser is believed.** The callback may submit exactly three
things -- ``razorpay_payment_id``, ``razorpay_order_id`` and
``razorpay_signature``. Amount, status and currency are read from Razorpay over
the server API and compared against values computed here. A tampered callback
therefore fails even when its signature is genuine, because a genuine signature
only proves the caller holds the secret for *that* order/payment pair.

**The relationship is checked end to end.** A payment fetched from Razorpay must
name *our* order, must be captured, and must be for the exact gross we computed
and froze. Each of those is a separate refusal, not one bundled boolean, so the
failure reason recorded in the audit trail is the real one.

**Reconciliation is recorded once and never rewritten.** Every verification
appends to ``payment_reconciliations`` under a unique business key, so a
duplicated callback, a retried webhook, and an out-of-order callback/webhook
pair all converge on one capture with one record.

The commission split is NOT recomputed here. It is read from the values frozen
on the payment document at order creation, so this module cannot change what a
driver is owed.
"""

import hmac
import hashlib

from pymongo.errors import DuplicateKeyError

from .db import get_db, utcnow
from .errors import APIError
from .ledger import CURRENCY, assert_currency, commission_for_payment

# The only fields a browser callback is allowed to contribute. Anything else in
# the request body is ignored outright -- not read, not trusted, not logged.
ALLOWED_CALLBACK_FIELDS = ("razorpay_payment_id", "razorpay_order_id",
                           "razorpay_signature")

# Where a verification came from. Stored on every reconciliation record so an
# operator can tell a user-reported capture from a gateway-reported one.
SOURCE_BROWSER_CALLBACK = "browser_callback"
SOURCE_WEBHOOK = "webhook"
SOURCE_DEMO = "demo"


def verify_checkout_signature(order_id, payment_id, signature, secret):
    """Razorpay Checkout signature: HMAC-SHA256(secret, "order_id|payment_id").

    A genuine signature proves only that the caller knows the key secret for
    this exact pair. It does not prove the payment was captured, nor that the
    amount was the right one -- those come from the API fetch below.
    """
    if not signature or not order_id or not payment_id or not secret:
        return False
    expected = hmac.new(secret.encode(),
                        f"{order_id}|{payment_id}".encode(),
                        hashlib.sha256).hexdigest()
    # compare_digest is constant-time: a byte-by-byte compare leaks the digest
    # one character at a time to an attacker who can time the response.
    return hmac.compare_digest(expected, signature)


def _extract_callback_fields(payload):
    """Pull only the three permitted fields out of a browser callback body."""
    return {field: (payload.get(field) or "").strip()
            if isinstance(payload.get(field), str) else ""
            for field in ALLOWED_CALLBACK_FIELDS}


def _check_against_payment(fetched, payment):
    """Validate a Razorpay payment object against our frozen expectations.

    Returns a list of human-readable failure reasons. An empty list means the
    gateway object and our record agree on every dimension that matters.
    """
    expected_currency = assert_currency(payment.get("currency"))
    split = commission_for_payment(payment)
    expected_gross = round(float(split["gross"]), 2)

    problems = []
    status = (fetched.get("status") or "").lower()
    if status != "captured":
        problems.append("status is %r, expected 'captured'" % (status or "missing",))

    # Razorpay reports money in the minor unit.
    try:
        fetched_amount = round(float(fetched.get("amount", 0)) / 100, 2)
    except (TypeError, ValueError):
        fetched_amount = None
    if fetched_amount is None:
        problems.append("gateway amount is not a number")
    elif abs(fetched_amount - expected_gross) > 0.01:
        problems.append("captured %s, expected %s" % (fetched_amount, expected_gross))

    fetched_currency = (fetched.get("currency") or "").upper()
    if fetched_currency != expected_currency:
        problems.append("currency is %r, expected %r"
                        % (fetched_currency or "missing", expected_currency))

    # The link that stops a valid payment for a *different* order being used to
    # settle this one.
    our_order = payment.get("order_id")
    if not our_order:
        problems.append("internal payment has no order_id yet")
    elif fetched.get("order_id") != our_order:
        problems.append("gateway order_id %r does not match internal %r"
                        % (fetched.get("order_id"), our_order))
    return problems


def _record(payment, *, source, fetched, order_id, payment_id, event_id,
            signature_ok, refund_id=None):
    """Append one immutable reconciliation record, or return the existing one.

    The business key is the thing that makes this converge: every verification
    of the same capture -- first callback, duplicate callback, webhook, webhook
    retry -- produces the same key, so the unique index keeps exactly one row.
    """
    db = get_db()
    split = commission_for_payment(payment)
    key = "RECON:%s:%s" % (source, payment_id or order_id or "unknown")
    now = utcnow()

    doc = {
        "business_key": key,
        "booking_id": payment.get("booking_id"),
        "payment_id": payment.get("_id"),
        # The four identifiers, so a dispute can be traced end to end without
        # guessing which gateway object belongs to which internal row.
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "webhook_event_id": event_id,
        "verification_source": source,
        "verified_at": now,
        "captured_amount": round(float(fetched.get("amount", 0)) / 100, 2)
        if isinstance(fetched, dict) and fetched.get("amount") is not None else None,
        "currency": (fetched.get("currency") if isinstance(fetched, dict) else None)
        or payment.get("currency") or CURRENCY,
        "signature_verified": bool(signature_ok),
        "refund_id": refund_id,
        # The frozen split, copied in so the record stands alone if the payment
        # document is ever edited.
        "gross": split["gross"],
        "platform_fee": split["platform_fee"],
        "driver_net": split["driver_net"],
        "commission_rate_percent": split["commission_rate_percent"],
        "created_at": now,
    }

    existing = db.payment_reconciliations.find_one({"business_key": key})
    if existing is not None:
        return existing, False
    try:
        db.payment_reconciliations.insert_one(doc)
        return doc, True
    except DuplicateKeyError:
        # A concurrent verification of the same capture won the insert.
        return db.payment_reconciliations.find_one({"business_key": key}), False


def reconcile_razorpay_callback(db, payment, payload, *, client, secret):
    """Verify a browser callback against Razorpay and return the capture.

    Raises 4xx on any mismatch; the caller must not mark the booking paid on a
    return value other than this function's success.
    """
    fields = _extract_callback_fields(payload or {})
    payment_id = fields["razorpay_payment_id"]
    order_id = fields["razorpay_order_id"]
    signature = fields["razorpay_signature"]

    if not payment_id or not order_id or not signature:
        raise APIError("Missing payment verification details.", 422,
                       code="payment_verify_invalid",
                       details={"expected": list(ALLOWED_CALLBACK_FIELDS)})

    if not order_id or order_id != payment.get("order_id"):
        raise APIError("Payment order mismatch.", 422, code="payment_verify_invalid")

    if not verify_checkout_signature(order_id, payment_id, signature, secret):
        raise APIError("Payment signature verification failed.", 402,
                       code="payment_verify_failed")

    try:
        fetched = client.payment.fetch(payment_id)
    except Exception as exc:  # noqa: BLE001 - gateway failure is not a client error
        raise APIError("Could not confirm payment with the gateway.", 502,
                       code="payment_verify_failed",
                       details={"reason": type(exc).__name__}) from None

    problems = _check_against_payment(fetched, payment)
    if problems:
        _record(payment, source=SOURCE_BROWSER_CALLBACK, fetched=fetched,
                order_id=order_id, payment_id=payment_id, event_id=None,
                signature_ok=True)
        raise APIError("Payment could not be reconciled with the gateway.", 402,
                       code="payment_verify_failed",
                       details={"reasons": problems})

    record, _fresh = _record(payment, source=SOURCE_BROWSER_CALLBACK,
                             fetched=fetched, order_id=order_id,
                             payment_id=payment_id, event_id=None,
                             signature_ok=True)
    return {
        "reference": payment_id,
        "order_id": order_id,
        "reconciliation": record,
        "amount": round(float(fetched["amount"]) / 100, 2),
        "currency": fetched.get("currency"),
    }


def reconcile_razorpay_webhook(db, payment, entity, *, client, event_id):
    """Verify a `payment.captured` webhook against Razorpay and our record.

    The webhook payload is treated as a *hint* only. It names a payment id, but
    the amount, currency, status and order relationship are re-fetched from the
    API, because a webhook body is just as forgeable as a browser body until the
    signature check above it has passed -- and a captured amount that arrived
    only in the payload would be an amount nobody verified.
    """
    payment_id = entity.get("id")
    if not payment_id:
        raise APIError("Webhook is missing the payment id.", 422,
                       code="webhook_invalid")

    try:
        fetched = client.payment.fetch(payment_id)
    except Exception as exc:  # noqa: BLE001
        raise APIError("Could not confirm payment with the gateway.", 502,
                       code="webhook_gateway_unavailable",
                       details={"reason": type(exc).__name__}) from None

    # If the gateway names an order, it must be ours. Checked against the
    # fetched object as well, so a mismatch inside the payload cannot slip past.
    payload_order = entity.get("order_id")
    our_order = payment.get("order_id")
    if payload_order and our_order and payload_order != our_order:
        record, _ = _record(payment, source=SOURCE_WEBHOOK, fetched=fetched,
                            order_id=payload_order, payment_id=payment_id,
                            event_id=event_id, signature_ok=True)
        return record, "order_mismatch"

    problems = _check_against_payment(fetched, payment)
    record, _ = _record(payment, source=SOURCE_WEBHOOK, fetched=fetched,
                        order_id=our_order, payment_id=payment_id,
                        event_id=event_id, signature_ok=True)
    if problems:
        return record, "mismatch"
    return record, "captured"


def attach_refund_to_reconciliation(payment, refund_id):
    """Link a refund back to the reconciliation record that authorised it.

    Set once. A later duplicate refund for the same capture must not rewrite
    which refund the capture was resolved by.
    """
    db = get_db()
    db.payment_reconciliations.update_many(
        {"payment_id": payment.get("_id"), "refund_id": None},
        {"$set": {"refund_id": str(refund_id) if refund_id else None,
                  "refund_linked_at": utcnow()}})
