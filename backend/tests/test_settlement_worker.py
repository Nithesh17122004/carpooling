"""Trip completion, disputes, and the settlement worker.

This file is the end-to-end proof of the money path, and most of it is
negative assertions. The question "can a driver be paid for a trip that did not
happen or is being complained about?" has to be un-answerable, and the only way
to show that is to try it from every direction and assert the refusal.

What is asserted, in order of how much money is at stake:

* a passenger confirmation releases earnings; a driver's tap alone does not;
* a dispute holds earnings indefinitely, and no amount of waiting releases them;
* auto-confirmation fires only after the window closes, and cannot overwrite a
  real answer;
* the worker pays out a confirmed trip, skips a disputed one, skips a driver
  with no payout account, and is safe to run twice;
* a payout stuck in `processing` is recovered to `failed`, never to `paid`.
"""

from datetime import timedelta

import pytest
from bson import ObjectId

from backend import completion, payouts
from backend.completion import (
    COMPLETION_AWAITING,
    COMPLETION_AUTO_CONFIRMED,
    COMPLETION_CONFIRMED,
    COMPLETION_DISPUTED,
)
from backend.payouts import PAYOUT_FAILED, PAYOUT_PENDING, PAYOUT_PROCESSING
from backend.states import (
    BOOKING_AWAITING_COMPLETION,
    BOOKING_COMPLETED,
    BOOKING_CONFIRMED,
    BOOKING_DISPUTED,
)
from backend.tests.conftest import make_ride
from backend.timeutil import utc_now


@pytest.fixture(autouse=True)
def ctx(app):
    with app.app_context():
        yield


# ----------------------------------------------------------------- fixtures
def _settled_booking(db, driver_id, gross=100.0, status=BOOKING_COMPLETED,
                     completion_status=COMPLETION_CONFIRMED, seats=1):
    """A paid booking with its DRIVER_PAYABLE already posted, as a real
    completed trip would have. Returns the booking document."""
    from backend.ledger import post_entry

    bid = ObjectId()
    pid = ObjectId()
    now = utc_now()
    fee = round(gross * 0.30, 2)
    db.payments.insert_one({
        "_id": pid, "booking_id": bid, "amount": gross, "currency": "INR",
        "platform_fee": fee, "driver_net": round(gross - fee, 2),
        "commission_rate_percent": 30.0, "status": "success",
        "created_at": now, "updated_at": now,
    })
    db.bookings.insert_one({
        "_id": bid, "owner_id": driver_id, "rider_id": ObjectId(),
        "ride_id": ObjectId(), "seats": seats, "amount": gross,
        "status": status, "completion_status": completion_status,
        "passenger_confirmed_at": now, "completed_at": now,
        "created_at": now, "updated_at": now,
    })
    post_entry(account_id=driver_id, account_type="driver",
               entry_type="DRIVER_PAYABLE", amount=round(gross * 0.70, 2),
               reference="TEST", meta={"booking_id": str(bid)})
    return db.bookings.find_one({"_id": bid})


# ------------------------------------------------- 1. what makes money payable
def test_a_confirmed_trip_is_payable(db, driver):
    booking = _settled_booking(db, driver["user"]["_id"])
    assert completion.is_payable(booking) is True
    assert payouts.outstanding_payable(driver["user"]["_id"]) == pytest.approx(70.0)


def test_a_trip_the_passenger_has_not_confirmed_is_not_payable(db, driver):
    """The core control: the driver's tap is not the passenger's confirmation."""
    _settled_booking(db, driver["user"]["_id"], status=BOOKING_AWAITING_COMPLETION,
                     completion_status=COMPLETION_AWAITING)
    uid = driver["user"]["_id"]

    # The fare was collected and ledgered...
    assert payouts.earned_payable(uid) == pytest.approx(70.0)
    # ...but it is held, so it cannot be paid out.
    assert payouts.held_payable(uid) == pytest.approx(70.0)
    assert payouts.outstanding_payable(uid) == pytest.approx(0.0)

    with pytest.raises(Exception) as exc:
        completion.assert_payable(db.bookings.find_one({"owner_id": uid}))
    assert exc.value.code == "completion_pending"


def test_a_disputed_trip_is_never_payable(db, driver):
    _settled_booking(db, driver["user"]["_id"], status=BOOKING_DISPUTED,
                     completion_status=COMPLETION_DISPUTED)
    uid = driver["user"]["_id"]
    assert payouts.held_payable(uid) == pytest.approx(70.0)
    assert payouts.outstanding_payable(uid) == pytest.approx(0.0)
    with pytest.raises(Exception) as exc:
        completion.assert_payable(db.bookings.find_one({"owner_id": uid}))
    assert exc.value.code == "trip_disputed"


def test_confirming_a_trip_releases_the_held_money(db, driver):
    bid = _settled_booking(db, driver["user"]["_id"],
                           status=BOOKING_AWAITING_COMPLETION,
                           completion_status=COMPLETION_AWAITING)["_id"]
    uid = driver["user"]["_id"]
    db.bookings.update_one({"_id": bid}, {"$set": {
        "rider_id": driver["user"]["_id"],
        "passenger_confirmed_at": None, "completed_at": None}})
    assert payouts.outstanding_payable(uid) == pytest.approx(0.0)

    # The returned document must be the one just written. pymongo's
    # find_one_and_update defaults to BEFORE, which would hand the caller a
    # booking that no longer exists in any collection -- a bug that is invisible
    # unless the return value itself is asserted.
    confirmed, idempotent = completion.passenger_confirmed(bid, driver["user"]["_id"])
    assert idempotent is False
    assert confirmed["completion_status"] == COMPLETION_CONFIRMED
    assert confirmed["status"] == BOOKING_COMPLETED
    assert completion.is_payable(confirmed)

    assert payouts.held_payable(uid) == pytest.approx(0.0)
    assert payouts.outstanding_payable(uid) == pytest.approx(70.0)

    # Re-confirming is idempotent and says so rather than raising.
    again, was_idempotent = completion.passenger_confirmed(bid, driver["user"]["_id"])
    assert was_idempotent is True
    assert again["completion_status"] == COMPLETION_CONFIRMED


# ---------------------------------------------------- 2. auto-confirm timing
def test_auto_confirm_waits_for_the_window_to_close(db, driver):
    bid = _settled_booking(db, driver["user"]["_id"],
                           status=BOOKING_AWAITING_COMPLETION,
                           completion_status=COMPLETION_AWAITING)["_id"]
    db.bookings.update_one({"_id": bid}, {"$set": {
        "completion_deadline": utc_now() + timedelta(hours=5)}})

    assert completion.auto_confirm(bid, now=utc_now()) is None
    assert db.bookings.find_one({"_id": bid})["completion_status"] == COMPLETION_AWAITING


def test_auto_confirm_fires_once_the_deadline_passes(db, driver):
    bid = _settled_booking(db, driver["user"]["_id"],
                           status=BOOKING_AWAITING_COMPLETION,
                           completion_status=COMPLETION_AWAITING)["_id"]
    db.bookings.update_one({"_id": bid}, {"$set": {
        "completion_deadline": utc_now() - timedelta(seconds=1)}})

    auto = completion.auto_confirm(bid, now=utc_now())
    assert auto is not None
    # The returned document reflects the write, not the pre-update state.
    assert auto["completion_status"] == COMPLETION_AUTO_CONFIRMED
    assert auto["status"] == BOOKING_COMPLETED
    booking = db.bookings.find_one({"_id": bid})
    assert booking["completion_status"] == COMPLETION_AUTO_CONFIRMED
    assert booking["status"] == BOOKING_COMPLETED
    # And the money is genuinely released.
    assert payouts.outstanding_payable(driver["user"]["_id"]) == pytest.approx(70.0)


def test_auto_confirm_never_overwrites_a_passenger_confirmation(db, driver):
    """The concurrency property: a real answer beats the timer, always."""
    bid = _settled_booking(db, driver["user"]["_id"],
                           status=BOOKING_AWAITING_COMPLETION,
                           completion_status=COMPLETION_AWAITING)["_id"]
    db.bookings.update_one({"_id": bid}, {"$set": {
        "rider_id": driver["user"]["_id"],
        "completion_deadline": utc_now() - timedelta(seconds=1)}})

    completion.passenger_confirmed(bid, driver["user"]["_id"])
    # A late worker pass must be a no-op, not an overwrite.
    assert completion.auto_confirm(bid, now=utc_now()) is None
    assert db.bookings.find_one({"_id": bid})["completion_status"] == COMPLETION_CONFIRMED


def test_auto_confirm_never_overwrites_a_dispute(db, driver):
    bid = _settled_booking(db, driver["user"]["_id"],
                           status=BOOKING_AWAITING_COMPLETION,
                           completion_status=COMPLETION_AWAITING)["_id"]
    db.bookings.update_one({"_id": bid}, {"$set": {
        "rider_id": driver["user"]["_id"],
        "completion_deadline": utc_now() - timedelta(seconds=1)}})

    completion.raise_dispute(bid, driver["user"]["_id"], "overcharged")
    assert completion.auto_confirm(bid, now=utc_now()) is None
    assert db.bookings.find_one({"_id": bid})["status"] == BOOKING_DISPUTED


def test_a_dispute_cannot_be_raised_after_the_window_closes(db, driver):
    bid = _settled_booking(db, driver["user"]["_id"],
                           status=BOOKING_COMPLETED,
                           completion_status=COMPLETION_CONFIRMED)["_id"]
    db.bookings.update_one({"_id": bid}, {"$set": {
        "rider_id": driver["user"]["_id"]}})
    with pytest.raises(Exception) as exc:
        completion.raise_dispute(bid, driver["user"]["_id"], "overcharged")
    assert exc.value.code == "completion_window_closed"


def test_dispute_reasons_are_a_closed_set(db, driver):
    bid = _settled_booking(db, driver["user"]["_id"],
                           status=BOOKING_AWAITING_COMPLETION,
                           completion_status=COMPLETION_AWAITING)["_id"]
    db.bookings.update_one({"_id": bid}, {"$set": {
        "rider_id": driver["user"]["_id"]}})
    with pytest.raises(Exception) as exc:
        completion.raise_dispute(bid, driver["user"]["_id"], "because-i-said-so")
    assert exc.value.code == "validation_error"


def test_a_dispute_is_only_resolvable_by_an_admin(db, driver):
    """The passenger cannot reopen a dispute into a payable state."""
    bid = _settled_booking(db, driver["user"]["_id"], status=BOOKING_DISPUTED,
                           completion_status=COMPLETION_DISPUTED)["_id"]
    db.bookings.update_one({"_id": bid}, {"$set": {
        "rider_id": driver["user"]["_id"]}})
    with pytest.raises(Exception) as exc:
        completion.raise_dispute(bid, driver["user"]["_id"], "other")
    assert exc.value.code == "completion_window_closed"

    # Releasing it is possible, but only through the admin path.
    released = completion.resolve_dispute(bid, "release", "admin-1")
    assert completion.is_payable(released)


# --------------------------------------------------------- 3. the worker
def test_the_worker_pays_a_confirmed_trip(db, driver):
    from backend import worker

    _settled_booking(db, driver["user"]["_id"])
    rows = worker.eligible_drivers()
    assert len(rows) == 1
    assert rows[0]["amount"] == pytest.approx(70.0)

    created = worker.create_payouts_pass()
    assert len(created) == 1
    payout = db.payouts.find_one({"_id": ObjectId(created[0])})
    assert payout["status"] == PAYOUT_PENDING
    assert payout["amount"] == pytest.approx(70.0)
    assert payout["period"]["auto"] is True


def test_running_the_worker_twice_does_not_pay_twice(db, driver):
    """The single most important property of an auto-payout worker."""
    from backend import worker

    _settled_booking(db, driver["user"]["_id"])
    assert len(worker.create_payouts_pass()) == 1
    assert len(worker.create_payouts_pass()) == 0
    assert db.payouts.count_documents({}) == 1


def test_the_worker_skips_a_disputed_trip(db, driver):
    from backend import worker

    _settled_booking(db, driver["user"]["_id"], status=BOOKING_DISPUTED,
                     completion_status=COMPLETION_DISPUTED)
    assert worker.eligible_drivers() == []
    assert worker.create_payouts_pass() == []
    assert db.payouts.count_documents({}) == 0


def test_the_worker_skips_a_driver_with_no_payout_account(db):
    from backend import worker
    from backend.tests.conftest import _make_user

    user = _make_user(db, "Unverified Driver", "unverified@test.in")
    _settled_booking(db, user["_id"])

    assert worker.eligible_drivers() == []
    assert worker.create_payouts_pass() == []
    assert db.payouts.count_documents({}) == 0

    # And a direct payout attempt is refused, not merely skipped.
    with pytest.raises(Exception) as exc:
        payouts.new_payout(user["_id"], 70.0)
    assert exc.value.code == "payout_onboarding_required"


def test_the_worker_never_settles_unconfirmed_earnings(db, driver):
    from backend import worker

    _settled_booking(db, driver["user"]["_id"], status=BOOKING_AWAITING_COMPLETION,
                     completion_status=COMPLETION_AWAITING)
    assert worker.eligible_drivers() == []
    assert worker.create_payouts_pass() == []


def test_a_stuck_processing_payout_is_recovered_to_failed_never_paid(db, driver):
    """A dead worker must not leave money in limbo, and must not guess 'paid'."""
    from backend import worker

    _settled_booking(db, driver["user"]["_id"])
    payout = payouts.new_payout(driver["user"]["_id"], 70.0)
    db.payouts.update_one({"_id": payout["_id"]}, {"$set": {
        "status": PAYOUT_PROCESSING,
        "processing_at": utc_now() - timedelta(hours=2)}})

    released = worker.reconcile_pass(ttl_seconds=60)
    assert released == [str(payout["_id"])]

    after = db.payouts.find_one({"_id": payout["_id"]})
    assert after["status"] == PAYOUT_FAILED
    assert after["failure_reason"] == "worker_claim_expired"


def test_a_fresh_processing_payout_is_left_alone(db, driver):
    """Reconcile must not steal a transfer that is still in flight."""
    from backend import worker

    _settled_booking(db, driver["user"]["_id"])
    payout = payouts.new_payout(driver["user"]["_id"], 70.0)
    db.payouts.update_one({"_id": payout["_id"]}, {"$set": {
        "status": PAYOUT_PROCESSING, "processing_at": utc_now()}})

    assert worker.reconcile_pass(ttl_seconds=600) == []
    assert db.payouts.find_one({"_id": payout["_id"]})["status"] == PAYOUT_PROCESSING


def test_a_full_worker_pass_never_marks_anything_paid(db, driver):
    """Even with nothing else going on, the worker has no path to `paid`."""
    from backend import worker

    _settled_booking(db, driver["user"]["_id"])
    summary = worker.run_once()
    assert summary["created"], summary
    assert db.payouts.count_documents({"status": "paid"}) == 0


def test_the_database_refuses_two_payouts_for_one_settlement_period(db, driver):
    """Paying a driver twice for one week's earnings is not recoverable.

    `create_payouts_pass` checks for an existing payout for the period and then
    inserts one. That is a check-then-insert, and two workers running at the same
    moment can both pass the check. The unique index on
    (user_id, period.auto_key) is what actually makes the second insert fail --
    the application check is only a fast path.

    Two settled trips are posted so there is enough outstanding for the *second*
    insert to be refused by the index rather than by the balance cap, which
    would otherwise mask what is being tested.
    """
    from pymongo.errors import DuplicateKeyError

    from backend import payouts as payouts_mod

    uid = driver["user"]["_id"]
    _settled_booking(db, uid, gross=100.0)
    _settled_booking(db, uid, gross=100.0)

    first = payouts_mod.new_payout(uid, 70.0,
                                   period={"auto_key": "2026-W15", "auto": True})
    assert first["period"]["auto_key"] == "2026-W15"

    with pytest.raises(DuplicateKeyError):
        payouts_mod.new_payout(uid, 70.0,
                               period={"auto_key": "2026-W15", "auto": True})

    assert db.payouts.count_documents({
        "user_id": uid, "period.auto_key": "2026-W15"}) == 1


def test_a_different_settlement_period_is_a_different_payout(db, driver):
    """The uniqueness is per period, not a blanket one-payout-per-driver rule."""
    from backend import payouts as payouts_mod

    uid = driver["user"]["_id"]
    _settled_booking(db, uid, gross=100.0)
    _settled_booking(db, uid, gross=100.0)
    payouts_mod.new_payout(uid, 70.0, period={"auto_key": "2026-W15", "auto": True})
    second = payouts_mod.new_payout(uid, 70.0,
                                    period={"auto_key": "2026-W16", "auto": True})
    assert second["period"]["auto_key"] == "2026-W16"
    assert db.payouts.count_documents({"user_id": uid}) == 2


def test_a_manual_payout_has_no_period_and_is_not_blocked(db, driver):
    """An admin's one-off payout must not collide with the weekly auto run."""
    from backend import payouts as payouts_mod

    uid = driver["user"]["_id"]
    _settled_booking(db, uid, gross=100.0)
    payouts_mod.new_payout(uid, 10.0)
    payouts_mod.new_payout(uid, 10.0)
    assert db.payouts.count_documents({
        "user_id": uid, "period.auto_key": None}) == 2


def test_a_second_worker_pass_does_not_pay_the_period_twice(db, driver):
    """The end-to-end version: two consecutive passes, one payout."""
    from backend import worker

    _settled_booking(db, driver["user"]["_id"])
    first = worker.create_payouts_pass()
    assert first
    second = worker.create_payouts_pass()
    assert second == []
    assert db.payouts.count_documents({
        "user_id": driver["user"]["_id"], "period.auto_key": {
            "$exists": True}}) == 1
