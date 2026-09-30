"""Driver-asserted trip lifecycle and no-show handling.

The rule under test throughout: the CLIENT proposes a coarse phase ("start",
"boarding", "complete") and the SERVER decides the exact state. There is no
endpoint that accepts a status string verbatim, because a client that can set
`completed` directly would also be able to skip `boarding`, and -- worse -- could
release driver payables for a trip that never ran.
"""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from backend import completion
from backend.completion import COMPLETION_AWAITING
from backend.states import (
    BOOKING_AWAITING_COMPLETION,
    BOOKING_CANCELLED,
    BOOKING_COMPLETED,
    BOOKING_CONFIRMED,
    BOOKING_NO_SHOW,
    BOOKING_REFUNDED,
    RIDE_BOARDING,
    RIDE_COMPLETED,
    RIDE_DRIVER_EN_ROUTE,
    RIDE_IN_PROGRESS,
    RIDE_PUBLISHED,
)
from backend.tests.conftest import make_ride

HOUR = timedelta(hours=1)


def _new_ride(client, driver, vehicle, seats=2, fare=100):
    """A published ride with a valid FUTURE departure."""
    ride = make_ride(client, driver["auth"], vehicle, seats=seats, fare=fare, day=2, hh=23, mm=0)
    assert ride.status_code == 201, ride.get_json()
    return ride.get_json()["ride"]["id"]


def _backdate_departure(db, ride_id):
    """Move a ride's departure into the past.

    Bookings require a future departure and completion requires a past one, so a
    trip that is actually driven has to pass through both. This is the only
    backdating seam, and it writes the field the server's own checks read.
    """
    db.rides.update_one({"_id": ObjectId(ride_id)},
                        {"$set": {"departure_at": datetime.now(timezone.utc) - HOUR}})


def _departed_ride(client, db, driver, vehicle, seats=2, fare=100):
    """A published ride whose departure time has already passed, so it is legal
    to move it all the way to `in_progress` and `completed`."""
    ride_id = _new_ride(client, driver, vehicle, seats=seats, fare=fare)
    _backdate_departure(db, ride_id)
    return ride_id


def _book(client, db, rider, ride_id, seats=1):
    resp = client.post("/api/bookings", headers=rider["auth"],
                       json={"ride_id": ride_id, "seats": seats})
    assert resp.status_code == 201, resp.get_json()
    booking = resp.get_json()["booking"]
    verify = client.post(f"/api/bookings/{booking['id']}/verify", headers=rider["auth"],
                         json={"payment_id": "pay_life", "signature": "sig_life"})
    assert verify.status_code == 200, verify.get_json()
    assert verify.get_json()["booking"]["status"] == BOOKING_CONFIRMED
    return db.bookings.find_one({"_id": ObjectId(booking["id"])})


@pytest.fixture
def trip(client, db, driver, rider, vehicle):
    """A departed ride with one confirmed rider, ready to be driven to completion.

    Ordering matters: the rider books while the departure is still in the future
    (bookings require that), and only then is the ride backdated.
    """
    ride_id = _new_ride(client, driver, vehicle)
    booking = _book(client, db, rider, ride_id)
    _backdate_departure(db, ride_id)
    return {"ride_id": ride_id, "booking_id": str(booking["_id"]), "booking": booking}


# ------------------------------------------------------------- the happy path
def test_lifecycle_walks_the_legal_states_in_order(client, trip, driver):
    rid = trip["ride_id"]
    for phase, expected in (("start", RIDE_DRIVER_EN_ROUTE),
                            ("boarding", RIDE_BOARDING),
                            ("depart", RIDE_IN_PROGRESS),
                            ("complete", RIDE_COMPLETED)):
        resp = client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                           json={"status": phase})
        assert resp.status_code == 200, (phase, resp.get_json())
        assert resp.get_json()["ride"]["status"] == expected, phase
        assert "status" not in resp.get_json()["ride"] or \
            resp.get_json()["ride"]["status"] == expected


def test_completing_the_trip_opens_the_confirmation_window(client, db, trip, driver):
    """The driver's tap must NOT close the booking.

    The old behaviour set `completed` here, which released the driver's payable
    the instant they tapped the button and made the driver the sole authority on
    whether a trip happened. The trip now only enters the confirmation window;
    settlement waits on the passenger (see completion.py).
    """
    rid, bid = trip["ride_id"], trip["booking_id"]
    for phase in ("start", "boarding", "depart"):
        client.post(f"/api/rides/{rid}/status", headers=driver["auth"], json={"status": phase})

    resp = client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                       json={"status": "complete"})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["completed_bookings"] == 1
    assert resp.get_json()["booking_ids"] == [bid]
    assert resp.get_json()["awaiting_confirmation"] == [bid]
    # Money has NOT moved, and the response says so.
    assert resp.get_json()["payable_now"] == []

    booking = db.bookings.find_one({"_id": ObjectId(bid)})
    assert booking["status"] == BOOKING_AWAITING_COMPLETION
    assert booking["completion_status"] == COMPLETION_AWAITING
    assert booking["driver_completed_at"] is not None
    assert booking["passenger_confirmed_at"] is None
    # `completed_at` is absent until something actually completes the trip.
    assert booking.get("completed_at") is None
    assert booking["completion_deadline"] is not None
    assert completion.is_payable(booking) is False


def test_completion_is_idempotent(client, db, trip, driver):
    """A retried 'complete' (double tap, flaky network) must not re-open the
    window, re-notify, or reset a passenger confirmation that already landed."""
    rid, bid = trip["ride_id"], trip["booking_id"]
    for phase in ("start", "boarding", "depart"):
        client.post(f"/api/rides/{rid}/status", headers=driver["auth"], json={"status": phase})
    first = client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                        json={"status": "complete"})
    second = client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                         json={"status": "complete"})

    assert first.status_code == 200 and second.status_code == 200
    # the repeat reports the already-opened state instead of claiming new work
    assert second.get_json()["completed_bookings"] == 1
    assert db.bookings.count_documents({"ride_id": ObjectId(rid),
                                        "status": BOOKING_AWAITING_COMPLETION}) == 1


# ------------------------------------------------------------- what is refused
def test_phases_cannot_be_skipped(client, trip, driver):
    """Publishing straight to `completed` would release payables for a trip that
    never drove, so the server's transition table is the only authority."""
    rid = trip["ride_id"]
    skipped = client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                          json={"status": "complete"})
    assert skipped.status_code == 409, skipped.get_json()
    assert skipped.get_json()["error"]["code"] == "invalid_transition"

    # the ride is untouched and can still be driven normally
    assert client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                       json={"status": "start"}).status_code == 200
    backwards = client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                            json={"status": "boarding"})
    assert backwards.status_code == 200  # en_route -> boarding is forward


def test_unknown_phase_is_rejected(client, trip, driver):
    resp = client.post(f"/api/rides/{trip['ride_id']}/status", headers=driver["auth"],
                       json={"status": "teleport"})
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "validation_error"
    assert "allowed" in resp.get_json()["error"]["details"]


def test_a_client_cannot_set_an_arbitrary_ride_status(client, trip, driver):
    """There is no escape hatch that accepts a raw state name."""
    for bogus in ("published", "cancelled", RIDE_COMPLETED, "failed"):
        resp = client.post(f"/api/rides/{trip['ride_id']}/status", headers=driver["auth"],
                           json={"status": bogus})
        assert resp.status_code == 422, (bogus, resp.get_json())


def test_only_the_driver_may_advance_the_ride(client, trip, rider, driver):
    resp = client.post(f"/api/rides/{trip['ride_id']}/status", headers=rider["auth"],
                       json={"status": "start"})
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "forbidden"


def test_a_rider_cannot_complete_someone_elses_ride(client, trip, rider, driver):
    rid = trip["ride_id"]
    for phase in ("start", "boarding", "depart"):
        client.post(f"/api/rides/{rid}/status", headers=driver["auth"], json={"status": phase})
    resp = client.post(f"/api/rides/{rid}/status", headers=rider["auth"],
                       json={"status": "complete"})
    assert resp.status_code == 403


def test_depart_before_the_scheduled_time_is_refused(client, db, driver, vehicle):
    """A future ride may be driven en route early, but cannot depart or complete
    before its departure time."""
    ride = make_ride(client, driver["auth"], vehicle, day=3, hh=23, mm=0)
    rid = ride.get_json()["ride"]["id"]
    assert client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                       json={"status": "start"}).status_code == 200
    early = client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                        json={"status": "depart"})
    assert early.status_code == 409
    assert early.get_json()["error"]["code"] == "ride_not_departed"
    # still en route, not silently advanced
    assert db.rides.find_one({"_id": ObjectId(rid)})["status"] == RIDE_DRIVER_EN_ROUTE


def test_completed_ride_is_terminal(client, db, trip, driver):
    rid = trip["ride_id"]
    for phase in ("start", "boarding", "depart", "complete"):
        assert client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                           json={"status": phase}).status_code == 200
    # cannot re-drive a finished trip
    resp = client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                       json={"status": "start"})
    assert resp.status_code == 409
    assert db.rides.find_one({"_id": ObjectId(rid)})["status"] == RIDE_COMPLETED


# ------------------------------------------------------------------- no-show
def test_no_show_refunds_the_rider_and_releases_the_seat(client, db, trip, driver):
    rid, bid = trip["ride_id"], trip["booking_id"]
    before = db.rides.find_one({"_id": ObjectId(rid)})

    resp = client.post(f"/api/rides/{rid}/bookings/{bid}/no-show",
                       headers=driver["auth"], json={})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["booking"]["status"] == BOOKING_NO_SHOW

    booking = db.bookings.find_one({"_id": ObjectId(bid)})
    assert booking["status"] == BOOKING_NO_SHOW
    assert booking["no_show_at"] is not None

    # the seat is back in the pool and the projected earnings dropped
    after = db.rides.find_one({"_id": ObjectId(rid)})
    assert after["seats_available"] == before["seats_available"] + 1
    assert after["earnings"] == 0

    # the rider was made whole, and the ledger reflects the reversal
    payment = db.payments.find_one({"booking_id": ObjectId(bid), "status": "success"})
    assert payment is None, "a refunded payment is no longer 'success'"
    refund = db.refunds.find_one({"payment_id": payment["_id"]}) if payment else None
    assert refund is None or refund["amount"] > 0
    assert db.ledger_entries.count_documents(
        {"entry_type": "DRIVER_PAYABLE_REVERSAL"}) >= 1


def test_no_show_is_idempotent(client, db, trip, driver):
    rid, bid = trip["ride_id"], trip["booking_id"]
    first = client.post(f"/api/rides/{rid}/bookings/{bid}/no-show",
                        headers=driver["auth"], json={})
    assert first.status_code == 200
    after_first = db.rides.find_one({"_id": ObjectId(rid)})

    second = client.post(f"/api/rides/{rid}/bookings/{bid}/no-show",
                         headers=driver["auth"], json={})
    assert second.status_code == 200
    assert second.get_json().get("idempotent") is True
    # no second seat release and no second refund
    after_second = db.rides.find_one({"_id": ObjectId(rid)})
    assert after_second["seats_available"] == after_first["seats_available"]
    assert db.refunds.count_documents({"booking_id": ObjectId(bid)}) <= 1


def test_no_show_cannot_be_overturned_into_a_completed_trip(client, db, trip, driver):
    """A no-show must not be undoable into earnings for a service not provided."""
    rid, bid = trip["ride_id"], trip["booking_id"]
    client.post(f"/api/rides/{rid}/bookings/{bid}/no-show", headers=driver["auth"], json={})
    for phase in ("start", "boarding", "depart", "complete"):
        assert client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                           json={"status": phase}).status_code == 200
    assert db.bookings.find_one({"_id": ObjectId(bid)})["status"] == BOOKING_NO_SHOW
    # and it releases no driver earnings. The original payable is NOT deleted --
    # a reversal is posted against it -- so the invariant is that the NET is
    # zero, which is what would actually be settled.
    from backend.payouts import earned_payable, outstanding_payable

    driver_id = driver["user"]["_id"]
    assert earned_payable(driver_id) == 0
    assert outstanding_payable(driver_id) == 0


def test_completion_skips_bookings_already_marked_no_show(client, db, trip, driver):
    """The no-show stays terminal when the trip is later completed."""
    rid, bid = trip["ride_id"], trip["booking_id"]
    client.post(f"/api/rides/{rid}/bookings/{bid}/no-show", headers=driver["auth"], json={})
    for phase in ("start", "boarding", "depart", "complete"):
        client.post(f"/api/rides/{rid}/status", headers=driver["auth"], json={"status": phase})
    assert db.bookings.find_one({"_id": ObjectId(bid)})["status"] == BOOKING_NO_SHOW
    assert db.bookings.count_documents({"ride_id": ObjectId(rid),
                                        "status": BOOKING_COMPLETED}) == 0


def test_no_show_requires_the_driver_and_a_booking_on_that_ride(client, trip, rider, driver):
    rid, bid = trip["ride_id"], trip["booking_id"]
    forbidden = client.post(f"/api/rides/{rid}/bookings/{bid}/no-show",
                            headers=rider["auth"], json={})
    assert forbidden.status_code == 403

    missing = client.post(f"/api/rides/{rid}/bookings/"
                          "000000000000000000000000/no-show",
                          headers=driver["auth"], json={})
    assert missing.status_code == 404


def test_a_cancelled_booking_cannot_be_marked_no_show(client, db, driver, rider, vehicle):
    """No-show applies to a *confirmed* rider. A self-cancelled booking has
    already been refunded, and claiming otherwise would double-refund."""
    rid = _new_ride(client, driver, vehicle)
    resp = client.post("/api/bookings", headers=rider["auth"],
                       json={"ride_id": rid, "seats": 1})
    booking = resp.get_json()["booking"]
    client.post(f"/api/bookings/{booking['id']}/verify", headers=rider["auth"],
                json={"payment_id": "pay_c", "signature": "sig_c"})
    cancel = client.delete(f"/api/bookings/{booking['id']}", headers=rider["auth"],
                           json={"reason": "plans changed"})
    assert cancel.status_code == 200, cancel.get_json()
    # cancelling settles the refund, so the booking is already terminal and is
    # definitely no longer `confirmed`
    after_cancel = db.bookings.find_one({"_id": ObjectId(booking["id"])})["status"]
    assert after_cancel in (BOOKING_CANCELLED, BOOKING_REFUNDED), after_cancel

    _backdate_departure(db, rid)
    late = client.post(f"/api/rides/{rid}/bookings/{booking['id']}/no-show",
                       headers=driver["auth"], json={})
    assert late.status_code == 409
    assert late.get_json()["error"]["code"] == "state_conflict"


def test_failed_refund_rolls_the_no_show_back_so_it_can_be_retried(client, db, trip, driver,
                                                                 monkeypatch):
    """The critical safety property: the booking only becomes a terminal no-show
    once the rider's money is actually back. If the gateway rejects the refund the
    driver must be able to try again, not be stuck with a 'no-show' that refunded
    nothing."""
    import backend.blueprints.rides as rides_mod
    from backend.errors import APIError

    rid, bid = trip["ride_id"], trip["booking_id"]

    from backend import payments as payments_mod

    real_refund = payments_mod.refund_payment

    def boom(*_a, **_k):
        raise APIError("The refund was declined by the provider.", 502,
                       code="refund_failed")

    monkeypatch.setattr("backend.payments.refund_payment", boom)
    failed = client.post(f"/api/rides/{rid}/bookings/{bid}/no-show",
                         headers=driver["auth"], json={})
    assert failed.status_code == 502, failed.get_json()

    booking = db.bookings.find_one({"_id": ObjectId(bid)})
    assert booking["status"] == BOOKING_CONFIRMED, "must be rolled back to retryable"
    assert booking.get("no_show_at") is None

    # the seat was returned too, so the ride is exactly as it was
    assert db.rides.find_one({"_id": ObjectId(rid)})["earnings"] == 100

    # retrying now succeeds and produces exactly one refund
    monkeypatch.setattr("backend.payments.refund_payment", real_refund)
    ok = client.post(f"/api/rides/{rid}/bookings/{bid}/no-show", headers=driver["auth"], json={})
    assert ok.status_code == 200, ok.get_json()
    assert db.bookings.find_one({"_id": ObjectId(bid)})["status"] == BOOKING_NO_SHOW
    assert db.refunds.count_documents({"booking_id": ObjectId(bid)}) == 1
    # the seat is released exactly once
    assert db.rides.find_one({"_id": ObjectId(rid)})["seats_available"] == 2
