"""Financial ledger: immutable money movement records.

The ledger is the source of truth for money. `ride.earnings` and similar
mutable counters are convenience caches only.

Ledger entry types:
    PASSENGER_PAYMENT       +gross booking amount       (booking paid)
    PLATFORM_FEE            -platform fee               (commission)
    DRIVER_PAYABLE          +net to driver              (after commission)
    PASSENGER_REFUND        -gross booking amount       (cancellation refund)
    PLATFORM_FEE_REVERSAL   +platform fee               (void commission)
    DRIVER_PAYABLE_REVERSAL -net from driver            (void driver payables)
    DRIVER_PAYOUT           -payout to driver

A full payment then full refund nets every account to zero.
"""

import uuid

from pymongo.errors import DuplicateKeyError

from .db import get_db, utcnow
from .errors import APIError

# The service prices, collects, refunds and pays in exactly one currency.
#
# Every amount in the ledger, on a payment, or in a payout is in this unit, and
# nothing in the money path is allowed to add the amounts of two different ones.
# A mixed-currency sum is not a rounding error: it is a wrong number that looks
# like a right one, so the currency is checked where a value enters the system
# rather than trusted from whatever the caller passed.
CURRENCY = "INR"

# Currencies the service would have to explicitly support. Kept as a set so
# adding one is a deliberate edit here, and so the rejection message can say
# what is supported instead of just refusing.
SUPPORTED_CURRENCIES = frozenset({"INR"})


def assert_currency(currency, *, field="currency"):
    """Reject anything the service cannot correctly account for.

    A missing value is accepted as the default, because the overwhelming
    majority of rows predate the field and backfilling them is not worth a
    migration. A value that is present and wrong is refused.
    """
    if currency is None or currency == "":
        return CURRENCY
    value = str(currency).strip().upper()
    if value not in SUPPORTED_CURRENCIES:
        raise APIError(
            "Unsupported currency %r; this service accounts in %s."
            % (currency, ", ".join(sorted(SUPPORTED_CURRENCIES))),
            422, code="unsupported_currency",
            details={"field": field, "currency": value,
                     "supported": sorted(SUPPORTED_CURRENCIES)})
    return value


def _entry_id():
    return "ENT-" + uuid.uuid4().hex


# The business key namespace. Every financial effect is named here, so the set
# of things that can be posted exactly once is a short, reviewable list rather
# than "whatever the caller remembered to guard".
_BK = {
    "payment_capture": "PAYMENT_CAPTURE",      # the passenger's gross
    "platform_fee": "PLATFORM_FEE",            # our cut of one capture
    "driver_payable": "DRIVER_PAYABLE",        # what one capture owes a driver
    "refund_gross": "REFUND_GROSS",            # money back to the passenger
    "refund_fee": "REFUND_FEE_REVERSAL",       # our cut given back
    "refund_payable": "REFUND_PAYABLE_REVERSAL",  # driver's claim given back
    "payout": "DRIVER_PAYOUT",                 # money out to a driver
}


def business_key(kind, *parts):
    """A deterministic, unique name for one financial effect.

    The point is that the same effect always produces the same string, so a
    retry -- a duplicated webhook, a re-run worker, a double-clicked button --
    collides on a unique index instead of silently double-posting. Guarding this
    in application code is not enough, because the guards themselves race; this
    makes the database the arbiter.

    Returns None when a part is missing rather than building a key out of
    ``None``: a key that is only accidentally unique is worse than no key,
    because it looks like protection.
    """
    prefix = _BK.get(kind)
    if not prefix:
        raise ValueError("unknown ledger business key %r" % (kind,))
    for part in parts:
        if part is None or str(part) == "":
            return None
    return ":".join([prefix] + [str(p) for p in parts])


def post_entry(*, account_id, account_type, entry_type, amount, booking_id=None,
               payment_id=None, refund_id=None, reference=None, currency=CURRENCY,
               business_key=None, meta=None):
    """Append an immutable ledger entry. `amount` is signed (credit +, debit -).

    The currency is validated on the way in: a ledger that holds two units
    cannot be summed, and the sum is what settlement and reconciliation read.

    `business_key` makes the append idempotent at the database level. When one is
    supplied and an entry with that key already exists, nothing is written and
    the existing entry is returned with `posted=False` -- so a caller can retry
    freely without double-counting, and can still tell whether this call was the
    one that actually moved the books.
    """
    currency = assert_currency(currency)
    db = get_db()
    doc = {
        "entry_id": _entry_id(),
        "account_id": str(account_id),
        "account_type": account_type,
        "entry_type": entry_type,
        "amount": round(float(amount), 2),
        "currency": currency,
        "booking_id": str(booking_id) if booking_id else None,
        "payment_id": str(payment_id) if payment_id else None,
        "refund_id": str(refund_id) if refund_id else None,
        "reference": reference or None,
        "business_key": business_key or None,
        "meta": meta or {},
        "created_at": utcnow(),
    }
    try:
        db.ledger_entries.insert_one(doc)
        doc["posted"] = True
        return doc
    except DuplicateKeyError:
        if not business_key:
            # The only unique index on this collection is on `entry_id`, which
            # is a fresh uuid, so this should be unreachable. Surfacing it is
            # better than silently swallowing a write failure.
            raise
        existing = db.ledger_entries.find_one({"business_key": business_key})
        if existing is None:
            raise
        existing["posted"] = False
        return existing


def fee_config():
    """Active commission config: (percent, minimum floor)."""
    from flask import current_app, has_app_context

    if has_app_context():
        return (current_app.config.get("PLATFORM_FEE_PERCENT", 30.0),
                current_app.config.get("MIN_PLATFORM_FEE", 0.0))
    from .config import Config

    return Config.PLATFORM_FEE_PERCENT, Config.MIN_PLATFORM_FEE


def platform_fee(gross_amount):
    """Commission for a gross INR amount. Server-side rule (never client).

    Reads from the active app config when a request context exists, falling
    back to the static Config (used by CLI scripts / tests without context).
    """
    pct, minimum = fee_config()
    fee = round(float(gross_amount) * pct / 100.0, 2)
    fee = max(fee, minimum)
    if fee >= gross_amount:
        fee = max(0.0, float(gross_amount))
    return round(fee, 2)


def quote_commission(gross_amount):
    """Freeze the money split for a gross amount at payment-creation time.

    Returns the exact triple stored on the payment document:
        gross  = what the passenger is charged
        fee    = platform commission
        net    = what the driver is owed (70% of gross)

    Persisting these three numbers (plus the rate that produced them) makes
    the split immutable: settlement, refund reversal, driver statements and
    reconciliation all read these stored values and never recompute from
    today's config, so a later commission change cannot rewrite history.
    """
    gross = round(float(gross_amount), 2)
    pct, _minimum = fee_config()
    fee = platform_fee(gross)
    net = round(gross - fee, 2)
    return {
        "gross": gross,
        "platform_fee": fee,
        "driver_net": net,
        "commission_rate_percent": float(pct),
        "currency": CURRENCY,
    }


def commission_for_payment(payment, gross=None):
    """The frozen commission split for a payment, with a legacy fallback.

    Payments created before the freeze shipped have no stored split; those are
    recomputed once from the current config purely for display, and the
    resulting `frozen` flag tells callers the value is not authoritative.
    """
    if payment.get("platform_fee") is not None and payment.get("driver_net") is not None:
        fee = round(float(payment["platform_fee"]), 2)
        net = round(float(payment["driver_net"]), 2)
        return {
            "gross": round(float(payment.get("amount", gross or 0.0)), 2),
            "platform_fee": fee,
            "driver_net": net,
            "commission_rate_percent": float(
                payment.get("commission_rate_percent", fee_config()[0])),
            "currency": payment.get("currency", "INR"),
            "frozen": True,
        }
    quote = quote_commission(gross if gross is not None else payment.get("amount", 0))
    quote["frozen"] = False
    return quote


def record_payment_ledger(booking, payment):
    """Post PASSENGER_PAYMENT / PLATFORM_FEE / DRIVER_PAYABLE entries.

    The split comes from the values frozen on the payment at order creation, so
    the ledger always satisfies gross == fee + net for that exact transaction
    even if the platform commission changed afterwards.

    Each of the three entries carries a business key derived from the payment
    id, so calling this again for the same capture posts nothing. That matters
    because both the browser callback and the gateway webhook can settle the
    same payment, and in either order.
    """
    split = commission_for_payment(payment)
    gross = split["gross"]
    fee = split["platform_fee"]
    net = split["driver_net"]
    meta = {
        "gross": gross,
        "platform_fee": fee,
        "driver_net": net,
        "commission_rate_percent": split["commission_rate_percent"],
    }
    pid = payment.get("_id")
    post_entry(account_id="PLATFORM", account_type="platform", entry_type="PLATFORM_FEE",
               amount=-fee, booking_id=booking["_id"], payment_id=pid,
               reference=payment.get("reference"), meta=dict(meta),
               business_key=business_key("platform_fee", pid))
    post_entry(account_id=booking["owner_id"], account_type="driver",
               entry_type="DRIVER_PAYABLE", amount=net,
               booking_id=booking["_id"], payment_id=pid,
               reference=payment.get("reference"), meta=dict(meta),
               business_key=business_key("driver_payable", pid))
    post_entry(account_id=booking["rider_id"], account_type="passenger",
               entry_type="PASSENGER_PAYMENT", amount=gross,
               booking_id=booking["_id"], payment_id=pid,
               reference=payment.get("reference"), meta=dict(meta),
               business_key=business_key("payment_capture", pid))


def _original_payment_fee(booking_id):
    """Fee actually recorded for this booking at payment time (entry readback),
    so refund reversals never depend on today's commission config."""
    rows = get_db().ledger_entries.find({"booking_id": str(booking_id), "entry_type": "PLATFORM_FEE"})
    taken = [abs(float(r.get("amount", 0.0))) for r in rows]
    return sum(taken)


def _original_payment_gross(booking_id):
    rows = get_db().ledger_entries.find({"booking_id": str(booking_id), "entry_type": "PASSENGER_PAYMENT"})
    return sum(float(r.get("amount", 0.0)) for r in rows)


def record_refund_ledger(booking, refund):
    """Reverse the original payment entries for a refund.

    Reversal amounts are proportional to the refunded fraction of the original
    payment (partial refunds reverse only their share of the recorded fee), so
    a full refund nets the passenger/driver/platform accounts back to zero.

    If the original payment was never ledgered (money captured for a booking
    that was cancelled before settlement), there is nothing to reverse: the
    refund is gateway-only and the ledger stays untouched.
    """
    gross = float(refund["amount"])
    orig_gross = _original_payment_gross(booking["_id"])
    if orig_gross and orig_gross > 0:
        ratio = round(gross / orig_gross, 4)
    else:
        ratio = 1.0 if gross == float(booking.get("amount") or 0) else round(gross / float(booking.get("amount") or gross), 4)
    taken_fee = _original_payment_fee(booking["_id"])
    if not taken_fee and not orig_gross:
        return  # nothing was ledgered for this payment -> nothing to reverse
    fee = round(taken_fee * ratio, 2)
    net = round(gross - fee, 2)
    rid = refund.get("_id")
    post_entry(account_id="PLATFORM", account_type="platform",
               entry_type="PLATFORM_FEE_REVERSAL", amount=fee,
               booking_id=booking["_id"], payment_id=refund.get("payment_id"),
               refund_id=rid, reference=refund.get("reference"),
               business_key=business_key("refund_fee", rid))
    post_entry(account_id=booking["owner_id"], account_type="driver",
               entry_type="DRIVER_PAYABLE_REVERSAL", amount=-net,
               booking_id=booking["_id"], payment_id=refund.get("payment_id"),
               refund_id=rid, reference=refund.get("reference"),
               business_key=business_key("refund_payable", rid))
    post_entry(account_id=booking["rider_id"], account_type="passenger",
               entry_type="PASSENGER_REFUND", amount=-gross,
               booking_id=booking["_id"], payment_id=refund.get("payment_id"),
               refund_id=rid, reference=refund.get("reference"),
               business_key=business_key("refund_gross", rid))


def account_balance(account_id, account_type):
    rows = get_db().ledger_entries.find({"account_id": str(account_id), "account_type": account_type})
    return round(sum(r.get("amount", 0.0) for r in rows), 2)


def driver_net_earnings(user_id):
    """DRIVER_PAYABLE (gross earned) - reversals - payouts."""
    return account_balance(user_id, "driver")


def driver_payable(user_id):
    """Cumulative net payables (before/after payouts, no payouts subtracted)."""
    rows = get_db().ledger_entries.find({"account_id": str(user_id), "account_type": "driver"})
    return round(sum(r.get("amount", 0.0) for r in rows), 2)


# Payout CREATION lives in payouts.py, not here. That module owns the payout
# state machine and the rule that only a provider confirmation may mark money
# as paid; keeping it out of the ledger module stops the two concerns from
# drifting apart.