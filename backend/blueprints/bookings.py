"""Booking endpoints: idempotent seat claims, payment flow, cancel/refund.

Flow
----
POST /api/bookings                         -> reserve seats, create payment order (pending_payment)
POST /api/bookings/<id>/verify            -> server-side verify -> confirmed + ledger
DELETE /api/bookings/<id>                 -> cancel per refund policy

Invariants
----------
- The ride seat claim (find_one_and_update with $gte guard) and the booking
  insert share a compensation: any insert/payment failure reverses the claim.
- Idempotency: a retried POST with the same idempotency_key returns the
  existing ACTIVE booking instead of a second reservation.
- A rider may hold at most one ACTIVE (pending_payment/payment_failed/
  confirmed) booking per ride; the partial-unique index enforces it even under
  concurrent POSTs. Rebooking after a cancellation/refund is allowed.
- Bookings stuck in pending_payment expire after PAYMENT_TTL_MINUTES and their
  seats are released.
- Cancellation is refused once the ride departs. Refund is 100% when the
  booking is cancelled more than CANCELLATION_CUTOFF_MINUTES before departure,
  else 50%. Refunds and ledger postings are provider-idempotent.
"""

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from flask import Blueprint, current_app, g, request

from ..db import get_db, to_object_id, utcnow
from ..errors import APIError
from .. import notifications
from ..payments import create_order, verify_payment, refund_payment, checkout_config
from ..ratelimit import rate_limit
from ..security import require_auth
from ..serializers import driver_snapshot, public_user
from ..states import (
    BOOKING_PENDING_PAYMENT,
    BOOKING_CONFIRMED,
    BOOKING_CANCELLED,
    BOOKING_REFUND_PENDING,
    BOOKING_REFUNDED,
    can_transition_booking,
)
from ..timeutil import is_past, iso_utc
from ..validators import as_int, as_str, body, require_fields

bp = Blueprint("bookings", __name__, url_prefix="/api/bookings")

_ACTIVE_BOOKING_DB = {BOOKING_PENDING_PAYMENT, BOOKING_CONFIRMED}
_REFUNDABLE = {BOOKING_CONFIRMED, BOOKING_REFUND_PENDING}
# States that hold seats AND satisfy the partial-unique (ride_id, rider_id)
# index — must match db._ACTIVE_BOOKING_STATES.
_SEAT_BOOKING_DB = (BOOKING_PENDING_PAYMENT, "payment_failed", BOOKING_CONFIRMED)


def _amount(fare, seats):
    return round(float(fare) * int(seats), 2)


def _refund_policy(ride, now=None):
    """Returns (refundable_percent, reason, cutoff_hours).

    100% when cancelled beyond the cutoff before departure; 50% inside the
    cutoff window; 0% once departed (cancellation then rejected upstream).
    """
    now = now or _now()
    cutoff_min = int(current_app.config.get("CANCELLATION_CUTOFF_MINUTES", 60) or 60)
    if ride is None or ride.get("departure_at") is None:
        return 100, "Full refund", 0
    if is_past(ride["departure_at"], now):
        return 0, "Ride already departed", 0
    minutes_left = (ride["departure_at"] - now).total_seconds() / 60
    if minutes_left > cutoff_min:
        return 100, f"More than {cutoff_min} minutes before departure", round(cutoff_min / 60, 1)
    return 50, f"Within {cutoff_min} minutes of departure", round(cutoff_min / 60, 1)


def _now():
    return utcnow()


def clean_booking(booking):
    b = dict(booking)
    out = {
        "id": str(b["_id"]),
        "ride_id": str(b["ride_id"]),
        "owner_id": str(b["owner_id"]),
        "seats": b["seats"],
        "amount": b["amount"],
        "fare_per_seat": b["fare_per_seat"],
        "status": b["status"],
        "refundable": b.get("refundable", False),
        "refund_amount": b.get("refund_amount"),
        "refund_percentage": b.get("refund_percentage"),
        "refund_status": b.get("refund_status"),
        "refunded": b.get("refunded", False),
        "payment": b.get("payment"),
        "ride": b.get("ride"),
        "driver": b.get("driver"),
        "passenger": b.get("passenger"),
        "ride_snapshot": b.get("ride_snapshot"),
        "created_at": iso_utc(b.get("created_at")),
        "updated_at": iso_utc(b.get("updated_at")),
        "cancelled_at": iso_utc(b.get("cancelled_at")),
        "details": b.get("details"),
    }
    return out


def _snapshot(ride):
    return {
        "origin": ride.get("origin"),
        "destination": ride.get("destination"),
        "departure_date": ride.get("departure_date"),
        "departure_time": ride.get("departure_time"),
        "timezone": ride.get("timezone", "Asia/Kolkata"),
        "departure_at": iso_utc(ride.get("departure_at")),
        "vehicle": ride.get("vehicle"),
    }


def _claim_seats(db, ride, seats, amount):
    """Atomically decrement seats; return updated ride or None."""
    now = _now()
    return db.rides.find_one_and_update(
        {"_id": ride["_id"],
         "status": {"$in": ["active", "published", "full"]},
         "departure_at": {"$gt": now},
         "seats_available": {"$gte": seats}},
        [
            {"$set": {
                "seats_available": {"$subtract": ["$seats_available", seats]},
                "earnings": {"$add": ["$earnings", amount]},
                "status": {"$cond": [{"$eq": ["$seats_available", seats]},
                                     "full", "$status"]},
                "updated_at": now,
            }},
        ],
        return_document=ReturnDocument.AFTER,
    )


def _release_seats(db, ride_id, seats, amount):
    db.rides.update_one(
        {"_id": ride_id},
        [{"$set": {
            "seats_available": {"$min": ["$seats_total",
                                          {"$add": ["$seats_available", seats]}]},
            "earnings": {"$max": [0, {"$subtract": ["$earnings", amount]}]},
            "status": {"$cond": [{"$eq": ["$seats_total", {"$add": ["$seats_available", seats]}]},
                                 "published", "$status"]},
            "updated_at": _now(),
        }}],
    )


def _expire_pending_payments():
    """Release seats for bookings abandoned before payment (browser close,
    timeout). A booking still in pending_payment after PAYMENT_TTL_MINUTES is
    cancelled and its seats returned to the pool. Lazy sweep on request paths.
    """
    from datetime import timedelta

    db = get_db()
    ttl_min = int(current_app.config.get("PAYMENT_TTL_MINUTES", 15) or 15)
    deadline = _now() - timedelta(minutes=ttl_min)
    stale = list(db.bookings.find({
        "status": {"$in": [BOOKING_PENDING_PAYMENT, "payment_failed"]},
        "updated_at": {"$lt": deadline},
    }))
    for b in stale:
        _release_seats(db, b["ride_id"], b["seats"], b["amount"])
        db.bookings.update_one(
            {"_id": b["_id"]},
            {"$set": {"status": BOOKING_CANCELLED, "cancelled_at": _now(),
                      "refund_amount": 0.0, "refunded": True,
                      "refund_percentage": 0, "refund_status": "NOT_REQUIRED",
                      "updated_at": _now(),
                      "details": "Payment not completed within the allowed time."}})


# ------------------------------------------------------------------- create
@bp.post("")
@require_auth
@rate_limit("strict")
def create_booking():
    db = get_db()
    _expire_pending_payments()
    data = body()
    require_fields(data, "ride_id", "seats")
    ride_id = to_object_id(data.get("ride_id"), "ride")
    seats = as_int(data.get("seats"), "seats", minimum=1,
                   maximum=current_app.config["SEATS_MAX_PER_BOOKING"], required=True)
    idem = as_str(data.get("idempotency_key"), "idempotency_key", max_len=64)

    ride = db.rides.find_one({"_id": ride_id})
    if not ride or ride.get("status") not in ("active", "published", "full"):
        raise APIError("Ride is no longer available.", 409, code="ride_unavailable")
    if str(ride["owner_id"]) == str(g.user["_id"]):
        raise APIError("You cannot book your own ride.", 400, code="own_ride")
    if is_past(ride.get("departure_at"), _now()):
        raise APIError("Ride has already departed.", 409, code="ride_departed")

    existing = db.bookings.find_one(
        {"ride_id": ride_id, "rider_id": g.user["_id"],
         "status": {"$in": list(_SEAT_BOOKING_DB)}})
    if existing:
        if idem and existing.get("idempotency_key") == idem:
            return {"ok": True, "booking": clean_booking(_hydrate(db, existing))}
        raise APIError("You have already booked this ride.", 409, code="already_booked")

    amount = _amount(ride["fare_per_seat"], seats)
    claimed = _claim_seats(db, ride, seats, amount)
    if not claimed:
        raise APIError("Not enough seats available on this ride.", 409, code="seats_unavailable")

    booking = {
        "ride_id": ride["_id"],
        "rider_id": g.user["_id"],
        "owner_id": ride["owner_id"],
        "seats": seats,
        "amount": amount,
        "fare_per_seat": ride["fare_per_seat"],
        "status": BOOKING_PENDING_PAYMENT,
        "payment": None,
        "idempotency_key": idem if idem else None,
        "ride_snapshot": _snapshot(ride),
        "refundable": True,
        "refund_amount": None,
        "refund_percentage": None,
        "refund_status": None,
        "refunded": False,
        "details": None,
        "created_at": _now(),
        "updated_at": _now(),
        "cancelled_at": None,
    }

    def _rollback(reason):
        _release_seats(db, ride_id, seats, amount)
        # Drop the payment row too: an order document pointing at a booking
        # that no longer exists would strand the seat claim on the gateway side.
        db.payments.delete_many({"booking_id": booking.get("_id")})
        db.bookings.delete_one({"_id": booking.get("_id")})
        raise APIError(reason, 502, code="payment_unavailable")

    try:
        result = db.bookings.insert_one(booking)
        booking["_id"] = result.inserted_id
    except DuplicateKeyError:
        _release_seats(db, ride_id, seats, amount)
        raise APIError("You have already booked this ride.", 409, code="already_booked")

    try:
        payment = create_order(booking, seats, amount)
        # Built inside the guard: a half-configured gateway must roll the seat
        # claim back, not leave the ride with a phantom held seat.
        checkout = checkout_config(payment, g.user)
    except APIError:
        _rollback("Payment could not be started; no charge was made.")
    db.bookings.update_one({"_id": booking["_id"]}, {"$set": {"payment": {
        "provider": payment["provider"],
        "order_id": payment["order_id"],
        "amount": payment["amount"],
        "status": payment["status"],
    }}})
    booking["payment"] = {
        "provider": payment["provider"],
        "order_id": payment["order_id"],
        "amount": payment["amount"],
        "status": payment["status"],
    }
    return {"ok": True, "booking": clean_booking(_hydrate(db, booking)),
            "payment": booking["payment"],
            "checkout": checkout}, 201


def _hydrate(db, booking):
    booking = dict(booking)
    ride = db.rides.find_one({"_id": booking["ride_id"]})
    if ride:
        booking["ride"] = {
            "id": str(ride["_id"]),
            "origin": ride.get("origin"),
            "destination": ride.get("destination"),
            "departure_date": ride.get("departure_date"),
            "departure_time": ride.get("departure_time"),
            "departure_at": iso_utc(ride.get("departure_at")),
            "status": ride["status"],
            "seats_total": ride.get("seats_total"),
            "seats_available": ride.get("seats_available"),
        }
        owner = db.users.find_one({"_id": ride["owner_id"]})
        booking["driver"] = driver_snapshot(owner) if owner else None
    return booking


# ------------------------------------------------------------------- verify
@bp.post("/<bid>/verify")
@require_auth
@rate_limit("strict")
def verify(bid):
    from ..ledger import record_payment_ledger

    db = get_db()
    _expire_pending_payments()
    booking_id = to_object_id(bid, "booking")
    booking = db.bookings.find_one({"_id": booking_id, "rider_id": g.user["_id"]})
    if not booking:
        raise APIError("Booking not found.", 404, code="not_found")
    if booking["status"] == BOOKING_CONFIRMED:
        return {"ok": True, "booking": clean_booking(_hydrate(db, booking))}

    payment = db.payments.find_one({"booking_id": booking_id})
    if not payment:
        # no order was created yet (retry path)
        payment = create_order(booking, booking["seats"], booking["amount"])

    pay_data = body()
    verified = verify_payment(booking, payment, pay_data)
    if verified.get("status") != "success":
        raise APIError("Payment has not settled. Please try again.", 402, code="payment_pending")

    if booking["status"] != BOOKING_PENDING_PAYMENT:
        raise APIError("Booking is in an unexpected state.", 409, code="state_conflict")
    if not can_transition_booking(BOOKING_PENDING_PAYMENT, BOOKING_CONFIRMED):
        raise APIError("Booking can no longer be confirmed.", 409, code="state_conflict")

    ride = db.rides.find_one({"_id": booking["ride_id"]})
    if ride is None or ride.get("status") in ("cancelled", "expired", "completed"):
        refund = refund_payment(verified, "Ride became unavailable before payment settled")
        db.bookings.update_one({"_id": booking_id},
                               {"$set": {"status": BOOKING_REFUNDED, "refunded": True,
                                         "refund_percentage": 100, "refund_status": "PROCESSED",
                                         "refund_amount": refund.get("amount", verified["amount"]),
                                         "updated_at": _now()}})
        raise APIError("Ride is no longer available; payment refunded.",
                       409, code="ride_unavailable")
    if is_past(ride.get("departure_at"), _now()):
        # payment settled after the ride left -> cannot board, reverse it
        refund = refund_payment(verified, "Ride departed before payment settled")
        db.bookings.update_one({"_id": booking_id},
                               {"$set": {"status": BOOKING_REFUNDED, "refunded": True,
                                         "refund_percentage": 100, "refund_status": "PROCESSED",
                                         "refund_amount": refund.get("amount", verified["amount"]),
                                         "updated_at": _now()}})
        raise APIError("Ride has already departed; payment refunded.",
                       409, code="ride_departed")

    db.bookings.update_one({"_id": booking_id},
                           {"$set": {"status": BOOKING_CONFIRMED, "updated_at": _now(),
                                     "payment": {
                                         "provider": verified["provider"],
                                         "order_id": verified.get("order_id"),
                                         "reference": verified.get("reference"),
                                         "amount": verified["amount"],
                                         "status": verified["status"],
                                     }}})
    fresh = db.bookings.find_one({"_id": booking_id})
    record_payment_ledger(fresh, verified)
    notifications.notify(
        fresh["rider_id"], "Booking confirmed",
        f"Your {fresh['seats']} seat(s) on {ride.get('origin', {}).get('label')} → "
        f"{ride.get('destination', {}).get('label')} are confirmed.",
        ref_type="booking", ref_id=str(fresh["_id"]))
    return {"ok": True, "booking": clean_booking(_hydrate(db, fresh))}


# ------------------------------------------------------------------- mine
@bp.get("/mine")
@require_auth
def my_bookings():
    db = get_db()
    now = _now()
    rows = list(db.bookings.find({"rider_id": g.user["_id"]}).sort("created_at", -1).limit(200))

    ride_ids = {b["ride_id"] for b in rows}
    rides = {r["_id"]: r for r in db.rides.find({"_id": {"$in": list(ride_ids)}})}
    owner_ids = {r["owner_id"] for r in rides.values()}
    owners = {u["_id"]: u for u in db.users.find({"_id": {"$in": list(owner_ids)}})}

    scope = request.args.get("scope", "all")
    result = []
    for b in rows:
        ride = rides.get(b["ride_id"])
        departed = bool(ride and is_past(ride.get("departure_at"), now))
        active_status = bool(b["status"] in _ACTIVE_BOOKING_DB)
        cancelled = b["status"] == BOOKING_CANCELLED
        ride_active = bool(ride and ride.get("status") not in ("cancelled", "expired", "completed") and not departed)
        if scope == "upcoming" and not (ride_active and active_status):
            continue
        if scope == "past" and (ride_active and active_status) and not cancelled:
            continue
        item = clean_booking(b)
        if ride:
            item["ride"] = {
                "id": str(ride["_id"]),
                "origin": ride.get("origin"),
                "destination": ride.get("destination"),
                "departure_date": ride.get("departure_date"),
                "departure_time": ride.get("departure_time"),
                "departure_at": iso_utc(ride.get("departure_at")),
                "status": ride["status"],
                "seats_total": ride.get("seats_total"),
                "seats_available": ride.get("seats_available"),
            }
        owner = owners.get(b["owner_id"])
        if owner:
            item["driver"] = driver_snapshot(owner)

        ts = ride.get("departure_at") if ride else b.get("created_at")
        if ts is None:
            ts = b.get("cancelled_at") or b.get("updated_at") or now
        result.append((0 if (ride_active and active_status) else 1, ts, item))

    result.sort(key=lambda t: (t[0], t[1]))
    if scope != "upcoming":
        result.sort(key=lambda t: (t[0], -t[1].timestamp() if t[1] else 0))
    return {"ok": True, "data": [item for _, _, item in result]}


# -------------------------------------------------------------- passengers
@bp.get("/for-ride/<rid>")
@require_auth
def ride_passengers(rid):
    db = get_db()
    ride_id = to_object_id(rid, "ride")
    ride = db.rides.find_one({"_id": ride_id})
    if not ride or str(ride["owner_id"]) != str(g.user["_id"]):
        raise APIError("Ride not found.", 404, code="not_found")

    bookings = list(db.bookings.find(
        {"ride_id": ride_id, "status": {"$in": ["confirmed", "pending_payment"]}})
        .sort("created_at", 1))
    rider_ids = {b["rider_id"] for b in bookings}
    riders = {u["_id"]: u for u in db.users.find({"_id": {"$in": list(rider_ids)}})}
    data = [
        {"seats": b["seats"], "amount": b["amount"], "booking_id": str(b["_id"]),
         "status": b["status"],
         "rider": public_user(riders.get(b["rider_id"])) if riders.get(b["rider_id"]) else None}
        for b in bookings
    ]
    return {"ok": True, "data": data}


# ------------------------------------------------------------------ cancel
@bp.delete("/<bid>")
@require_auth
@rate_limit("strict")
def cancel_booking(bid):
    db = get_db()
    now = _now()
    booking_id = to_object_id(bid, "booking")
    booking = db.bookings.find_one({"_id": booking_id, "rider_id": g.user["_id"]})
    if not booking:
        raise APIError("Booking not found.", 404, code="not_found")
    if booking["status"] == BOOKING_CANCELLED:
        raise APIError("Booking is already cancelled.", 409, code="already_cancelled")
    if booking["status"] not in _ACTIVE_BOOKING_DB:
        raise APIError("Booking is closed.", 409, code="state_conflict")

    ride = db.rides.find_one({"_id": booking["ride_id"]})
    if ride and is_past(ride.get("departure_at"), now):
        raise APIError("This ride has already departed and the booking cannot be cancelled.",
                       409, code="ride_departed")

    pct, reason, _cutoff_hours = _refund_policy(ride, now)
    refund_amount = round(booking["amount"] * pct / 100.0, 2)

    _release_seats(db, booking["ride_id"], booking["seats"], booking["amount"])

    # Only callers who merely witnessed the pre-cancel state land here with a
    # claim: the transition is performed atomically from the EXACT status this
    # request validated, so exactly one concurrent cancel wins the booking and
    # reaches the refund path. Losers fall out with a 409 and never touch the
    # gateway, the ledger, or the payment status (not even once).
    if not ride or booking["status"] == BOOKING_PENDING_PAYMENT:
        # nothing (or nothing settled) to refund; just cancel
        claim = db.bookings.find_one_and_update(
            {"_id": booking_id, "status": booking["status"]},
            {"$set": {"status": BOOKING_CANCELLED, "cancelled_at": now,
                      "refund_amount": 0.0, "refunded": True, "updated_at": now,
                      "refund_percentage": pct,
                      "refund_status": "NOT_REQUIRED",
                      "details": reason}})
        if claim is None:
            raise APIError("Booking was cancelled concurrently.", 409, code="state_conflict")
        fresh = db.bookings.find_one({"_id": booking_id})
        return {"ok": True, "booking": clean_booking(_hydrate(db, fresh))}

    # confirmed booking -> refund per policy. A 0% policy closes the booking
    # with refund_status=NOT_REQUIRED and never calls the payment gateway.
    if refund_amount <= 0:
        claim = db.bookings.find_one_and_update(
            {"_id": booking_id, "status": booking["status"]},
            {"$set": {"status": BOOKING_CANCELLED, "cancelled_at": now,
                      "refund_amount": 0.0, "refunded": True, "updated_at": now,
                      "refund_percentage": 0, "refund_status": "NOT_REQUIRED",
                      "details": reason}})
        if claim is None:
            raise APIError("Booking was cancelled concurrently.", 409, code="state_conflict")
        fresh = db.bookings.find_one({"_id": booking_id})
        notifications.notify(
            fresh["rider_id"], "Booking cancelled",
            "Booking cancelled.", ref_type="booking", ref_id=str(fresh["_id"]))
        return {"ok": True, "booking": clean_booking(_hydrate(db, fresh))}

    payment = db.payments.find_one({"booking_id": booking_id, "status": "success"})
    if payment is None:
        # nothing settled yet -> no provider refund is possible or needed
        claim = db.bookings.find_one_and_update(
            {"_id": booking_id, "status": booking["status"]},
            {"$set": {"status": BOOKING_CANCELLED, "cancelled_at": now,
                      "refund_amount": 0.0, "refunded": True, "updated_at": now,
                      "refund_percentage": pct, "refund_status": "NOT_REQUIRED",
                      "details": reason}})
        if claim is None:
            raise APIError("Booking was cancelled concurrently.", 409, code="state_conflict")
    else:
        claim = db.bookings.find_one_and_update(
            {"_id": booking_id, "status": BOOKING_CONFIRMED},
            {"$set": {"status": BOOKING_REFUND_PENDING, "cancelled_at": now,
                      "refund_amount": refund_amount, "refund_percentage": pct,
                      "refund_status": "PROCESSING",
                      "updated_at": now, "details": reason}})
        if claim is None:
            raise APIError("Booking was cancelled concurrently.", 409, code="state_conflict")
        refund_payment(payment, reason, amount=refund_amount)
        db.bookings.update_one(
            {"_id": booking_id},
            {"$set": {"status": BOOKING_REFUNDED, "refunded": True,
                      "refund_percentage": pct, "refund_status": "PROCESSED",
                      "updated_at": now}})

    fresh = db.bookings.find_one({"_id": booking_id})
    notifications.notify(
        fresh["rider_id"], "Booking cancelled",
        f"{pct}% refund of ${booking['amount']:.2f} issued" if pct else "Booking cancelled.",
        ref_type="booking", ref_id=str(fresh["_id"]))
    return {"ok": True, "booking": clean_booking(_hydrate(db, fresh))}