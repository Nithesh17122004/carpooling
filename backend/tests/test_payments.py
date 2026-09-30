"""Payments: demo provider order/verify correctness, server-side signature,
idempotent verify, webhook settlement semantics, and the real Razorpay
HMAC-verified webhook path (independently of the demo provider used elsewhere)."""

import hashlib
import hmac
import json

import pytest
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
class _FakeGateway:
    """Stands in for the Razorpay SDK.

    The webhook path is no longer allowed to believe the request body, so every
    test that settles a capture has to give the server something to fetch. This
    object is the *authoritative* side of the reconciliation: a test that wants
    a mismatch simply changes what it returns here, which is what makes "the
    amount in the webhook is ignored" an assertion rather than a hope.
    """

    def __init__(self, status="captured", amount=12000, currency="INR",
                 order_id="order_live_1", payment_id="pay_live_1",
                 raises=None):
        self._payment = {"id": payment_id, "status": status, "amount": amount,
                         "currency": currency, "order_id": order_id}
        self._raises = raises
        self.fetch_calls = []
        outer = self

        class _Payment:
            def fetch(self, pid, **kwargs):
                outer.fetch_calls.append(pid)
                if outer._raises is not None:
                    raise outer._raises
                return dict(outer._payment)

        class _Orders:
            def fetch(self, oid, **kwargs):
                if outer._raises is not None:
                    raise outer._raises
                return {"id": oid, "status": "paid",
                        "amount": outer._payment["amount"],
                        "currency": outer._payment["currency"]}

        class _Refunds:
            def create(self, payload, **kwargs):
                if outer._raises is not None:
                    raise outer._raises
                return {"id": "rfnd_fake_1", "status": "processed"}

            def __call__(self, payment_id, payload, **kwargs):
                if outer._raises is not None:
                    raise outer._raises
                return {"id": "rfnd_fake_1", "status": "processed"}

        self.payment = _Payment()
        self.order = _Orders()
        self.refund = _Refunds()
        # Razorpay reaches refunds through the payment resource: payment.refund(...)
        self.payment.refund = _Refunds()


def _install_gateway(monkeypatch, payments_mod, **kwargs):
    gateway = _FakeGateway(**kwargs)
    monkeypatch.setattr(payments_mod, "_razorpay_client", lambda: gateway)
    return gateway


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


def _razorpay_body(amount=12000, **entity):
    ent = {"id": "pay_live_1", "amount": amount, "currency": "INR",
           "order_id": "order_live_1"}
    ent.update(entity)
    return json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": ent}},
    })


def test_razorpay_webhook_settles_booking(client, db, driver, rider, vehicle,
                                          monkeypatch):
    from backend import payments as payments_mod
    _install_gateway(monkeypatch, payments_mod)
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


def test_razorpay_webhook_is_idempotent(client, db, driver, rider, vehicle,
                                        monkeypatch):
    from backend import payments as payments_mod
    _install_gateway(monkeypatch, payments_mod)
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


# ------------------------------------------------------------------ currency
def test_the_service_refuses_to_account_in_an_unknown_currency():
    """Money in two units cannot be summed, and the sum is what settlement
    reads. The unit is checked on the way in rather than assumed."""
    from backend.errors import APIError
    from backend.ledger import CURRENCY, assert_currency

    assert CURRENCY == "INR"
    assert assert_currency(None) == "INR"          # legacy rows with no field
    assert assert_currency("inr") == "INR"         # case is not a difference

    with pytest.raises(APIError) as exc:
        assert_currency("USD")
    assert exc.value.code == "unsupported_currency"
    assert exc.value.status == 422


def test_a_payment_order_carries_the_service_currency(client, db, driver, rider, vehicle):
    ride, booking, payment = _book(client, driver, rider, vehicle)
    doc = db.payments.find_one({"booking_id": ObjectId(booking["id"])})
    assert doc["currency"] == "INR"


def test_the_frozen_split_and_the_payment_agree_on_currency(client, db, driver, rider, vehicle):
    from backend.ledger import CURRENCY, quote_commission

    ride, booking, _payment = _book(client, driver, rider, vehicle, fare=200)
    doc = db.payments.find_one({"booking_id": ObjectId(booking["id"])})
    assert doc["currency"] == CURRENCY == quote_commission(200)["currency"]


def test_a_refund_of_a_foreign_currency_payment_is_refused(client, db, driver, rider, vehicle):
    """A refund recorded in a different unit than the capture is compared
    against the capture during reconciliation, where the numbers look
    comparable and are not."""
    from backend import payments as payments_mod
    from backend.errors import APIError

    ride, booking, _payment = _book(client, driver, rider, vehicle)
    doc = db.payments.find_one({"booking_id": ObjectId(booking["id"])})
    doc["currency"] = "USD"

    with pytest.raises(APIError) as exc:
        payments_mod.refund_payment(doc, "test")
    assert exc.value.code == "unsupported_currency"
    # Refused before the claim row was written, so no stuck `processing` refund.
    assert db.refunds.count_documents({"payment_id": doc["_id"]}) == 0


def test_a_payout_in_a_foreign_currency_is_refused(db, driver):
    from backend import payouts
    from backend.errors import APIError

    with pytest.raises(APIError) as exc:
        payouts.new_payout(driver["user"]["_id"], 100, currency="USD")
    assert exc.value.code == "unsupported_currency"
    # Nothing was reserved: the balance is untouched by a refused payout.
    assert db.payouts.count_documents({"user_id": driver["user"]["_id"]}) == 0


# ------------------------------------------------------------- webhook audit
def _webhook_audits(db, **match):
    """Audit rows for webhook deliveries, in the order they were written.

    Sorted by `_id` rather than left in natural order: two deliveries in one
    test can share a millisecond, and `created_at` alone would make the
    ordering a coin flip.
    """
    return list(db.audit_logs.find(dict({"action": "financial.webhook"}, **match))
                .sort("_id", 1))


def test_a_capture_webhook_is_audited(client, db, driver, rider, vehicle,
                                      monkeypatch):
    from backend import payments as payments_mod
    _install_gateway(monkeypatch, payments_mod)
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    body = _razorpay_body()
    sig = hmac.new(b"whsec_test_secret", body.encode(), hashlib.sha256).hexdigest()

    c.post("/api/payments/webhook", headers={"X-Razorpay-Signature": sig},
           data=body, content_type="application/json")

    rows = _webhook_audits(db, target_id="pay_live_1")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["actor_role"] == "webhook"
    assert row["outcome"] == "settled"
    assert row["meta"]["provider"] == "razorpay"
    assert row["meta"]["event"] == "payment.captured"
    # The audit has to carry what the gateway *verified*, not what it claimed.
    assert row["meta"]["reconciliation_status"] == "captured"


def test_a_redelivered_webhook_is_audited_as_a_duplicate(client, db, driver, rider,
                                                         vehicle, monkeypatch):
    """The second delivery is the one that proves deduplication works, and it
    is the one an operator needs to see when a gateway retries in a loop."""
    from backend import payments as payments_mod
    _install_gateway(monkeypatch, payments_mod)
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    body = json.dumps(dict(json.loads(_razorpay_body()), event_id="evt_abc123"))
    sig = hmac.new(b"whsec_test_secret", body.encode(), hashlib.sha256).hexdigest()
    headers = {"X-Razorpay-Signature": sig}

    first = c.post("/api/payments/webhook", headers=headers, data=body,
                   content_type="application/json")
    second = c.post("/api/payments/webhook", headers=headers, data=body,
                    content_type="application/json")
    assert first.get_json().get("duplicate") is False
    assert second.get_json().get("duplicate") is True

    outcomes = [r["outcome"] for r in _webhook_audits(db)]
    assert outcomes == ["settled", "duplicate"], outcomes
    assert _webhook_audits(db, outcome="duplicate")[0]["meta"]["event_id"] == "evt_abc123"


def test_a_webhook_for_an_unknown_payment_is_audited(client, db, driver, rider, vehicle):
    """Money moved at the gateway for a capture we have no record of. Nothing
    else in the system will ever mention it again."""
    app, c, _booking_id = _razorpay_env(db, driver, rider, vehicle)
    body = json.dumps({"event": "payment.captured",
                       "payload": {"payment": {"entity": {"id": "pay_unknown_9",
                                                          "amount": 12000}}}})
    sig = hmac.new(b"whsec_test_secret", body.encode(), hashlib.sha256).hexdigest()

    c.post("/api/payments/webhook", headers={"X-Razorpay-Signature": sig},
           data=body, content_type="application/json")

    rows = _webhook_audits(db, target_id="pay_unknown_9")
    assert len(rows) == 1, rows
    assert rows[0]["outcome"] == "unknown_payment"


def test_an_ignored_webhook_event_is_still_audited(client, db, driver, rider, vehicle):
    app, c, _booking_id = _razorpay_env(db, driver, rider, vehicle)
    body = json.dumps({"event": "refund.processed",
                       "payload": {"payment": {"entity": {"id": "pay_live_1",
                                                          "amount": 12000}}}})
    sig = hmac.new(b"whsec_test_secret", body.encode(), hashlib.sha256).hexdigest()

    resp = c.post("/api/payments/webhook", headers={"X-Razorpay-Signature": sig},
                  data=body, content_type="application/json")
    assert resp.get_json()["ignored"] == "refund.processed"
    assert [r["outcome"] for r in _webhook_audits(db)] == ["ignored"]


def test_the_demo_webhook_is_audited_too(client, db, driver, rider, vehicle):
    ride, booking, payment = _book(client, driver, rider, vehicle)
    resp = client.post("/api/payments/webhook", json={"order_id": payment["order_id"]})
    assert resp.status_code == 200, resp.get_json()
    rows = _webhook_audits(db, target_id=payment["order_id"])
    assert len(rows) == 1, rows
    assert rows[0]["meta"]["provider"] == "demo"
    assert rows[0]["outcome"] == "settled"


def test_the_demo_webhook_settles_the_booking(client, db, driver, rider, vehicle):
    """The demo webhook is a dev affordance, but a 200 that settles nothing is
    worse than a failure: it looks like it worked."""
    ride, booking, payment = _book(client, driver, rider, vehicle)
    pay = db.payments.find_one({"booking_id": ObjectId(booking["id"])})
    assert pay["status"] == "created"

    client.post("/api/payments/webhook", json={"order_id": payment["order_id"]})

    assert db.payments.find_one({"_id": pay["_id"]})["status"] == "success"
    assert db.bookings.find_one({"_id": ObjectId(booking["id"])})["status"] == "confirmed"
    assert db.ledger_entries.count_documents({"booking_id": str(booking["id"])}) == 3


# ------------------------------------------- authoritative reconciliation
def _post_capture(c, body):
    sig = hmac.new(b"whsec_test_secret", body.encode(), hashlib.sha256).hexdigest()
    return c.post("/api/payments/webhook", headers={"X-Razorpay-Signature": sig},
                  data=body, content_type="application/json")


def _assert_nothing_moved(db, booking_id):
    assert db.bookings.find_one({"_id": booking_id})["status"] == "pending_payment"
    assert db.payments.find_one({"booking_id": booking_id})["status"] == "created"
    assert db.ledger_entries.count_documents({"booking_id": str(booking_id)}) == 0


def test_a_webhook_that_understates_the_amount_cannot_cheat_the_booking(client, db,
                                                                      driver,
                                                                      rider,
                                                                      vehicle,
                                                                      monkeypatch):
    """The body says 1 rupee; the gateway says 120. The gateway wins.

    This is the exact shape of a tampered replay -- a valid signature over a
    doctored amount -- and signature verification alone would wave it through,
    because the signature covers the order/payment pair, not the amount. The
    capture still settles, because it *was* really 120, and the ledger is
    posted from the gateway's number. The doctored 1 is nowhere in the books.
    """
    from backend import payments as payments_mod
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    _install_gateway(monkeypatch, payments_mod, amount=12000)

    resp = _post_capture(c, _razorpay_body(amount=100))
    assert resp.status_code == 200
    assert resp.get_json()["settled"] is True, resp.get_json()
    assert db.bookings.find_one({"_id": booking_id})["status"] == "confirmed"

    # The books reflect 120. The tampered rupee left no trace anywhere.
    rec = db.payment_reconciliations.find_one({"razorpay_payment_id": "pay_live_1"})
    assert rec["captured_amount"] == 120.0
    from backend.ledger import _BK
    driver_entry = db.ledger_entries.find_one(
        {"booking_id": str(booking_id),
         "entry_type": _BK["driver_payable"]})
    assert driver_entry["amount"] == 84.0


def test_a_webhook_for_an_uncaptured_payment_is_refused(client, db, driver,
                                                        rider, vehicle,
                                                        monkeypatch):
    """`payment.authorized` is money held, not money taken. Settling on it
    confirms a booking the rider has not actually paid for."""
    from backend import payments as payments_mod
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    _install_gateway(monkeypatch, payments_mod, status="authorized")

    resp = _post_capture(c, _razorpay_body())
    assert resp.get_json()["settled"] is False
    _assert_nothing_moved(db, booking_id)


def test_a_webhook_in_the_wrong_currency_is_refused(client, db, driver, rider,
                                                    vehicle, monkeypatch):
    from backend import payments as payments_mod
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    _install_gateway(monkeypatch, payments_mod, currency="USD")

    resp = _post_capture(c, _razorpay_body(currency="INR"))
    assert resp.get_json()["settled"] is False
    _assert_nothing_moved(db, booking_id)


def test_a_payment_for_a_different_order_cannot_settle_this_booking(client, db,
                                                                    driver, rider,
                                                                    vehicle,
                                                                    monkeypatch):
    """A genuine capture, of a genuine amount -- but against someone else's
    order. Without the order check, one paying rider unlocks every booking."""
    from backend import payments as payments_mod
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    _install_gateway(monkeypatch, payments_mod, order_id="order_someone_else")

    resp = _post_capture(c, _razorpay_body(order_id="order_someone_else"))
    assert resp.get_json()["settled"] is False
    _assert_nothing_moved(db, booking_id)
    assert _webhook_audits(db)[0]["outcome"] == "order_mismatch"


def test_a_webhook_whose_order_contradicts_our_record_is_refused(client, db,
                                                                  driver, rider,
                                                                  vehicle,
                                                                  monkeypatch):
    """The payload names one order, the gateway names another. Either could be
    wrong, so neither is trusted and the capture is held back for review."""
    from backend import payments as payments_mod
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    _install_gateway(monkeypatch, payments_mod)

    resp = _post_capture(c, _razorpay_body(order_id="order_attacker"))
    assert resp.get_json()["settled"] is False
    _assert_nothing_moved(db, booking_id)
    assert _webhook_audits(db)[0]["outcome"] == "order_mismatch"


def test_an_unreachable_gateway_never_guesses(client, db, driver, rider,
                                              vehicle, monkeypatch):
    """A 502 from Razorpay is an outage, not a payment.

    Two things have to hold. The booking must not confirm, because nothing said
    it was paid. And the response must not be 2xx, because acknowledging
    delivery tells Razorpay to stop retrying -- the capture would then sit
    unconfirmed until a human noticed. The 502 puts it back on the retry
    schedule.
    """
    from backend import payments as payments_mod
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    _install_gateway(monkeypatch, payments_mod, raises=RuntimeError("502 from gateway"))

    resp = _post_capture(c, _razorpay_body())
    assert resp.status_code == 502, resp.get_json()
    _assert_nothing_moved(db, booking_id)
    assert _webhook_audits(db)[0]["outcome"] == "gateway_unavailable"


def test_a_webhook_arriving_before_the_callback_still_settles(client, db, driver,
                                                              rider, vehicle,
                                                              monkeypatch):
    """A fast payment, or a closed tab, delivers the webhook first. At that
    moment the internal payment has no provider_reference yet -- the row only
    carries the order id we gave Razorpay. Resolving by provider_reference alone
    drops the event and the customer pays for nothing.

    Razorpay copies our order `notes` onto the payment, so the booking id in the
    notes is the second, always-available link.
    """
    from backend import payments as payments_mod
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    # No provider_reference yet: exactly the webhook-first state.
    db.payments.update_one({"booking_id": booking_id},
                           {"$unset": {"provider_reference": ""}})
    _install_gateway(monkeypatch, payments_mod)

    body = _razorpay_body(notes={"booking_id": str(booking_id)})
    resp = _post_capture(c, body)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["settled"] is True, resp.get_json()
    assert db.bookings.find_one({"_id": booking_id})["status"] == "confirmed"
    assert db.ledger_entries.count_documents({"booking_id": str(booking_id)}) == 3


def test_reconciliation_is_recorded_once_however_often_it_is_attempted(client, db,
                                                                      driver, rider,
                                                                      vehicle,
                                                                      monkeypatch):
    """Callback, duplicate callback, webhook, webhook retry -- all four describe
    one capture and must leave one row, or the reconciliation log overstates how
    many times money moved."""
    from backend import payments as payments_mod
    from backend import payment_recon
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    _install_gateway(monkeypatch, payments_mod)
    pay = db.payments.find_one({"booking_id": booking_id})

    webhook = _post_capture(c, _razorpay_body()).get_json()

    # A genuine browser callback, repeated. Same capture, same three fields.
    sig = hmac.new(b"whsec_test_secret", b"order_live_1|pay_live_1",
                   hashlib.sha256).hexdigest()
    for _ in range(3):
        payment_recon.reconcile_razorpay_callback(
            db, pay, {"razorpay_payment_id": "pay_live_1",
                      "razorpay_order_id": "order_live_1",
                      "razorpay_signature": sig},
            client=payments_mod._razorpay_client(), secret="whsec_test_secret")

    rows = list(db.payment_reconciliations.find({"payment_id": pay["_id"]}))
    sources = sorted(r["verification_source"] for r in rows)
    # one for the webhook, one for the callback attempts (all identical)
    assert sources == ["browser_callback", "webhook"], rows
    assert webhook["settled"] is True


def test_a_reconciliation_records_the_verified_facts_not_the_claimed_ones(client,
                                                                          db,
                                                                          driver,
                                                                          rider,
                                                                          vehicle,
                                                                          monkeypatch):
    """The record is what an operator reads during a dispute, so it has to
    carry the frozen split and the gateway's numbers -- and it has to say which
    of the two paths verified it."""
    from backend import payments as payments_mod
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    _install_gateway(monkeypatch, payments_mod)

    _post_capture(c, _razorpay_body())

    pay = db.payments.find_one({"booking_id": booking_id})
    rec = db.payment_reconciliations.find_one(
        {"razorpay_payment_id": "pay_live_1"})
    assert rec["verification_source"] == "webhook"
    assert rec["signature_verified"] is True
    assert rec["razorpay_order_id"] == "order_live_1"
    assert rec["booking_id"] == booking_id
    assert rec["currency"] == "INR"
    assert rec["captured_amount"] == 120.0
    # The split is copied in from the frozen payment, so this row stands alone.
    assert rec["gross"] == 120.0
    assert rec["commission_rate_percent"] == 30
    assert rec["driver_net"] == 84.0
    assert rec["refund_id"] is None
    assert pay["provider_reference"] == "pay_live_1"


def test_a_capture_is_linked_to_the_refund_that_resolved_it(client, db, driver,
                                                           rider, vehicle,
                                                           monkeypatch):
    """Reconciled as captured, then refunded. Both facts have to be on the same
    row, or an operator has to join three collections by hand to answer 'did
    this customer get their money back'."""
    from backend import payments as payments_mod
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    _install_gateway(monkeypatch, payments_mod)
    body = _razorpay_body(event_id="evt_1")
    assert _post_capture(c, body).get_json()["settled"] is True

    # The rider cancels after the money landed: capture stands, refund follows.
    from backend.payments import refund_payment
    pay = db.payments.find_one({"booking_id": booking_id})
    refund = refund_payment(pay, "duplicate capture")
    from backend import payment_recon
    payment_recon.attach_refund_to_reconciliation(pay, refund["_id"])

    rec = db.payment_reconciliations.find_one({"razorpay_payment_id": "pay_live_1"})
    assert rec["refund_id"] == str(refund["_id"])
    # Linking twice must not move the record onto a later refund.
    payment_recon.attach_refund_to_reconciliation(pay, "later_refund")
    assert db.payment_reconciliations.find_one(
        {"razorpay_payment_id": "pay_live_1"})["refund_id"] == str(refund["_id"])


def test_webhook_audit_never_breaks_the_delivery_path(client, db, driver, rider, vehicle, monkeypatch):
    """A webhook is the only proof money moved. If recording it fails, the
    capture must still settle -- otherwise the audit sink takes down payments.
    """
    from backend import payments as payments_mod
    _install_gateway(monkeypatch, payments_mod)
    app, c, booking_id = _razorpay_env(db, driver, rider, vehicle)
    body = _razorpay_body()
    sig = hmac.new(b"whsec_test_secret", body.encode(), hashlib.sha256).hexdigest()

    from backend import audit as audit_mod
    original = audit_mod.record
    monkeypatch.setattr(audit_mod, "record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sink down")))

    resp = c.post("/api/payments/webhook", headers={"X-Razorpay-Signature": sig},
                  data=body, content_type="application/json")
    assert resp.status_code == 200, resp.get_json()
    assert db.bookings.find_one({"_id": booking_id})["status"] == "confirmed"
    assert db.payments.find_one({"booking_id": booking_id})["status"] == "success"
    monkeypatch.setattr(audit_mod, "record", original)
