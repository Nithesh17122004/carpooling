"""Passenger ride completion and dispute handling.

When a driver finished a trip, the fare has been collected but the trip is not
yet *finished* in any sense the passenger would recognise. The old behaviour
marked every confirmed booking `completed` the moment the driver tapped the
button, which released the driver's payable immediately. That makes the driver
the sole authority on whether a trip happened, so a no-show or an unpleasant trip
is indistinguishable from a good one, and there is nothing to appeal.

This module inserts a confirmation window between "driver finished" and "money is
payable":

    confirmed --driver completes--> awaiting_completion
                                              |
                        +---------------------+---------------------+
                        |                                           |
                 passenger confirms                          timeout reached
                        |                                           |
                        v                                           v
                    confirmed                                    auto_confirmed
                                                              (worker, after
                                                               the window)

    awaiting_completion --passenger disputes--> disputed --admin resolves-->
                                        completed | refund_pending

Two properties are the point of the whole design:

* **`is_payable()` is the only question the money path asks.** It is derived from
  the completion state, never from a status the driver set, and a disputed trip
  is never payable. Payout creation and the settlement worker both call it.
* **Auto-completion cannot race a real answer.** The worker's claim is a
  conditional `update_one` on the same `completion_status` the passenger's tap
  would change, so whichever lands first wins and the loser becomes a no-op
  rather than an overwrite.
"""

from datetime import timedelta

from flask import current_app
from pymongo import ReturnDocument

from . import audit
from .db import get_db, utcnow
from .errors import APIError
from .states import (
    BOOKING_AWAITING_COMPLETION,
    BOOKING_COMPLETED,
    BOOKING_CONFIRMED,
    BOOKING_DISPUTED,
    BOOKING_REFUND_PENDING,
    can_transition_booking,
)
from .timeutil import iso_utc

COMPLETION_AWAITING = "awaiting_passenger"
COMPLETION_CONFIRMED = "confirmed"
COMPLETION_AUTO_CONFIRMED = "auto_confirmed"
COMPLETION_DISPUTED = "disputed"

COMPLETION_STATES = {COMPLETION_AWAITING, COMPLETION_CONFIRMED,
                     COMPLETION_AUTO_CONFIRMED, COMPLETION_DISPUTED}

# The only completion states in which a driver's earnings may be paid out.
PAYABLE_COMPLETION_STATES = {COMPLETION_CONFIRMED, COMPLETION_AUTO_CONFIRMED}

_TRANSITIONS = {
    COMPLETION_AWAITING: {COMPLETION_CONFIRMED, COMPLETION_AUTO_CONFIRMED,
                          COMPLETION_DISPUTED},
    COMPLETION_CONFIRMED: set(),
    COMPLETION_AUTO_CONFIRMED: set(),
    # A dispute is resolved by an admin, not by the passenger reopening it.
    COMPLETION_DISPUTED: {COMPLETION_CONFIRMED, COMPLETION_AUTO_CONFIRMED},
}

_MAX_REASON = 500

DISPUTE_REASONS = (
    "driver_no_show", "trip_not_completed", "overcharged", "unsafe_driving",
    "vehicle_mismatch", "other",
)


def confirm_window_minutes():
    """How long a passenger has to confirm before auto-completion."""
    return int(current_app.config.get("COMPLETION_CONFIRM_TIMEOUT_MINUTES",
                                      24 * 60) or 24 * 60)


def completion_state(booking):
    if not booking:
        return None
    return booking.get("completion_status") or COMPLETION_AWAITING


def can_transition_completion(source, target):
    return target in _TRANSITIONS.get(completion_state({"completion_status": source}),
                                      set())


def is_payable(booking):
    """May this booking's driver earnings be released to settlement?

    Derived, never stored as its own flag, so it cannot drift out of step with
    the completion state. A trip that nobody has confirmed is not payable even
    though the fare was collected, and a disputed trip is not payable even
    though the driver completed it.
    """
    if not booking:
        return False
    return completion_state(booking) in PAYABLE_COMPLETION_STATES


def deadline_for(now=None):
    now = now or utcnow()
    return now + timedelta(minutes=confirm_window_minutes())


def is_expired(booking, now=None):
    """Has the confirmation window closed?"""
    if completion_state(booking) != COMPLETION_AWAITING:
        return False
    deadline = booking.get("completion_deadline")
    if not deadline:
        return False
    return (now or utcnow()) >= deadline


# ------------------------------------------------------------------- lifecycle
def driver_completed(ride_id, driver_id, *, now=None):
    """Move every confirmed booking on a ride into the confirmation window.

    Called by the ride-completion endpoint. Returns the booking ids that entered
    the window.

    Each booking is claimed with a conditional update, so two drivers (or a retry
    of the same request) cannot both open a window on the same booking.
    """
    now = now or utcnow()
    deadline = deadline_for(now)
    db = get_db()

    opened = []
    for booking in db.bookings.find({"ride_id": ride_id, "status": BOOKING_CONFIRMED}):
        if not can_transition_booking(booking["status"], BOOKING_AWAITING_COMPLETION):
            continue
        result = db.bookings.update_one(
            {"_id": booking["_id"], "status": BOOKING_CONFIRMED},
            {"$set": {
                "status": BOOKING_AWAITING_COMPLETION,
                "completion_status": COMPLETION_AWAITING,
                "driver_completed_at": now,
                "completion_deadline": deadline,
                "passenger_confirmed_at": None,
                "updated_at": now,
            }})
        if result.modified_count:
            opened.append(str(booking["_id"]))
            audit.record("financial.completion.awaiting", domain=audit.FINANCIAL,
                         actor_id=driver_id, actor_role="driver",
                         target_type="booking", target_id=booking["_id"],
                         meta={"ride_id": str(ride_id),
                               "deadline": iso_utc(deadline),
                               "amount": booking.get("amount")})
    return opened


def passenger_confirmed(booking_id, passenger_id, *, now=None):
    """Passenger confirms the trip. This is the event that makes it payable."""
    now = now or utcnow()
    db = get_db()
    booking = db.bookings.find_one({"_id": booking_id})
    if not booking:
        raise APIError("Booking not found.", 404, code="not_found")
    if booking.get("rider_id") != passenger_id:
        raise APIError("Only the passenger on this trip can confirm it.", 403,
                       code="forbidden")

    state = completion_state(booking)
    if state == COMPLETION_CONFIRMED:
        return db.bookings.find_one({"_id": booking_id}), True
    if state != COMPLETION_AWAITING:
        raise APIError(
            "This trip cannot be confirmed (completion state: %s)." % state, 409,
            code="completion_invalid_transition",
            details={"from": state, "to": COMPLETION_CONFIRMED})

    claimed = db.bookings.find_one_and_update(
        {"_id": booking_id, "status": BOOKING_AWAITING_COMPLETION,
         "completion_status": COMPLETION_AWAITING},
        {"$set": {
            "status": BOOKING_COMPLETED,
            "completion_status": COMPLETION_CONFIRMED,
            "passenger_confirmed_at": now,
            "completed_at": now,
            "updated_at": now,
         }},
        # AFTER, not the pymongo default of BEFORE: the caller is handed the
        # booking it just confirmed. Returning the pre-update document would
        # make `passenger_confirmed()` report a state that no longer exists.
        return_document=ReturnDocument.AFTER)
    if claimed is None:
        # The window expired and the worker already auto-confirmed, or a dispute
        # landed first. Report the real state rather than pretending to confirm.
        current = db.bookings.find_one({"_id": booking_id})
        if completion_state(current) in PAYABLE_COMPLETION_STATES:
            return current, True
        raise APIError(
            "This trip can no longer be confirmed.", 409,
            code="completion_window_closed",
            details={"completion_status": completion_state(current)})

    audit.record("financial.completion.confirmed", domain=audit.FINANCIAL,
                 actor_id=passenger_id, actor_role="passenger",
                 target_type="booking", target_id=booking_id,
                 meta={"ride_id": str(booking.get("ride_id")),
                       "amount": booking.get("amount"),
                       "payable": True})
    return claimed, False


def raise_dispute(booking_id, passenger_id, reason, details=None, *, now=None):
    """Passenger disputes. Blocks settlement immediately and permanently.

    There is no path from `disputed` back to payable by the passenger -- only an
    admin decision can release the money, which is what stops a driver from
    simply waiting out a complaint and then completing it.
    """
    now = now or utcnow()
    if not (reason or "").strip():
        raise APIError("Tell us what went wrong.", 422,
                       code="validation_error", details={"fields": ["reason"]})
    if reason not in DISPUTE_REASONS:
        raise APIError("Unknown dispute reason.", 422, code="validation_error",
                       details={"fields": ["reason"],
                                "allowed": list(DISPUTE_REASONS)})

    db = get_db()
    booking = db.bookings.find_one({"_id": booking_id})
    if not booking:
        raise APIError("Booking not found.", 404, code="not_found")
    if booking.get("rider_id") != passenger_id:
        raise APIError("Only the passenger on this trip can raise a dispute.", 403,
                       code="forbidden")
    if completion_state(booking) != COMPLETION_AWAITING:
        raise APIError("This trip can no longer be disputed.", 409,
                       code="completion_window_closed",
                       details={"completion_status": completion_state(booking)})

    claimed = db.bookings.find_one_and_update(
        {"_id": booking_id, "status": BOOKING_AWAITING_COMPLETION,
         "completion_status": COMPLETION_AWAITING},
        {"$set": {
            "status": BOOKING_DISPUTED,
            "completion_status": COMPLETION_DISPUTED,
            "dispute_reason": reason,
            "dispute_details": str(details or "").strip()[:_MAX_REASON] or None,
            "disputed_at": now,
            "updated_at": now,
         }},
        return_document=ReturnDocument.AFTER)
    if claimed is None:
        raise APIError("This trip can no longer be disputed.", 409,
                       code="completion_window_closed")

    audit.record("financial.completion.dispute", domain=audit.FINANCIAL,
                 actor_id=passenger_id, actor_role="passenger",
                 target_type="booking", target_id=booking_id,
                 reason=details or reason,
                 meta={"ride_id": str(booking.get("ride_id")),
                       "dispute_reason": reason,
                       "amount": booking.get("amount"),
                       "payable": False})
    return claimed


def auto_confirm(booking_id, *, now=None, actor_id="settlement-worker"):
    """Worker path: close the window once the deadline has passed.

    The conditional filter on `completion_status` is the concurrency control. If
    the passenger taps confirm in the same millisecond, one of the two
    `find_one_and_update` calls matches zero documents and this becomes a no-op
    rather than clobbering their confirmation.
    """
    now = now or utcnow()
    db = get_db()
    booking = db.bookings.find_one({"_id": booking_id})
    if not booking:
        return None
    if not is_expired(booking, now):
        return None

    claimed = db.bookings.find_one_and_update(
        {"_id": booking_id, "status": BOOKING_AWAITING_COMPLETION,
         "completion_status": COMPLETION_AWAITING,
         "completion_deadline": {"$lte": now}},
        {"$set": {
            "status": BOOKING_COMPLETED,
            "completion_status": COMPLETION_AUTO_CONFIRMED,
            "completed_at": now,
            "auto_confirmed_at": now,
            "updated_at": now,
         }},
        return_document=ReturnDocument.AFTER)
    if claimed is None:
        return None

    audit.record("financial.completion.auto_confirmed", domain=audit.FINANCIAL,
                 actor_id=actor_id, target_type="booking", target_id=booking_id,
                 meta={"ride_id": str(booking.get("ride_id")),
                       "amount": booking.get("amount"),
                       "payable": True,
                       "window_minutes": confirm_window_minutes()})
    return claimed


def auto_confirm_expired(limit=100, *, now=None):
    """Every booking whose window has closed. Used by the settlement worker."""
    now = now or utcnow()
    db = get_db()
    rows = db.bookings.find(
        {"status": BOOKING_AWAITING_COMPLETION,
         "completion_status": COMPLETION_AWAITING,
         "completion_deadline": {"$ne": None, "$lte": now}},
        {"_id": 1}).limit(limit)
    confirmed = []
    for row in rows:
        result = auto_confirm(row["_id"], now=now)
        if result is not None:
            confirmed.append(str(row["_id"]))
    return confirmed


def resolve_dispute(booking_id, resolution, admin_id, note=None):
    """Admin decision on a disputed trip.

    `release` pays the driver (the trip happened), `refund` sends the fare back.
    Both are terminal and neither can be undone through this module.
    """
    now = utcnow()
    resolution = (resolution or "").strip().lower()
    if resolution in ("release", "release_to_driver", "dismiss", "uphold_driver"):
        completion = COMPLETION_AUTO_CONFIRMED
        status = BOOKING_COMPLETED
    elif resolution in ("refund", "refund_passenger", "partial_refund"):
        completion = COMPLETION_DISPUTED     # stays non-payable; payouts.py refunds
        status = BOOKING_REFUND_PENDING
    else:
        raise APIError("resolution must be 'release' or 'refund'.", 422,
                       code="validation_error", details={"fields": ["resolution"]})

    db = get_db()
    booking = db.bookings.find_one({"_id": booking_id})
    if not booking:
        raise APIError("Booking not found.", 404, code="not_found")
    if completion_state(booking) != COMPLETION_DISPUTED:
        raise APIError("This trip is not disputed.", 409,
                       code="completion_invalid_transition",
                       details={"completion_status": completion_state(booking)})

    claimed = db.bookings.find_one_and_update(
        {"_id": booking_id, "status": BOOKING_DISPUTED,
         "completion_status": COMPLETION_DISPUTED},
        {"$set": {
            "status": status,
            "completion_status": completion,
            "dispute_resolution": resolution,
            "dispute_resolved_at": now,
            "dispute_resolved_by": admin_id,
            "dispute_admin_note": str(note or "").strip()[:_MAX_REASON] or None,
            "updated_at": now,
         }},
        return_document=ReturnDocument.AFTER)
    if claimed is None:
        raise APIError("This dispute was already resolved.", 409,
                       code="dispute_already_resolved")

    audit.record("financial.completion.dispute_resolved", domain=audit.FINANCIAL,
                 actor_id=admin_id, actor_role="admin",
                 target_type="booking", target_id=booking_id,
                 reason=note,
                 meta={"resolution": resolution,
                       "status": status,
                       "amount": booking.get("amount"),
                       "payable": is_payable(claimed)})
    return claimed


# ----------------------------------------------------------------------- gate
def assert_payable(booking):
    """Refuse settlement for a booking that is not confirmed.

    A dispute is reported as a dispute rather than as a generic block, because
    the correct action for an operator is completely different.
    """
    if is_payable(booking):
        return
    state = completion_state(booking)
    details = {"completion_status": state, "booking_status": booking.get("status")}

    if state == COMPLETION_DISPUTED:
        raise APIError(
            "This trip is under dispute and cannot be settled until it is "
            "resolved.", 409, code="trip_disputed",
            details={**details, "dispute_reason": booking.get("dispute_reason")})
    if state == COMPLETION_AWAITING:
        raise APIError(
            "Waiting for the passenger to confirm this trip before payout.", 409,
            code="completion_pending",
            details={**details,
                     "deadline": iso_utc(booking.get("completion_deadline"))})
    raise APIError("This trip is not complete.", 409,
                   code="completion_required", details=details)


# --------------------------------------------------------------------- views
def completion_summary(booking):
    """The view both the passenger and the driver see for one trip.

    Reports what still has to happen and by when, rather than just a status
    string, because "confirm your trip" is only actionable next to a deadline.
    """
    booking = booking or {}
    state = completion_state(booking)
    window = confirm_window_minutes()
    return {
        "completion_status": state,
        "payable": is_payable(booking),
        "driver_completed_at": iso_utc(booking.get("driver_completed_at")),
        "passenger_confirmed_at": iso_utc(booking.get("passenger_confirmed_at")),
        "auto_confirmed_at": iso_utc(booking.get("auto_confirmed_at")),
        "completed_at": iso_utc(booking.get("completed_at")),
        "deadline": iso_utc(booking.get("completion_deadline")),
        "window_minutes": window,
        # The two things a passenger can do right now.
        "can_confirm": state == COMPLETION_AWAITING,
        "can_dispute": state == COMPLETION_AWAITING,
        "can_withdraw": state in (COMPLETION_CONFIRMED, COMPLETION_AUTO_CONFIRMED),
        "dispute_reason": booking.get("dispute_reason"),
        "dispute_details": booking.get("dispute_details"),
        "disputed_at": iso_utc(booking.get("disputed_at")),
        "dispute_resolution": booking.get("dispute_resolution"),
        "dispute_resolved_at": iso_utc(booking.get("dispute_resolved_at")),
        "dispute_admin_note": booking.get("dispute_admin_note"),
    }
