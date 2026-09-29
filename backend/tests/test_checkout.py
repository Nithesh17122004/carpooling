"""Checkout handoff: the config the browser receives to open Razorpay, and the
guarantees around what that config is allowed to contain.

The browser must be able to open a gateway sheet (it needs a publishable key id
and the order id) while remaining incapable of influencing the charge. These
tests pin both halves of that contract.
"""

import hashlib
import hmac
import json

import pytest
from bson import ObjectId

from backend.app import create_app
from backend.tests.conftest import make_ride


def _razorpay_app(monkeypatch, key_id="rzp_test_PUBLISHABLE", key_secret="super_secret_value"):
    """A razorpay-configured app whose SDK calls are stubbed out."""
    import backend.config as configmod

    monkeypatch.setattr(configmod.Config, "RAZORPAY_KEY_ID", key_id, raising=False)
    monkeypatch.setattr(configmod.Config, "RAZORPAY_KEY_SECRET", key_secret, raising=False)

    import backend.payments as paymod

    class _Orders:
        last = {}

        @staticmethod
        def create(payload):
            _Orders.last = payload
            return {"id": "order_TEST_0001", "amount": payload["amount"], "status": "created"}

    class _Payments:
        @staticmethod
        def fetch(pid):
            return {"id": pid, "status": "captured", "amount": 12000}

        @staticmethod
        def refund(pid, payload):
            return {"id": "rfnd_TEST_0001"}

    class _FakeClient:
        order = _Orders()
        payment = _Payments()

    # exposed so tests can assert on what was actually sent to the gateway
    paymod.test_gateway_orders = _Orders

    monkeypatch.setattr(paymod, "_razorpay_client", lambda: _FakeClient())

    app = create_app()
    app.config.update({
        "PAYMENT_PROVIDER": "razorpay",
        "RAZORPAY_KEY_ID": key_id,
        "RAZORPAY_KEY_SECRET": key_secret,
        "RAZORPAY_WEBHOOK_SECRET": "whsec_test_secret",
    })
    return app


def _book(app, db, driver, rider, vehicle, fare=120):
    c = app.test_client()
    ride = make_ride(c, driver["auth"], vehicle, fare=fare).get_json()["ride"]
    b = c.post("/api/bookings", headers=rider["auth"],
               json={"ride_id": ride["id"], "seats": 1})
    assert b.status_code == 201, b.get_json()
    return c, ride, b.get_json()


# ------------------------------------------------------------------- contents
def test_checkout_config_carries_everything_needed_and_nothing_secret(client, db, driver, rider, vehicle, monkeypatch):
    app = _razorpay_app(monkeypatch)
    _, _, body = _book(app, db, driver, rider, vehicle, fare=120)
    co = body["checkout"]

    assert co["provider"] == "razorpay"
    assert co["key_id"] == "rzp_test_PUBLISHABLE"
    assert co["order_id"] == "order_TEST_0001"
    assert co["currency"] == "INR"
    assert co["amount"] == 120
    assert co["booking_id"] == body["booking"]["id"]
    assert co["prefill"]["email"] == "rider@test.in"

    # the frozen split travels with the order so the UI can show it honestly
    assert co["breakdown"] == {
        "gross": 120, "platform_fee": 36, "driver_payout": 84,
        "commission_rate_percent": 30.0,
    }

    # and nothing secret is anywhere in the response
    blob = json.dumps(body)
    assert "super_secret_value" not in blob
    assert "RAZORPAY_KEY_SECRET" not in blob
    assert "rzp_test_PUBLISHABLE" in blob  # publishable id IS expected here


def test_amount_sent_to_gateway_is_the_server_amount(client, db, driver, rider, vehicle, monkeypatch):
    """A client cannot influence the charge: the order is created from the
    booking's server-side amount, in paise, and the split matches it."""
    import backend.payments as paymod

    app = _razorpay_app(monkeypatch)
    c, _, body = _book(app, db, driver, rider, vehicle, fare=275)
    co = body["checkout"]

    assert co["amount"] == 275
    assert co["breakdown"]["platform_fee"] == 82.5      # 30% of 275
    assert co["breakdown"]["driver_payout"] == 192.5
    # paise conversion for the gateway
    assert paymod.test_gateway_orders.last["amount"] == 27500
    assert paymod.test_gateway_orders.last["currency"] == "INR"
    assert paymod.test_gateway_orders.last["notes"]["booking_id"] == body["booking"]["id"]


def test_client_supplied_amount_is_ignored(client, db, driver, rider, vehicle, monkeypatch):
    """Booking with a forged amount/fee field changes nothing: the charge is
    derived from the ride fare, not from the request body."""
    app = _razorpay_app(monkeypatch)
    c = app.test_client()
    ride = make_ride(c, driver["auth"], vehicle, fare=120).get_json()["ride"]
    b = c.post("/api/bookings", headers=rider["auth"], json={
        "ride_id": ride["id"], "seats": 1,
        "amount": 1, "platform_fee": 0, "commission_rate_percent": 0,
        "gross": 1, "driver_net": 1,
    })
    assert b.status_code == 201, b.get_json()
    co = b.get_json()["checkout"]
    assert co["amount"] == 120
    assert co["breakdown"]["platform_fee"] == 36
    assert co["breakdown"]["commission_rate_percent"] == 30.0


# -------------------------------------------------------------------- verify
def test_successful_callback_still_requires_a_valid_server_signature(client, db, driver, rider, vehicle, monkeypatch):
    """The browser's 'payment.success' is not proof. Without the correct HMAC
    the server refuses to confirm, and nothing is ledgered."""
    app = _razorpay_app(monkeypatch)
    c, _, body = _book(app, db, driver, rider, vehicle)
    bid = body["booking"]["id"]

    bad = c.post(f"/api/bookings/{bid}/verify", headers=rider["auth"], json={
        "razorpay_order_id": "order_TEST_0001",
        "razorpay_payment_id": "pay_TEST_0001",
        "razorpay_signature": "forged",
    })
    assert bad.status_code == 402
    assert bad.get_json()["error"]["code"] == "payment_verify_failed"
    assert db.bookings.find_one({"_id": ObjectId(bid)})["status"] == "pending_payment"
    assert db.ledger_entries.count_documents({"booking_id": bid}) == 0


def test_valid_signature_confirms_and_ledger_posts_the_frozen_split(client, db, driver, rider, vehicle, monkeypatch):
    app = _razorpay_app(monkeypatch)
    c, _, body = _book(app, db, driver, rider, vehicle, fare=120)
    bid = body["booking"]["id"]

    order_id, payment_id = "order_TEST_0001", "pay_TEST_0001"
    sig = hmac.new(b"super_secret_value", f"{order_id}|{payment_id}".encode(),
                   hashlib.sha256).hexdigest()
    ok = c.post(f"/api/bookings/{bid}/verify", headers=rider["auth"], json={
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": sig,
    })
    assert ok.status_code == 200, ok.get_json()
    assert ok.get_json()["booking"]["status"] == "confirmed"

    rows = {r["entry_type"]: r["amount"]
            for r in db.ledger_entries.find({"booking_id": bid})}
    assert rows["PASSENGER_PAYMENT"] == 120
    assert rows["PLATFORM_FEE"] == -36
    assert rows["DRIVER_PAYABLE"] == 84


def test_verify_rejects_an_order_id_from_another_booking(client, db, driver, rider, vehicle, monkeypatch):
    """Cross-order replay: a signature valid for a different order is refused."""
    app = _razorpay_app(monkeypatch)
    c, _, body = _book(app, db, driver, rider, vehicle)
    bid = body["booking"]["id"]

    sig = hmac.new(b"super_secret_value", b"order_SOMEONE_ELSE|pay_X", hashlib.sha256).hexdigest()
    resp = c.post(f"/api/bookings/{bid}/verify", headers=rider["auth"], json={
        "razorpay_order_id": "order_SOMEONE_ELSE",
        "razorpay_payment_id": "pay_X",
        "razorpay_signature": sig,
    })
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "payment_verify_invalid"
    assert db.bookings.find_one({"_id": ObjectId(bid)})["status"] == "pending_payment"


def test_webhook_settles_even_when_the_browser_never_returns(client, db, driver, rider, vehicle, monkeypatch):
    """Closing the sheet must not lose the money: the signed webhook is the
    authoritative settlement path."""
    app = _razorpay_app(monkeypatch)
    c, _, body = _book(app, db, driver, rider, vehicle)
    bid = body["booking"]["id"]
    assert db.bookings.find_one({"_id": ObjectId(bid)})["status"] == "pending_payment"

    payload = json.dumps({
        "event": "payment.captured",
        "event_id": "evt_TEST_1",
        "payload": {"payment": {"entity": {"id": "order_TEST_0001", "amount": 12000}}},
    })
    sig = hmac.new(b"whsec_test_secret", payload.encode(), hashlib.sha256).hexdigest()
    r = c.post("/api/payments/webhook", headers={"X-Razorpay-Signature": sig},
               data=payload, content_type="application/json")
    assert r.status_code == 200, r.get_json()
    assert db.bookings.find_one({"_id": ObjectId(bid)})["status"] == "confirmed"
    assert db.ledger_entries.count_documents({"booking_id": bid}) == 3


def test_demo_checkout_config_omits_the_gateway_key(client, db, driver, rider, vehicle):
    """In the development demo provider there is no key to publish, and the
    browser must not be handed an empty one that looks broken."""
    ride = make_ride(client, driver["auth"], vehicle, fare=50).get_json()["ride"]
    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": 1})
    assert b.status_code == 201, b.get_json()
    co = b.get_json()["checkout"]
    assert co["provider"] == "demo"
    assert "key_id" not in co
    assert co["amount"] == 50
    assert co["breakdown"]["platform_fee"] == 15


def test_razorpay_without_a_key_id_fails_loudly(client, db, driver, rider, vehicle, monkeypatch):
    """A half-configured razorpay deployment must not hand the browser an
    unusable order: the booking rolls back and the rider is not charged.

    The internal 503 is deliberately not leaked to the client (it would tell an
    attacker how the deployment is misconfigured); what matters is that the
    failure is safe and complete -- no seat held, no booking, no payment row.
    """
    import backend.payments as paymod

    class _Orders:
        @staticmethod
        def create(payload):
            return {"id": "order_TEST_0002"}

    class _FakeClient:
        order = _Orders()

    monkeypatch.setattr(paymod, "_razorpay_client", lambda: _FakeClient())
    app = create_app()
    app.config.update({"PAYMENT_PROVIDER": "razorpay", "RAZORPAY_KEY_ID": ""})

    c = app.test_client()
    ride = make_ride(c, driver["auth"], vehicle, fare=90).get_json()["ride"]
    ride_id = ObjectId(ride["id"])
    b = c.post("/api/bookings", headers=rider["auth"],
               json={"ride_id": ride["id"], "seats": 1})
    assert b.status_code == 502, b.get_json()
    assert b.get_json()["error"]["code"] == "payment_unavailable"
    assert "no charge was made" in b.get_json()["error"]["message"]

    # the seat claim was rolled back, so the ride is still fully bookable
    again = c.get(f"/api/rides/{ride['id']}").get_json()["ride"]
    assert again["seats_available"] == again["seats_total"]
    # and no half-created rows were left behind
    assert db.bookings.count_documents({"ride_id": ride_id}) == 0
    assert db.payments.count_documents({"ride_id": ride_id}) == 0
    assert db.ledger_entries.count_documents({"booking_id": {"$exists": True}}) == 0
