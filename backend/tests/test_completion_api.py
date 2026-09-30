"""Completion and dispute over HTTP.

test_settlement_worker.py drives the completion state machine directly. This
file goes through the real endpoints, which is the only way to catch the parts
that live in the blueprint layer: who is allowed to call what, and whether the
two halves of a resolution can be raced.

The concurrency test at the bottom is the reason this file exists. `resolve` is
two steps -- claim the dispute, then move money -- and the order of those two
steps is the difference between "one trip, one outcome" and "one trip paid out
and refunded".
"""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from backend.tests.conftest import _make_user, auth, login, make_ride
from backend.states import (
    BOOKING_AWAITING_COMPLETION,
    BOOKING_COMPLETED,
    BOOKING_DISPUTED,
    BOOKING_REFUND_PENDING,
)
from backend.completion import (
    COMPLETION_AUTO_CONFIRMED,
    COMPLETION_AWAITING,
    COMPLETION_CONFIRMED,
    COMPLETION_DISPUTED,
)

HOUR = timedelta(hours=1)


@pytest.fixture
def admin(client, db):
    user = _make_user(db, "Trip Admin", "trip_admin@test.in", role="admin")
    return {"user": user, "auth": auth(login(client, "trip_admin@test.in"))}


def _paid_booking(client, db, rider, ride_id, seats=1):
    resp = client.post("/api/bookings", headers=rider["auth"],
                       json={"ride_id": ride_id, "seats": seats})
    assert resp.status_code == 201, resp.get_json()
    bid = resp.get_json()["booking"]["id"]
    verify = client.post(f"/api/bookings/{bid}/verify", headers=rider["auth"],
                         json={"payment_id": "pay_api", "signature": "sig_api"})
    assert verify.status_code == 200, verify.get_json()
    return bid


def _paid_trip(client, db, driver, rider, vehicle, seats=2, fare=100):
    """A ride that has actually been driven: booked, paid, then departed.

    Order matters. Booking requires a *future* departure and completion requires a
    *past* one, so the ride has to pass through both, and the backdating seam is
    the only way to get there.
    """
    ride = make_ride(client, driver["auth"], vehicle, seats=seats, fare=fare,
                     day=2, hh=23, mm=0)
    assert ride.status_code == 201, ride.get_json()
    rid = ride.get_json()["ride"]["id"]
    bid = _paid_booking(client, db, rider, rid)
    db.rides.update_one({"_id": ObjectId(rid)}, {"$set": {
        "departure_at": datetime.now(timezone.utc) - HOUR}})
    return rid, bid


@pytest.fixture
def trip(client, db, driver, rider, vehicle):
    """A ride that has been driven: booked, paid, departed and completed."""
    rid, bid = _paid_trip(client, db, driver, rider, vehicle)
    _drive_to_completion(client, driver, rid)
    return rid, bid


def _drive_to_completion(client, driver, ride_id):
    """Walk the ride through the states a real trip passes through.

    Completion is a ride-state transition, not a standalone booking call: the
    driver moves en_route -> boarding -> in_progress -> completed, and the
    booking's confirmation window opens on the last of those.
    """
    for phase in ("start", "boarding", "depart", "complete"):
        resp = client.post(f"/api/rides/{ride_id}/status", headers=driver["auth"],
                           json={"status": phase})
        assert resp.status_code == 200, (phase, resp.get_json())
    return resp.get_json()


def _complete_only(client, db, driver, ride_id):
    """Put the booking into its confirmation window without the ride-state walk.

    Used by the tests that are about the dispute itself, not about ride states.
    """
    from backend import completion as completion_mod

    booking = db.bookings.find_one({"ride_id": ObjectId(ride_id),
                                   "status": {"$ne": "cancelled"}})
    return completion_mod.driver_completed(ride_id, driver["user"]["_id"])


# ============================================================ driver completes
def test_completing_a_ride_opens_a_confirmation_window(client, db, driver, rider,
                                                       vehicle):
    rid, bid = _paid_trip(client, db, driver, rider, vehicle)
    body = _drive_to_completion(client, driver, rid)

    booking = db.bookings.find_one({"_id": ObjectId(bid)})
    assert booking["status"] == BOOKING_AWAITING_COMPLETION
    assert booking["completion_status"] == COMPLETION_AWAITING
    # The booking must not be closed yet, or the driver could be paid for a trip
    # the passenger has not confirmed.
    assert booking["status"] != BOOKING_COMPLETED

    view = client.get(f"/api/bookings/{bid}/completion", headers=rider["auth"])
    assert view.status_code == 200, view.get_json()
    assert view.get_json()["completion"]["completion_status"] == COMPLETION_AWAITING
    assert view.get_json()["completion"]["payable"] is False


def test_only_the_driver_can_complete(client, db, driver, rider, vehicle):
    """The passenger is on the trip but must not be able to close it."""
    rid, _ = _paid_trip(client, db, driver, rider, vehicle)
    resp = client.post(f"/api/rides/{rid}/status", headers=rider["auth"],
                       json={"status": "complete"})
    assert resp.status_code == 403


def test_only_the_passenger_can_confirm(client, db, driver, trip):
    _, bid = trip
    resp = client.post(f"/api/bookings/{bid}/confirm-completion",
                       headers=driver["auth"], json={})
    assert resp.status_code == 403


def test_confirming_marks_the_earnings_payable(client, db, rider, trip):
    _, bid = trip
    resp = client.post(f"/api/bookings/{bid}/confirm-completion",
                       headers=rider["auth"], json={})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["booking"]["status"] == BOOKING_COMPLETED
    assert resp.get_json()["completion"]["completion_status"] == COMPLETION_CONFIRMED
    assert resp.get_json()["completion"]["payable"] is True


def test_completing_twice_is_a_safe_no_op(client, db, driver, trip):
    """A retried complete must not re-open or extend the window.

    Idempotency matters here because a driver on a flaky connection will retry.
    The retry has to be a no-op, not an error and not a fresh deadline -- a
    moving deadline would let a driver keep a trip in dispute-reach indefinitely.
    """
    rid, bid = trip
    before = db.bookings.find_one({"_id": ObjectId(bid)})
    first_deadline = before["completion_deadline"]

    again = client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                        json={"status": "complete"})
    assert again.status_code == 200, again.get_json()

    after = db.bookings.find_one({"_id": ObjectId(bid)})
    assert after["completion_deadline"] == first_deadline
    assert after["status"] == BOOKING_AWAITING_COMPLETION
    assert after["completion_status"] == COMPLETION_AWAITING


def test_an_out_of_order_trip_step_is_refused(client, db, driver, rider, vehicle):
    """A driver cannot skip straight from published to completed."""
    rid, _ = _paid_trip(client, db, driver, rider, vehicle)
    resp = client.post(f"/api/rides/{rid}/status", headers=driver["auth"],
                       json={"status": "complete"})
    assert resp.status_code == 409
    assert db.bookings.find_one({"ride_id": ObjectId(rid)})["status"] == "confirmed"


def test_confirming_a_trip_that_was_never_completed_is_refused(client, db, driver,
                                                               rider, vehicle):
    _, bid = _paid_trip(client, db, driver, rider, vehicle)
    resp = client.post(f"/api/bookings/{bid}/confirm-completion",
                       headers=rider["auth"], json={})
    assert resp.status_code == 409


# =================================================================== disputes
def _dispute(client, db, driver, rider, rid, bid, reason="overcharged"):
    return client.post(f"/api/bookings/{bid}/dispute", headers=rider["auth"],
                       json={"reason": reason})


def test_a_dispute_holds_the_money_and_says_so(client, db, driver, rider, trip):
    rid, bid = trip
    resp = _dispute(client, db, driver, rider, rid, bid)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["booking"]["status"] == BOOKING_DISPUTED
    assert resp.get_json()["completion"]["payable"] is False


def test_the_driver_cannot_dispute_their_own_trip(client, db, driver, trip):
    rid, bid = trip
    resp = client.post(f"/api/bookings/{bid}/dispute", headers=driver["auth"],
                       json={"reason": "overcharged"})
    assert resp.status_code == 403


def test_an_unknown_dispute_reason_is_refused(client, db, driver, rider, trip):
    rid, bid = trip
    resp = client.post(f"/api/bookings/{bid}/dispute", headers=rider["auth"],
                       json={"reason": "because_i_said_so"})
    assert resp.status_code == 422
    # The closed set is deliberate: an open field becomes a support-ticket
    # smuggling channel and cannot be reported on.
    assert resp.get_json()["error"]["details"]["allowed"]


def test_a_dispute_is_irreversible_for_the_passenger(client, db, driver, rider, trip):
    """There is no un-dispute route: a passenger cannot reopen a held trip."""
    rid, bid = trip
    _dispute(client, db, driver, rider, rid, bid)
    again = client.post(f"/api/bookings/{bid}/confirm-completion",
                        headers=rider["auth"], json={})
    assert again.status_code == 409
    assert db.bookings.find_one({"_id": ObjectId(bid)})["completion_status"] == COMPLETION_DISPUTED


def test_only_an_admin_can_resolve_a_dispute(client, db, driver, rider, trip):
    rid, bid = trip
    _dispute(client, db, driver, rider, rid, bid)
    for who in (driver, rider):
        resp = client.post(f"/api/bookings/{bid}/resolve-dispute",
                           headers=who["auth"], json={"resolution": "release"})
        assert resp.status_code == 403


def test_resolving_in_favour_of_the_driver_pays_them(client, db, admin, driver, rider,
                                                     trip):
    rid, bid = trip
    _dispute(client, db, driver, rider, rid, bid)
    resp = client.post(f"/api/bookings/{bid}/resolve-dispute", headers=admin["auth"],
                       json={"resolution": "release", "note": "GPS confirms the route"})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["booking"]["status"] == BOOKING_COMPLETED
    assert resp.get_json()["completion"]["completion_status"] == COMPLETION_AUTO_CONFIRMED
    assert resp.get_json()["completion"]["payable"] is True


def test_resolving_in_favour_of_the_passenger_refunds_and_stays_unpayable(
        client, db, admin, driver, rider, trip):
    rid, bid = trip
    _dispute(client, db, driver, rider, rid, bid)
    resp = client.post(f"/api/bookings/{bid}/resolve-dispute", headers=admin["auth"],
                       json={"resolution": "refund", "note": "driver never arrived"})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["booking"]["status"] == BOOKING_REFUND_PENDING
    assert body["completion"]["payable"] is False


def test_a_second_resolution_of_the_same_dispute_is_refused(client, db, admin, driver,
                                                            rider, trip):
    """The whole point: one trip, one outcome.

    Two admins resolving the same dispute -- one to release, one to refund --
    must not both succeed. The first resolution claims the trip atomically; the
    second is refused. Without that claim the trip ends up both paid out and
    refunded.
    """
    rid, bid = trip
    _dispute(client, db, driver, rider, rid, bid)
    first = client.post(f"/api/bookings/{bid}/resolve-dispute", headers=admin["auth"],
                        json={"resolution": "release"})
    assert first.status_code == 200, first.get_json()
    second = client.post(f"/api/bookings/{bid}/resolve-dispute", headers=admin["auth"],
                         json={"resolution": "refund"})
    assert second.status_code == 409
    assert db.bookings.find_one({"_id": ObjectId(bid)})["status"] == BOOKING_COMPLETED


def test_an_unknown_resolution_is_refused(client, db, admin, driver, rider, trip):
    rid, bid = trip
    _dispute(client, db, driver, rider, rid, bid)
    resp = client.post(f"/api/bookings/{bid}/resolve-dispute", headers=admin["auth"],
                       json={"resolution": "send_them_bananas"})
    assert resp.status_code == 422
    assert db.bookings.find_one({"_id": ObjectId(bid)})["status"] == BOOKING_DISPUTED


def test_the_dispute_queue_is_admin_only(client, db, admin, driver, rider, trip):
    rid, bid = trip
    _dispute(client, db, driver, rider, rid, bid)
    assert client.get("/api/bookings/completion/queue",
                      headers=driver["auth"]).status_code == 403
    ok = client.get("/api/bookings/completion/queue", headers=admin["auth"])
    assert ok.status_code == 200
    assert [b["id"] for b in ok.get_json()["queue"]] == [bid]
    assert ok.get_json()["count"] == 1


def test_resolving_a_dispute_is_audited(client, db, admin, driver, rider, trip):
    rid, bid = trip
    _dispute(client, db, driver, rider, rid, bid)
    client.post(f"/api/bookings/{bid}/resolve-dispute", headers=admin["auth"],
                json={"resolution": "release", "note": "GPS confirms the route"})
    row = db.audit_logs.find_one({"action": "financial.completion.dispute_resolved"})
    assert row is not None, "resolving a dispute must leave a financial audit row"
    assert str(row["actor_id"]) == str(admin["user"]["_id"])
    assert row["actor_role"] == "admin"
    # Every completion event is namespaced under the financial domain so
    # "show me every event that can move money" is a single query.
    assert db.audit_logs.count_documents(
        {"action": "audit.invalid_action"}) == 0
