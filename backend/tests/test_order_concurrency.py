"""Order-creation concurrency.

The guarantee under test: creating an order for a booking is idempotent, and
concurrently asking for one produces EXACTLY ONE order at the gateway. The
pre-fix shape (call the gateway, then insert) passed every single-threaded test
while still creating a live, uncancellable orphan order on every double-click,
which is precisely the class of bug a unit test written after the fact misses.
"""

import threading
import uuid

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from backend.errors import APIError
from backend.tests.conftest import make_ride


def _booking(client, db, driver, rider, vehicle, fare=100):
    ride = make_ride(client, driver["auth"], vehicle, fare=fare)
    assert ride.status_code == 201, ride.get_json()
    rid = ride.get_json()["ride"]["id"]
    resp = client.post("/api/bookings", headers=rider["auth"],
                       json={"ride_id": rid, "seats": 1})
    assert resp.status_code == 201, resp.get_json()
    return db.bookings.find_one({"_id": ObjectId(resp.get_json()["booking"]["id"])})


@pytest.fixture
def fake_razorpay(app, monkeypatch):
    """Point payments at a fake gateway that records every call.

    Returns a small handle so a test can inspect `calls` or swap in a failing
    `create` to exercise the gateway-error path.
    """
    from backend import payments as payments_mod

    class Gateway:
        def __init__(self):
            self.calls = []
            self.lock = threading.Lock()
            self.counter = {"n": 1}

        def create(self, payload):
            with self.lock:
                self.calls.append(payload)
                self.counter["n"] += 1
                return {"id": f"order_TEST_{self.counter['n']}",
                        "amount": payload["amount"]}

        def fail(self, exc):
            """Make the next gateway calls raise instead of returning an order."""
            def _create(_payload):
                raise exc

            self.create = _create

        def succeed(self, order_id):
            def _create(payload):
                with self.lock:
                    self.calls.append(payload)
                return {"id": order_id, "amount": payload["amount"]}

            self.create = _create

    gateway = Gateway()

    class FakeClient:
        def __init__(self, gw):
            self.order = gw

    monkeypatch.setattr(payments_mod, "_razorpay_client", lambda: FakeClient(gateway))
    monkeypatch.setattr(payments_mod, "_provider", lambda: "razorpay")
    monkeypatch.setattr(payments_mod, "_idp_key", lambda: uuid.uuid4().hex)
    monkeypatch.setattr(payments_mod, "assert_checkout_ready", lambda: None)
    # The publishable KEY_ID is required to build a Razorpay checkout payload
    # (it identifies the app, it cannot move money), so a fake-gateway test must
    # supply one. Without it every booking here 502s on `payments_unconfigured`
    # before the concurrency logic under test is ever reached.
    monkeypatch.setitem(app.config, "RAZORPAY_KEY_ID", "rzp_test_fake_key")
    return gateway


def test_a_booking_can_never_have_two_payment_rows(app, client, db, driver, rider, vehicle,
                                                  fake_razorpay):
    """The unique booking_id index is the real mutex, not application logic."""
    from backend.payments import create_order

    booking = _booking(client, db, driver, rider, vehicle)
    with app.test_request_context():
        create_order(booking, 1, 100)
        with pytest.raises(DuplicateKeyError):
            db.payments.insert_one({"booking_id": booking["_id"], "order_id": "dup",
                                    "status": "created", "amount": 100})
    assert db.payments.count_documents({"booking_id": booking["_id"]}) == 1


def test_creating_the_order_twice_reuses_the_first(app, db, client, driver, rider, vehicle,
                                                  fake_razorpay):
    """Serial retry is fully idempotent: no second gateway call, no error."""
    from backend import payments as payments_mod

    booking = _booking(client, db, driver, rider, vehicle)
    with app.test_request_context():
        first = payments_mod.create_order(booking, 1, 100)
        second = payments_mod.create_order(booking, 1, 100)

    assert len(fake_razorpay.calls) == 1
    assert first["order_id"] == second["order_id"]
    assert db.payments.count_documents({"booking_id": booking["_id"]}) == 1


def test_concurrent_order_creation_calls_the_gateway_once(app, db, client, driver, rider,
                                                          vehicle, fake_razorpay):
    """N threads race to create an order for ONE booking. Exactly one gateway
    call may happen, and every caller must end up with that same order."""
    from backend import payments as payments_mod

    booking = _booking(client, db, driver, rider, vehicle)
    workers, results, errors = 8, [], []
    barrier = threading.Barrier(workers)

    with app.test_request_context():
        def worker():
            barrier.wait()
            try:
                results.append(payments_mod.create_order(booking, 1, 100))
            except APIError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert len(fake_razorpay.calls) == 1, f"gateway was called {len(fake_razorpay.calls)}x"
    # every successful caller got the SAME order; losers were told to retry
    assert results, "at least one caller must succeed"
    assert {r["order_id"] for r in results} == {"order_TEST_2"}
    assert all(e.code == "payment_order_in_progress" for e in errors), \
        sorted({e.code for e in errors})
    assert len(results) + len(errors) == workers
    assert db.payments.count_documents({"booking_id": booking["_id"]}) == 1


def test_a_failed_gateway_call_releases_the_claim_for_an_immediate_retry(app, db, client,
                                                                        driver, rider,
                                                                        vehicle,
                                                                        fake_razorpay):
    """If the gateway call fails the booking must not be stuck 'creating'
    forever -- the next attempt has to be able to start immediately."""
    from backend import payments as payments_mod

    # The booking is created while the gateway still works, THEN the gateway is
    # broken, so the failure under test is the order call and not the booking.
    booking = _booking(client, db, driver, rider, vehicle)
    # drop the order the booking flow created, so create_order has to claim
    db.payments.delete_many({"booking_id": booking["_id"]})
    fake_razorpay.fail(RuntimeError("gateway timeout"))

    with app.test_request_context():
        with pytest.raises(APIError) as first:
            payments_mod.create_order(booking, 1, 100)
        assert first.value.code == "payment_order_failed"

        row = db.payments.find_one({"booking_id": booking["_id"]})
        assert row["order_id"] is None
        assert row["status"] == "failed"

    # the retry works straight away, without waiting out the claim TTL
    fake_razorpay.succeed("order_RETRY_1")
    with app.test_request_context():
        second = payments_mod.create_order(booking, 1, 100)
    assert second["order_id"] == "order_RETRY_1"
    assert db.payments.count_documents({"booking_id": booking["_id"]}) == 1
