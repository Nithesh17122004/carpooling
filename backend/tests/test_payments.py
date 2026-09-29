"""Payments: demo provider order/verify correctness, server-side signature,
idempotent verify, webhook settlement semantics, and the real Razorpay
HMAC-verified webhook path (independently of the demo provider used elsewhere)."""

import hashlib
import hmac
import json

from bson import ObjectId

from backend.app import create_app
from backend.tests.conftest import make_ride
from backend.timeutil import utc_now


def _book(client, driver, rider, vehicle, fare=120):
    r = make_ride(client, driver["auth"], vehicle, fare=fare)
    ride = r.get_json()["ride"]
    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": 1})
    assert b.status_code == 201, b.get_json()
    body = b.get_json()
    return ride, body["booking"], body["payment"]


def _verify(client, ride, rider, b_id, pr, sig):
    return client.post(f"/api/bookings/{b_id}/verify", headers=rider["auth"],
                       json={"payment_id": pr, "signature": sig})


def test_demo_order_flow(client, db, driver, rider, vehicle):
    ride, booking, payment = _book(client, driver, rider, vehicle)
    assert payment["order_id"]
    assert payment["amount"] == 120
    assert payment["status"] == "created"

    verify = _verify(client, ride, rider, booking["id"], "pay_demo123", "sig_demo123")
    assert verify.status_code == 200, verify.get_json()
    assert verify.get_json()["booking"]["status"] == "confirmed"
    pay = db.payments.find_one({"booking_id": ObjectId(booking["id"])})
    assert pay["status"] == "success"
    assert pay["amount"] == 120


def test_verify_is_idempotent(client, db, driver, rider, vehicle):
    ride, b, _pay = _book(client, driver, rider, vehicle)
    a = _verify(client, ride, rider, b["id"], "pay_x", "sig_x")
    btz = _verify(client, ride, rider, b["id"], "pay_x", "sig_x")
    assert a.status_code == btz.status_code == 200
    assert db.payments.count_documents({"booking_id": ObjectId(b["id"])}) == 1


def test_webhook_unknown_order_ignored(app, client, db, driver, rider, vehicle):
    h = {"X-Razorpay-Signature": "bogus"}
    r = client.post("/api/payments/webhook", headers=h, json={
        "event": "payment.captured", "payload": {"payment": {"entity": {"id": "nope"}}}})
    # unknown payment captcha -> ignored without a crash and without settling
    assert r.status_code in (400, 403, 404, 200)
    assert r.status_code != 500
    assert db.payments.count_documents({}) == 0


def test_ledger_rows_after_confirmed_payment(client, db, driver, rider, vehicle):
    ride, b, _pay = _book(client, driver, rider, vehicle)
    _verify(client, ride, rider, b["id"], "pay_l", "sig_l")
    oid = ObjectId(b["id"])
    assert db.payments.count_documents({"booking_id": oid}) == 1
    rows = list(db.ledger_entries.find({"booking_id": str(oid)}))
    kinds = {x["entry_type"] for x in rows}
    assert {"PASSENGER_PAYMENT", "PLATFORM_FEE", "DRIVER_PAYABLE"} <= kinds
    fee = next(x for x in rows if x["entry_type"] == "PLATFORM_FEE")
    # gross 120 -> commission is exactly 30% = 36, and is a debit to the platform
    assert fee["amount"] == -36
    payable = next(x for x in rows if x["entry_type"] == "DRIVER_PAYABLE")
    assert abs(payable["amount"] - 84) < 0.01
    for row in rows:
        assert row["currency"] == "INR"


# ----------------------------------------------------------------- razorpay webhook
def _razorpay_env(db, driver, rider, vehicle):
    """A throwaway app configured for the razorpay provider + an order/booking
    that looks like a live gateway capture (no SDK calls needed for settle)."""
    app = create_app()
    app.config.update({
        "PAYMENT_PROVIDER": "razorpay",
        "RAZORPAY_WEBHOOK_SECRET": "whsec_test_secret",
    })
    c = app.test_client()

    r = make_ride(c, driver["auth"], vehicle, fare=120)
    ride = r.get_json()["ride"]

    now = utc_now()
    booking = {
        "_id": ObjectId(),
        "ride_id": ObjectId(ride["id"]),
        "rider_id": rider["user"]["_id"],
        "owner_id": driver["user"]["_id"],
        "seats": 1, "amount": 120.0, "fare_per_seat": 120,
        "status": "pending_payment", "payment": None,
        "idempotency_key": None, "refundable": True,
        "created_at": now, "updated_at": now,
    }
    booking_id = booking["_id"]
    db.bookings.insert_one(booking)
    db.payments.insert_one({
        "booking_id": booking_id,
        "order_id": "order_live_1",
        "provider": "razorpay",
        "provider_reference": "pay_live_1",
        "amount": 120.0,
        "currency": "INR",
        "status": "created",
        "created_at": now, "updated_at": now,
    })
    return app, c, booking_id


def _razorpay_body():
    return json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_live_1", "amount": 12000}}},
    })


def test_razorpay_webhook_settles_booking(client, db, driver, rider, vehicle):
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    body = _razorpay_body()
    sig = hmac.new(b"whsec_test_secret", body.encode(), hashlib.sha256).hexdigest()

    resp = c.post("/api/payments/webhook",
                  headers={"X-Razorpay-Signature": sig},
                  data=body, content_type="application/json")
    assert resp.status_code == 200, resp.get_json()
    assert db.bookings.find_one({"_id": booking_id})["status"] == "confirmed"
    pay = db.payments.find_one({"booking_id": booking_id})
    assert pay["status"] == "success"
    assert db.ledger_entries.count_documents({"booking_id": str(booking_id)}) == 3


def test_razorpay_webhook_rejects_bad_signature(client, db, driver, rider, vehicle):
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    body = _razorpay_body()
    wrong = hmac.new(b"wrong_secret", body.encode(), hashlib.sha256).hexdigest()

    resp = c.post("/api/payments/webhook",
                  headers={"X-Razorpay-Signature": wrong},
                  data=body, content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "webhook_invalid"
    assert db.bookings.find_one({"_id": booking_id})["status"] == "pending_payment"


def test_razorpay_webhook_is_idempotent(client, db, driver, rider, vehicle):
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    body = _razorpay_body()
    sig = hmac.new(b"whsec_test_secret", body.encode(), hashlib.sha256).hexdigest()
    headers = {"X-Razorpay-Signature": sig}

    a = c.post("/api/payments/webhook", headers=headers, data=body,
               content_type="application/json")
    b = c.post("/api/payments/webhook", headers=headers, data=body,
               content_type="application/json")
    assert a.status_code == b.status_code == 200
    # exactly one success + one confirmed + one ledger set
    assert db.payments.count_documents({"booking_id": booking_id, "status": "success"}) == 1
    assert db.bookings.count_documents({"_id": booking_id, "status": "confirmed"}) == 1
    assert db.ledger_entries.count_documents({"booking_id": str(booking_id)}) == 3