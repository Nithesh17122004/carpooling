"""Payments: order creation, server-side verification, webhooks, refunds.

Layered on a provider abstraction:
  - "demo"  : development-only simulated gateway. NEW ORDERS AND VERIFY CALLS
              ARE REJECTED IN PRODUCTION. It exists so the full booking flow
              (order -> verify -> ledger) can be exercised locally.
  - "razorpay": real gateway. Signature + order state are verified server-side;
              the client is never trusted with amounts or status.

Every mutation is idempotent: an order/payment/refund is keyed by
`booking_id` + `idempotency_key` with unique indexes.
"""

import hashlib
import hmac
import uuid

from flask import current_app, request
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from . import db as dbmodule
from .db import get_db, utcnow
from .errors import APIError

PROVIDERS = ("demo", "razorpay")


def _provider():
    return getattr(current_app, "config", {}).get("PAYMENT_PROVIDER", "demo")


def _demo_allowed():
    if _provider() == "razorpay":
        return False
    return current_app.config.get("ENV", "development") != "production"


def _razorpay_client():
    from .config import Config

    if not Config.RAZORPAY_KEY_ID or not Config.RAZORPAY_KEY_SECRET:
        return None
    try:
        import razorpay

        return razorpay.Client(auth=(Config.RAZORPAY_KEY_ID, Config.RAZORPAY_KEY_SECRET))
    except ImportError:
        return None


def _idp_key():
    """Idempotency key covering a single money mutation for a booking."""
    return uuid.uuid4().hex


# ------------------------------------------------------------------ orders
def create_order(booking, seats, amount):
    """Create a payment order for an unconfirmed booking. Idempotent per booking.

    The money split (gross / 30% platform fee / driver net) and the rate that
    produced it are computed once here and frozen onto the payment document.
    Settlement, refund reversal and reporting all read those stored values, so
    a later commission change can never rewrite this transaction. The amount is
    always derived server-side from the booking; no client value is trusted.

    Ordering is deliberate: the booking is CLAIMED in the database before the
    gateway is called. Two concurrent requests for the same booking therefore
    cannot both create an order at Razorpay -- the loser either reuses the
    winner's finished order or is told to retry. Calling the gateway first and
    inserting afterwards (the previous shape) let both requests reach Razorpay
    and left an orphan live order that nobody would ever pay or cancel.
    """
    from .ledger import quote_commission

    # Configuration is checked before any gateway call, never after.
    assert_checkout_ready()

    db = get_db()
    split = quote_commission(amount)
    now = utcnow()
    doc = {
        "booking_id": booking["_id"],
        "ride_id": booking.get("ride_id"),
        "rider_id": booking.get("rider_id"),
        "driver_id": booking.get("owner_id"),
        "order_id": None,
        "provider": _provider(),
        "provider_reference": None,
        "amount": split["gross"],
        "currency": split["currency"],
        "platform_fee": split["platform_fee"],
        "driver_net": split["driver_net"],
        "commission_rate_percent": split["commission_rate_percent"],
        "commission_frozen_at": now,
        "status": "creating",
        "creating_at": now,
        "idempotency_key": _idp_key(),
        "created_at": now,
        "updated_at": now,
    }

    # 1) Claim the booking. The unique booking_id index is the mutex -- there is
    # deliberately no read-then-write here, because a check-then-act gap is
    # exactly the race this function exists to close.
    try:
        claimed = db.payments.insert_one(doc)
        doc["_id"] = claimed.inserted_id
    except DuplicateKeyError:
        # 2) Someone else got there first.
        current = db.payments.find_one({"booking_id": booking["_id"]})
        if current is None:
            raise APIError("Payment could not be started. Try again.", 502,
                           code="payment_order_failed")
        if current.get("order_id"):
            return current                      # fully idempotent retry
        # A claim exists with no order behind it: either another request is
        # inside its gateway call right now, or the claim was abandoned. Adopt
        # it only if it is genuinely takeable, otherwise make the caller wait.
        if not _take_over_creating(db, current):
            raise APIError(
                "A payment for this booking is already being created. Try again "
                "in a moment.", 409, code="payment_order_in_progress")
        doc["_id"] = current["_id"]
        doc["updated_at"] = utcnow()

    # 3) Only the owner of the claim reaches the gateway.
    order_id = None
    if _provider() == "razorpay":
        client = _razorpay_client()
        if client is None:
            _abandon_order(db, doc["_id"])
            raise APIError("Payments are not configured on the server.", 503,
                           code="payments_unconfigured")
        try:
            resp = client.order.create({
                "amount": int(round(float(amount) * 100)),
                "currency": "INR",
                "receipt": "ride_" + str(booking["ride_id"]),
                "notes": {"booking_id": str(booking["_id"])},
            })
            order_id = resp.get("id")
        except Exception as exc:  # noqa: BLE001 - gateway errors surface cleanly
            _abandon_order(db, doc["_id"])
            raise APIError("Payment could not be started. Try again.", 502,
                           code="payment_order_failed",
                           details={"reason": type(exc).__name__})
    else:
        if not _demo_allowed():
            _abandon_order(db, doc["_id"])
            raise APIError("Simulated payments are disabled in production.", 503,
                           code="demo_payments_disabled")
        order_id = "demo_order_" + uuid.uuid4().hex[:12]

    # 4) Publish the gateway order onto the claimed row.
    fresh = db.payments.find_one_and_update(
        {"_id": doc["_id"], "status": "creating"},
        {"$set": {"order_id": order_id, "provider_reference": order_id,
                  "status": "created", "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER)
    return fresh or db.payments.find_one({"_id": doc["_id"]})


# A claim older than this is assumed abandoned (process killed mid-gateway-call)
# and may be taken over, so a crash cannot leave a booking permanently unpayable.
_ORDER_CLAIM_TTL_SECONDS = 60


def _take_over_creating(db, current):
    """Try to adopt an in-flight `creating` row whose owner never finished.

    Only a row that has already gone stale (or already failed) can be adopted;
    an in-progress claim is left alone so two live requests never both call the
    gateway.
    """
    from datetime import timedelta

    cutoff = utcnow() - timedelta(seconds=_ORDER_CLAIM_TTL_SECONDS)
    adopted = db.payments.find_one_and_update(
        {"_id": current["_id"], "order_id": None,
         "$or": [{"status": "failed"}, {"creating_at": {"$lte": cutoff}},
                 {"creating_at": {"$exists": False}}]},
        {"$set": {"status": "creating", "creating_at": utcnow(),
                  "updated_at": utcnow()}})
    return adopted is not None


def _abandon_order(db, payment_id):
    """Release a claim whose gateway call never happened, so the client can
    retry immediately instead of waiting out the TTL."""
    db.payments.update_one(
        {"_id": payment_id, "order_id": None},
        {"$set": {"status": "failed", "updated_at": utcnow()},
         "$unset": {"creating_at": ""}})


def assert_checkout_ready():
    """Fail before any money-moving side effect if the gateway is unusable.

    Called at the top of `create_order` so a half-configured deployment never
    creates a real order it cannot hand to the browser -- and therefore never
    leaves an orphaned order at the gateway or a payment row for a booking that
    is about to be rolled back.
    """
    provider = _provider()
    if provider == "razorpay":
        if not current_app.config.get("RAZORPAY_KEY_ID", ""):
            raise APIError("Payments are not configured on the server.", 503,
                           code="payments_unconfigured")
        if _razorpay_client() is None:
            raise APIError("Payments are not configured on the server.", 503,
                           code="payments_unconfigured")
        return
    if not _demo_allowed():
        raise APIError("Simulated payments are disabled in production.", 503,
                       code="demo_payments_disabled")


def checkout_config(payment, user=None):
    """Everything the browser needs to open a gateway checkout, and nothing more.

    The Razorpay KEY_ID is a publishable identifier (it identifies the app to
    the gateway, it cannot move money) and is required by the Checkout script.
    The KEY_SECRET is never included. The amount handed to the gateway is the
    server-computed gross taken from the payment document, so the browser can
    never influence what is charged -- and the split is echoed back only so the
    UI can show the rider an honest breakdown.
    """
    from .ledger import commission_for_payment

    split = commission_for_payment(payment)
    cfg = {
        "provider": payment.get("provider", "demo"),
        "order_id": payment.get("order_id"),
        "amount": split["gross"],
        "currency": payment.get("currency", "INR"),
        "booking_id": str(payment.get("booking_id")),
        "breakdown": {
            "gross": split["gross"],
            "platform_fee": split["platform_fee"],
            "driver_payout": split["driver_net"],
            "commission_rate_percent": split["commission_rate_percent"],
        },
    }
    if payment.get("provider") == "razorpay":
        cfg["key_id"] = current_app.config.get("RAZORPAY_KEY_ID", "") or None
        if not cfg["key_id"]:
            raise APIError("Payments are not configured on the server.", 503,
                           code="payments_unconfigured")
        if user:
            cfg["prefill"] = {
                "name": user.get("name") or "",
                "email": user.get("email") or "",
                "contact": user.get("phone") or "",
            }
    return cfg


# ----------------------------------------------------------------- verify
def verify_payment(booking, payment, payload):
    """Server-side finish: mark payment success + confirm booking + ledger.

    Ignores any client-supplied status/amount; derives truth from the gateway.
    Idempotent: a confirmed booking returns its existing payment.
    """
    db = get_db()
    if payment.get("status") == "success":
        return payment

    reference = None
    if payment.get("provider") == "razorpay":
        client = _razorpay_client()
        signature = (payload.get("razorpay_signature") or "").strip()
        razorpay_id = (payload.get("razorpay_payment_id") or "").strip()
        order_id = (payload.get("razorpay_order_id") or "").strip()
        if not signature or not razorpay_id or not order_id:
            raise APIError("Missing payment verification details.", 422, code="payment_verify_invalid")
        if order_id != payment.get("order_id"):
            raise APIError("Payment order mismatch.", 422, code="payment_verify_invalid")
        secret = current_app.config.get("RAZORPAY_KEY_SECRET", "")
        expected = hmac.new(secret.encode(), f"{order_id}|{razorpay_id}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise APIError("Payment signature verification failed.", 402, code="payment_verify_failed")
        try:
            fetched = client.payment.fetch(razorpay_id)
        except Exception as exc:  # noqa: BLE001
            raise APIError("Could not confirm payment with the gateway.", 502, code="payment_verify_failed",
                           details={"reason": type(exc).__name__})
        if fetched.get("status") != "captured" or float(fetched.get("amount", 0)) / 100 != float(payment["amount"]):
            raise APIError("Payment was not captured for the correct amount.", 402, code="payment_verify_failed")
        reference = razorpay_id
    else:
        if not _demo_allowed():
            raise APIError("Simulated payments are disabled in production.", 503, code="demo_payments_disabled")
        reference = "demo_pay_" + uuid.uuid4().hex[:12]

    db.payments.update_one(
        {"_id": payment["_id"], "status": {"$ne": "success"}},
        {"$set": {"status": "success", "reference": reference, "provider_reference": reference,
                  "paid_at": utcnow(), "updated_at": utcnow()}},
    )
    return db.payments.find_one({"_id": payment["_id"]})


# ----------------------------------------------------------------- webhook
def _dedup_event(provider, event_id):
    """Register a webhook event for at-least-once delivery guarantees.

    Returns the stored doc for a NEW event, or the previous doc for a duplicate
    redelivery. Every Razorpay delivery carries a stable `event_id`, so a retry
    of the exact same event is answered with ALREADY PROCESSED instead of being
    settled twice. Events without an id are deduplicated by the atomic payment
    claim in `_settle_webhook` instead.
    """
    db = get_db()
    if not event_id:
        return None
    doc = {
        "provider": provider,
        "event_id": event_id,
        "count_received": 1,
        "first_received_at": utcnow(),
        "last_received_at": utcnow(),
    }
    try:
        result = db.webhook_events.insert_one(doc)
        doc["_id"] = result.inserted_id
        return doc
    except DuplicateKeyError:
        db.webhook_events.find_one_and_update(
            {"provider": provider, "event_id": event_id},
            {"$inc": {"count_received": 1}, "$set": {"last_received_at": utcnow()}})
        return db.webhook_events.find_one({"provider": provider, "event_id": event_id})


def handle_webhook():
    """Razorpay webhook (or demo-simulated success). Idempotent settle route.

    At-least-once delivery: the gateway may redeliver the same event. Event
    ids are recorded exactly once; a duplicate delivery is acknowledged with
    `duplicate: True` and never settles a payment twice. Events without an id
    fall back to the atomic payment claim in `_settle_webhook`.
    """
    provider = _provider()
    db = get_db()
    if provider == "razorpay":
        secret = current_app.config.get("RAZORPAY_WEBHOOK_SECRET", "")
        if not secret:
            raise APIError("Payments webhook not configured.", 503, code="payments_unconfigured")
        body = request.get_data(as_text=True)
        signature = request.headers.get("X-Razorpay-Signature", "")
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise APIError("Invalid webhook signature.", 400, code="webhook_invalid")
        event = request.get_json(silent=True) or {}
        entity = event.get("payload", {}).get("payment", {}).get("entity", {}) or {}
        reference = entity.get("id")
        amount = float(entity.get("amount", 0)) / 100
        event_id = (event.get("event_id") or "").strip()
        dup = _dedup_event("razorpay", event_id) if event_id else None
        if dup and dup.get("count_received", 1) > 1:
            return {"ok": True, "received": True, "duplicate": True,
                    "first_processed_at": dup.get("first_received_at") is not None}
        if event.get("event") == "payment.captured":
            _settle_webhook(reference, amount)
            return {"ok": True, "received": True, "duplicate": False}
        return {"ok": True, "received": True, "ignored": event.get("event"), "duplicate": False}

    # demo webhook (dev only, no gateway, no signatures)
    if not _demo_allowed():
        raise APIError("Simulated payments are disabled in production.", 503, code="demo_payments_disabled")
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id") or data.get("provider_reference")
    if order_id:
        payment = db.payments.find_one({"order_id": order_id})
        if payment:
            _dedup_event("demo", f"demo:{order_id}")
            _settle_webhook(payment.get("reference") or order_id, payment.get("amount"))
    return {"ok": True, "received": True}


def _settle_webhook(reference, amount):
    """Confirm the booking + post ledger exactly once.

    Concurrency-safe against duplicate deliveries (two webhook POSTs for the
    same capture) and against a concurrent rider cancellation. The booking
    transition is conditional (pending_payment -> confirmed): it can never
    overwrite an in-flight cancellation. Money that arrives for a booking that
    was cancelled (or a ride that became unusable) is refunded automatically,
    and refunds are themselves claimed atomically so the gateway is never
    asked twice.
    """
    from .ledger import record_payment_ledger
    from .states import BOOKING_CONFIRMED, BOOKING_PENDING_PAYMENT, BOOKING_REFUNDED
    from . import notifications

    db = get_db()
    payment = db.payments.find_one({"provider_reference": reference})
    if not payment or payment.get("status") == "success":
        return
    booking = db.bookings.find_one({"_id": payment["booking_id"]})
    if not booking or float(payment.get("amount", 0)) != amount:
        return

    # Atomic claim: exactly one concurrent delivery flips created -> processing.
    claimed = db.payments.find_one_and_update(
        {"_id": payment["_id"], "status": {"$in": ["created", "processing"]}},
        {"$set": {"status": "processing", "updated_at": utcnow()}})
    if claimed is None:
        return  # another delivery is (or already did) settle this capture

    # Money arrived, but did the rider cancel while the capture was in flight?
    fresh = db.bookings.find_one({"_id": booking["_id"]})
    if fresh.get("status") != BOOKING_PENDING_PAYMENT:
        if fresh.get("status") == BOOKING_CONFIRMED:
            return  # a concurrent settle already confirmed; it posts the ledger
        # cancelled / refunded / closed before the capture landed -> return money
        refund_payment(payment, "Booking was cancelled before the payment settled")
        return

    ride = db.rides.find_one({"_id": booking["ride_id"]})
    if ride and ride.get("status") in ("cancelled", "expired", "completed"):
        # Payment captured for a ride that can no longer run -> full refund.
        refund_payment(payment, "Ride unavailable at payment time")
        db.bookings.update_one(
            {"_id": booking["_id"]},
            {"$set": {"status": BOOKING_REFUNDED, "refunded": True,
                      "refund_percentage": 100, "refund_status": "PROCESSED",
                      "updated_at": utcnow()}})
        return

    # Conditional booking transition: never overwrites an in-flight cancel that
    # landed between the read above and this update.
    res = db.bookings.update_one(
        {"_id": booking["_id"], "status": BOOKING_PENDING_PAYMENT},
        {"$set": {"status": BOOKING_CONFIRMED, "updated_at": utcnow()}})
    if res.modified_count == 0:
        later = db.bookings.find_one({"_id": booking["_id"]})
        if later and later.get("status") == BOOKING_CONFIRMED:
            return  # a concurrent delivery won the transition and posts ledger
        refund_payment(payment, "Booking was cancelled before the payment settled")
        return

    db.payments.update_one({"_id": payment["_id"]},
                           {"$set": {"status": "success", "paid_at": utcnow(),
                                     "updated_at": utcnow()}})
    updated = db.bookings.find_one({"_id": booking["_id"]})
    record_payment_ledger(updated, payment)
    notifications.notify(
        booking.get("rider_id"), "Booking confirmed",
        f"Your {updated.get('seats', 1)} seat(s) on {ride.get('origin', {}).get('label')} → "
        f"{ride.get('destination', {}).get('label')} are confirmed.")


def handle_payout_webhook():
    """RazorpayX payout events. The ONLY way a payout becomes `paid`.

    Signed with the same webhook secret as payment events, and deduplicated by
    event id so a retried delivery is acknowledged without re-applying it.
    Confirming here is what makes "paid" trustworthy: the state changes because
    the provider said the money moved, not because anyone asserted it did.
    """
    from .payouts import PAYOUT_PAID, confirm_payout, fail_payout

    provider = _provider()
    db = get_db()
    if provider != "razorpay":
        raise APIError("Payout webhooks require the razorpay provider.", 400,
                       code="payouts_not_configured")

    secret = current_app.config.get("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        raise APIError("Payments webhook not configured.", 503, code="payments_unconfigured")
    body = request.get_data(as_text=True)
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise APIError("Invalid webhook signature.", 400, code="webhook_invalid")

    event = request.get_json(silent=True) or {}
    event_id = (event.get("event_id") or "").strip()
    dup = _dedup_event("razorpay_payout", event_id) if event_id else None
    if dup and dup.get("count_received", 1) > 1:
        return {"ok": True, "received": True, "duplicate": True}

    entity = event.get("payload", {}).get("payout", {}).get("entity", {}) or {}
    provider_ref = entity.get("id")
    our_ref = entity.get("reference_id") or (entity.get("notes") or {}).get("reference_id")
    if not provider_ref and not our_ref:
        return {"ok": True, "received": True, "ignored": event.get("event")}

    payout = None
    if provider_ref:
        payout = db.payouts.find_one({"provider_reference": provider_ref})
    if payout is None and our_ref:
        payout = db.payouts.find_one({"reference": our_ref})
    if payout is None:
        return {"ok": True, "received": True, "unmatched": True}

    name = event.get("event") or ""
    if name in ("payout.processed", "payout.settled", "payout.reversed"):
        if entity.get("status") == "failed" or name == "payout.reversed":
            fail_payout(payout["_id"], entity.get("failure_reason") or name, source="provider")
        else:
            confirm_payout(payout["_id"], provider_ref, source="provider")
    elif name == "payout.failed":
        fail_payout(payout["_id"], entity.get("failure_reason") or "payout failed",
                    source="provider")
    else:
        return {"ok": True, "received": True, "ignored": name}

    fresh = db.payouts.find_one({"_id": payout["_id"]})
    return {"ok": True, "received": True, "status": fresh.get("status")}


# ----------------------------------------------------------------- refunds
def refund_payment(payment, reason, amount=None):
    """Refund a successful payment. Idempotent per payment. Returns refund doc.

    Concurrency-safe claim: the refunds row is inserted with status
    "processing" first (unique per payment while it exists), so concurrent
    callers cannot both reach the gateway. A DuplicateKeyError means another
    claim won and its refund doc is returned. Ledger reversals are posted once,
    after the provider call, and only when the original payment was ledgered.

    A 0%/negative amount is a hard error -- never silently falls back to a full
    refund of `payment.amount`.
    """
    from .ledger import record_refund_ledger

    db = get_db()
    if amount is None:
        amount = float(payment.get("amount", 0))
    else:
        amount = float(amount)
    if amount <= 0:
        raise APIError("Refund amount must be greater than zero.", 422,
                       code="invalid_refund_amount")
    booking = db.bookings.find_one({"_id": payment["booking_id"]})
    if not booking:
        raise APIError("Booking for this payment was not found.", 404, code="not_found")

    existing = db.refunds.find_one({"payment_id": payment["_id"], "status": {"$ne": "failed"}})
    if existing:
        return existing

    refund = {
        "payment_id": payment["_id"],
        "booking_id": booking["_id"],
        "ride_id": booking["ride_id"],
        "provider": payment.get("provider", "demo"),
        "amount": round(amount, 2),
        "currency": "INR",
        "reason": reason,
        "status": "processing",
        "idempotency_key": uuid.uuid4().hex,
        "provider_reference": None,
        "created_at": utcnow(),
    }
    try:
        result = db.refunds.insert_one(refund)
    except DuplicateKeyError:
        # a concurrent refund claim for the same payment won -> use its result
        winner = db.refunds.find_one({"payment_id": payment["_id"], "status": {"$ne": "failed"}})
        if winner:
            return winner
        raise
    refund["_id"] = result.inserted_id

    provider = payment.get("provider", "demo")
    reference = None
    if provider == "razorpay":
        client = _razorpay_client()
        try:
            resp = client.payment.refund(payment.get("provider_reference"), {
                "amount": int(round(amount * 100)),
                "notes": {"reason": reason},
            })
            reference = resp.get("id")
        except Exception as exc:  # noqa: BLE001
            db.refunds.update_one({"_id": refund["_id"]},
                                  {"$set": {"status": "failed", "updated_at": utcnow()}})
            raise APIError("Refund could not be processed.", 502, code="refund_failed",
                           details={"reason": type(exc).__name__})
    else:
        if not _demo_allowed():
            raise APIError("Simulated payments are disabled in production.", 503, code="demo_payments_disabled")
        reference = "demo_refund_" + uuid.uuid4().hex[:12]

    db.refunds.update_one({"_id": refund["_id"]}, {"$set": {
        "status": "processed",
        "provider_reference": reference,
        "reason": reason,
        "updated_at": utcnow(),
    }})
    refund["provider_reference"] = reference
    refund["status"] = "processed"
    record_refund_ledger(booking, refund)
    db.payments.update_one({"_id": payment["_id"]},
                           {"$set": {"status": "refunded", "updated_at": utcnow()}})
    return refund