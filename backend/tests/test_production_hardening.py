"""Production-mandate regression tests (P0 financial/concurrency/security).

Covers:
  Part 4  - webhook atomic settlement + event-id idempotency
  Part 6  - 0% refunds are a no-refund, never a full refund
  Part 7  - concurrent refund claims are atomic (gateway called once)
  Part 8  - cancel-vs-webhook race cannot re-confirm a cancelled booking
  Part 9  - concurrent payouts cannot pay the same balance twice
  Part 10 - refresh-token rotation is atomic and reuse revokes the family
  Part 11 - Google credential claims are verified, email never client-supplied
  Part 13 - IDOR guards on every protected resource
  Part 15 - upload validation (size / extension / traversal / masks)
  Part 22 - relevance match scoring + score sort
  Part 32 - recurring commute series (idempotent, conflict-tolerant)
  Part 38 - security batch (auth bypass, JWT tamper, injection, CSRF, manipulation)
"""

import base64
import hashlib
import hmac
import io
import json
import threading
from datetime import date, timedelta
from bson import ObjectId

import pytest

import backend.google_oauth as google_oauth_module
from backend.errors import APIError
from backend.tests.conftest import (
    _make_user,
    login,
    auth,
    token_of,
    make_ride,
    satisfy_other_publish_gates,
    PASSWORD,
)

_ONE_PX_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture
def admin(client, db):
    user = _make_user(db, "Admin Two", "admin2@test.in", role="admin")
    resp = login(client, "admin2@test.in")
    return {"user": user, "auth": auth(resp)}


def _book_pending(client, driver, rider, vehicle, fare=100, seats=1):
    r = make_ride(client, driver["auth"], vehicle, fare=fare, seats=2)
    ride = r.get_json()["ride"]
    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": seats})
    assert b.status_code == 201, b.get_json()
    return ride, b.get_json()["booking"], b.get_json()["payment"]


def _book_and_verify(client, driver, rider, vehicle, fare=100):
    ride, booking, _pay = _book_pending(client, driver, rider, vehicle, fare=fare)
    v = client.post(f"/api/bookings/{booking['id']}/verify", headers=rider["auth"], json={})
    assert v.status_code == 200, v.get_json()
    return ride, booking, v.get_json()["booking"]


# ------------------------------------------------------------------ Part 4
def _install_gateway(monkeypatch, *, amount=12000, currency="INR",
                     status="captured", order_id="order_h_1",
                     payment_id="pay_h_1"):
    """Stand in for the Razorpay SDK on the authoritative side.

    The webhook is reconciled against this object, not against the request body,
    so every test that settles a capture has to supply it. `amount` here is what
    the *gateway* says -- changing it is how a test simulates a real mismatch.
    """
    import backend.payments as paymod

    class _Payment:
        def fetch(self, pid, **kwargs):
            return {"id": pid, "status": status, "amount": amount,
                    "currency": currency, "order_id": order_id, "notes": {}}

        def refund(self, pid, payload, **kwargs):
            return {"id": "rfnd_hardening_1", "status": "processed"}

    class _Order:
        def create(self, payload, **kwargs):
            return {"id": "order_h_1", "status": "created",
                    "amount": payload.get("amount")}

    class _Client:
        payment = _Payment()
        order = _Order()

    client = _Client()
    monkeypatch.setattr(paymod, "_razorpay_client", lambda: client)
    return client


def _razorpay_capture_app(db, driver, rider, vehicle, event_id=True,
                          monkeypatch=None, **gateway):
    """Configured razorpay provider with a pending booking + created payment."""
    from backend.app import create_app
    from backend.timeutil import utc_now

    if monkeypatch is not None:
        _install_gateway(monkeypatch, **gateway)

    app = create_app()
    app.config.update({
        "PAYMENT_PROVIDER": "razorpay",
        "RAZORPAY_WEBHOOK_SECRET": "whsec_hardening_test",
        "RATE_LIMIT_DEFAULT": 100000,
        "RATE_LIMIT_AUTH": 100000,
        "RATE_LIMIT_STRICT": 100000,
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
    db.bookings.insert_one(booking)
    db.payments.insert_one({
        "booking_id": booking["_id"],
        "order_id": "order_h_1",
        "provider": "razorpay",
        "provider_reference": "pay_h_1",
        "amount": 120.0,
        "currency": "INR",
        "status": "created",
        "created_at": now, "updated_at": now,
    })
    return app, c, booking["_id"]


def _razorpay_post(c, event_id="evt_h_1", amount=12000, reference="pay_h_1"):
    body = json.dumps({
        "event_id": event_id,
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": reference, "amount": amount}}},
    })
    sig = hmac.new(b"whsec_hardening_test", body.encode(), hashlib.sha256).hexdigest()
    return c.post("/api/payments/webhook",
                  headers={"X-Razorpay-Signature": sig},
                  data=body, content_type="application/json")


def test_webhook_event_id_redelivery_is_already_processed(client, db, driver, rider, vehicle, monkeypatch):
    _app, c, booking_id = _razorpay_capture_app(db, driver, rider, vehicle,
                                                event_id=True, monkeypatch=monkeypatch)
    first = _razorpay_post(c, "evt_dedup_1")
    assert first.status_code == 200 and first.get_json().get("duplicate") is False
    # same event_id redelivered -> acknowledged, never settled twice
    again = _razorpay_post(c, "evt_dedup_1")
    assert again.status_code == 200
    assert again.get_json().get("duplicate") is True
    assert db.ledger_entries.count_documents({"booking_id": str(booking_id)}) == 3
    assert db.payments.count_documents({"booking_id": booking_id, "status": "success"}) == 1


def test_a_webhook_body_cannot_state_a_different_amount(client, db, driver, rider, vehicle, monkeypatch):
    """The body claims 999999 and the gateway says 120. The body is ignored.

    The gateway is the only thing that knows how much money moved, so the
    capture settles at 120 and the books reflect 120. Reading the number out of
    the request would post a driver payable of Rs 699,999.
    """
    _app, c, booking_id = _razorpay_capture_app(db, driver, rider, vehicle,
                                                monkeypatch=monkeypatch,
                                                amount=12000)
    resp = _razorpay_post(c, "evt_body_lies", amount=999999)
    assert resp.status_code == 200
    assert resp.get_json()["settled"] is True
    rec = db.payment_reconciliations.find_one({"razorpay_payment_id": "pay_h_1"})
    assert rec["captured_amount"] == 120.0
    assert db.ledger_entries.count_documents(
        {"booking_id": str(booking_id), "amount": 84.0}) == 1


def test_a_gateway_amount_that_disagrees_never_settles(client, db, driver, rider, vehicle, monkeypatch):
    """Now the *gateway* is the one that disagrees with what we froze. This is
    the case that must stop: the rider was charged 999999 for a 120 fare, and
    confirming the booking would hide a real overcharge behind a 'paid' state."""
    _app, c, booking_id = _razorpay_capture_app(db, driver, rider, vehicle,
                                                monkeypatch=monkeypatch,
                                                amount=999999)
    resp = _razorpay_post(c, "evt_real_mismatch")
    assert resp.status_code == 200
    assert resp.get_json()["settled"] is False
    assert db.bookings.find_one({"_id": booking_id})["status"] == "pending_payment"
    assert db.ledger_entries.count_documents({"booking_id": str(booking_id)}) == 0
    # Recorded, because an overcharge needs a human, not a silent 200.
    assert db.payment_reconciliations.count_documents(
        {"razorpay_payment_id": "pay_h_1"}) == 1


def test_an_uncaptured_payment_never_settles(client, db, driver, rider, vehicle, monkeypatch):
    _app, c, booking_id = _razorpay_capture_app(db, driver, rider, vehicle,
                                                monkeypatch=monkeypatch,
                                                status="authorized")
    resp = _razorpay_post(c, "evt_authorized")
    assert resp.get_json()["settled"] is False
    assert db.bookings.find_one({"_id": booking_id})["status"] == "pending_payment"
    assert db.ledger_entries.count_documents({"booking_id": str(booking_id)}) == 0


def test_an_unreachable_gateway_is_retried_not_dropped(client, db, driver, rider, vehicle, monkeypatch):
    """A gateway outage must answer non-2xx so Razorpay keeps retrying.

    A 200 here would be the quiet failure this whole path exists to prevent: the
    capture is never confirmed, no ledger is posted, and nothing retries.
    """
    import backend.payments as paymod

    class _Down:
        class payment:
            @staticmethod
            def fetch(pid, **kwargs):
                raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(paymod, "_razorpay_client", lambda: _Down())
    _app, c, booking_id = _razorpay_capture_app(db, driver, rider, vehicle)
    resp = _razorpay_post(c, "evt_gateway_down")
    assert resp.status_code == 502
    assert db.bookings.find_one({"_id": booking_id})["status"] == "pending_payment"
    assert db.ledger_entries.count_documents({"booking_id": str(booking_id)}) == 0


def test_concurrent_duplicate_webhooks_settle_once(client, db, driver, rider, vehicle, monkeypatch):
    """Two concurrent captures for the same payment -> one confirm + one ledger."""
    _app, c, booking_id = _razorpay_capture_app(db, driver, rider, vehicle,
                                                event_id=False,
                                                monkeypatch=monkeypatch)

    results = []

    def deliver(_):
        results.append(_razorpay_post(c, "evt_concurrent"))

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 4
    assert all(r.status_code == 200 for r in results)
    assert db.bookings.count_documents({"_id": booking_id, "status": "confirmed"}) == 1
    assert db.payments.count_documents({"booking_id": booking_id, "status": "success"}) == 1
    assert db.ledger_entries.count_documents({"booking_id": str(booking_id)}) == 3


# ------------------------------------------------------------------ Part 8
def test_webhook_cannot_reconfirm_cancelled_booking(client, db, driver, rider, vehicle):
    """A capture landing after the rider cancelled must refund, not confirm."""
    ride, booking, _pay = _book_pending(client, driver, rider, vehicle, fare=90)
    bid = booking["id"]

    cr = client.delete(f"/api/bookings/{bid}", headers=rider["auth"])
    assert cr.status_code == 200
    assert cr.get_json()["booking"]["status"] == "cancelled"

    # the gateway capture arrives late
    pay = db.payments.find_one({"booking_id": ObjectId(bid)})
    r = client.post("/api/payments/webhook", json={"order_id": pay["order_id"]})
    assert r.status_code == 200

    fresh = db.bookings.find_one({"_id": ObjectId(bid)})
    assert fresh["status"] == "cancelled"          # never re-confirmed
    assert db.payments.find_one({"_id": pay["_id"]})["status"] == "refunded"
    assert db.refunds.count_documents({"booking_id": ObjectId(bid)}) == 1
    # the booking was never settled -> nothing to reverse in the ledger
    assert db.ledger_entries.count_documents({"booking_id": str(bid), "entry_type": "PASSENGER_REFUND"}) == 0


# ------------------------------------------------------------------ Part 6
def test_zero_percent_refund_records_not_required(client, db, driver, rider, vehicle, monkeypatch):
    """A 0% refund policy closes the booking WITHOUT a provider refund, and
    records refund_percentage=0 / refund_status=NOT_REQUIRED."""
    ride, _b, booked = _book_and_verify(client, driver, rider, vehicle, fare=80)
    bid = booked["id"]

    monkeypatch.setattr("backend.blueprints.bookings._refund_policy",
                        lambda ride, now=None: (0, "Test: no refund", 0))

    cr = client.delete(f"/api/bookings/{bid}", headers=rider["auth"])
    assert cr.status_code == 200, cr.get_json()
    cb = cr.get_json()["booking"]
    assert cb["status"] == "cancelled"
    assert cb["refund_amount"] == 0.0
    assert cb["refund_percentage"] == 0
    assert cb["refund_status"] == "NOT_REQUIRED"
    assert db.refunds.count_documents({}) == 0          # gateway never called
    pay = db.payments.find_one({"booking_id": ObjectId(bid)})
    assert pay["status"] == "success"                   # untouched (nothing to refund)


def test_refund_payment_rejects_zero_amount(client, db, driver, rider, vehicle):
    """Defensive guard at the money layer: 0/negative refund amounts are a hard
    error and can never silently expand into a full (or 50%) refund."""
    from backend.payments import refund_payment
    import backend.blueprints.bookings as _bmod

    ride, _b, booked = _book_and_verify(client, driver, rider, vehicle, fare=50)
    pay = db.payments.find_one({"booking_id": ObjectId(booked["id"])})
    with client.application.app_context():
        for bad_amount in (0.0, -5.0):
            with pytest.raises(APIError) as exc:
                refund_payment(pay, "test zero", amount=bad_amount)
            assert exc.value.code == "invalid_refund_amount"
    assert db.refunds.count_documents({}) == 0
    assert db.payments.find_one({"_id": pay["_id"]})["status"] == "success"

    # 100% and 50% refunds carry the new metadata
    full = client.delete(f"/api/bookings/{booked['id']}", headers=rider["auth"])
    assert full.get_json()["booking"]["refund_status"] == "PROCESSED"


# ------------------------------------------------------------------ Part 7
def test_concurrent_cancels_issue_exactly_one_refund(client, db, driver, rider, vehicle):
    """Two simultaneous cancels of the same confirmed booking -> the refund
    claim is atomic: one refunds record, one provider-call equivalent."""
    ride, _b, booked = _book_and_verify(client, driver, rider, vehicle, fare=100)
    bid = booked["id"]

    codes = []

    def do_cancel(_):
        codes.append(client.delete(f"/api/bookings/{bid}", headers=rider["auth"]).status_code)

    threads = [threading.Thread(target=do_cancel, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(c in (200, 409) for c in codes), codes
    assert db.refunds.count_documents({"booking_id": ObjectId(bid)}) == 1
    assert db.payments.count_documents({"booking_id": ObjectId(bid), "status": "refunded"}) == 1
    assert db.ledger_entries.count_documents(
        {"booking_id": str(bid), "entry_type": "PASSENGER_REFUND"}) == 1


# ------------------------------------------------------------------ Part 9
def test_concurrent_payouts_cannot_double_pay(client, db, admin, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle, fare=100)
    ride = r.get_json()["ride"]
    b = client.post("/api/bookings", headers=rider["auth"], json={"ride_id": ride["id"], "seats": 1})
    v = client.post(f"/api/bookings/{b.get_json()['booking']['id']}/verify",
                    headers=rider["auth"], json={})
    assert v.status_code == 200
    driver_id = str(driver["user"]["_id"])

    barrier = threading.Barrier(4)
    results = []

    def create_payout(_):
        barrier.wait()
        resp = client.post("/api/admin/payouts", headers=admin["auth"], json={"user_id": driver_id})
        results.append(resp.status_code)

    threads = [threading.Thread(target=create_payout, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [c for c in results if c in (200, 201)]
    assert len(ok) == 1, results
    assert db.payouts.count_documents({"user_id": driver["user"]["_id"]}) == 1
    assert db.ledger_entries.count_documents({"entry_type": "DRIVER_PAYOUT"}) == 1
    from backend.ledger import driver_net_earnings
    assert driver_net_earnings(driver["user"]["_id"]) >= 0.0


# ------------------------------------------------------------------ Part 10
def test_refresh_rotation_is_atomic_and_reuse_revokes_family(client, db):
    """use_cookies=False: the ONLY cookie on the wire is the one we send, so the
    rotation race and the replay are deterministic (Werkzeug's cookie jar cannot
    sneak the already-rotated replacement into a sibling request)."""
    bare = client.application.test_client(use_cookies=False)
    _make_user(db, "Race User", "race@test.in")
    resp = bare.post("/api/auth/login", json={"email": "race@test.in", "password": PASSWORD})
    cookie = resp.headers.get("Set-Cookie", "")
    jti = cookie.split("rm_refresh=")[1].split(";")[0]
    uid = db.users.find_one({"email": "race@test.in"})["_id"]

    barrier = threading.Barrier(2)
    codes = []
    results = []

    def refresh_once(_):
        barrier.wait()
        r = bare.post("/api/auth/refresh",
                      headers={"Cookie": f"rm_refresh={jti}; Path=/api/auth"})
        codes.append(r.status_code)
        results.append(r.get_json())

    threads = [threading.Thread(target=refresh_once, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(codes) == [200, 401], codes
    loser = results[codes.index(401)]
    assert loser["error"]["code"] == "refresh_reused"
    # the loser's reuse detection revokes the WHOLE family, winner's rotation included
    assert db.refresh_tokens.count_documents({"user_id": ObjectId(uid), "revoked": False}) == 0
    # replaying the original (already rotated) token is also reuse
    replay = bare.post("/api/auth/refresh",
                       headers={"Cookie": f"rm_refresh={jti}; Path=/api/auth"})
    assert replay.status_code == 401
    assert replay.get_json()["error"]["code"] == "refresh_reused"


def test_the_replacement_token_exists_before_the_claim_is_visible(client, db, monkeypatch):
    """Why the rotation race above is not flaky: it is not.

    Reuse detection works in two steps -- the loser sees `replaced_by` on the
    presented row, then sweeps every unrevoked token in the family. That sweep
    can only catch the winner's replacement if the replacement already exists,
    and it can only see it if its `family` is already set, because the sweep
    filters on that field.

    Two threads make the ordering probabilistic, so this pins it directly: at
    the instant the claim commits, the replacement must be present and already
    labelled with its family. Move the insert back after the claim and this
    fails every run instead of once in twenty.
    """
    from pymongo.collection import Collection

    _make_user(db, "Order User", "order@test.in")
    resp = client.post("/api/auth/login", json={"email": "order@test.in",
                                                "password": PASSWORD})
    jti = resp.headers.get("Set-Cookie", "").split("rm_refresh=")[1].split(";")[0]
    uid = db.users.find_one({"email": "order@test.in"})["_id"]
    family = db.refresh_tokens.find_one({"jti": jti})["family"]

    seen = {}
    original = Collection.find_one_and_update

    def spy(self, filter, update, *a, **kw):
        result = original(self, filter, update, *a, **kw)
        if result is not None and update.get("$set", {}).get("replaced_by"):
            replacement = update["$set"]["replaced_by"]
            row = self.find_one({"jti": replacement})
            seen["present"] = row is not None
            seen["family_set"] = bool(row and row.get("family"))
        return result

    monkeypatch.setattr(Collection, "find_one_and_update", spy)

    from backend import security
    from backend.errors import APIError

    user = db.users.find_one({"_id": uid})
    with client.application.app_context():
        new_jti, got_family = security.rotate_refresh_token(jti, user)

        assert seen.get("present") is True, (
            "the replacement was inserted after the claim, so a concurrent reuse "
            "sweep would miss it and leave a live token behind")
        assert seen.get("family_set") is True, (
            "the replacement had no family at claim time, so the sweep's "
            "family filter would not match it")
        assert got_family == family
        assert db.refresh_tokens.find_one({"jti": new_jti})["revoked"] is False

        # And the happy path does not accumulate junk: one rotation leaves the
        # presented row revoked and one live replacement, not a spare.
        assert db.refresh_tokens.count_documents({"user_id": uid}) == 2

        # A second rotation attempt on the spent token is reuse, and cleans up
        # after itself rather than leaving a dangling replacement.
        with pytest.raises(APIError) as exc:
            security.rotate_refresh_token(jti, user)
    assert exc.value.code == "refresh_reused"
    assert db.refresh_tokens.count_documents({"user_id": uid,
                                              "revoked": False}) == 0


# ------------------------------------------------------------------ Part 11
class _FakeVerify:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def verify_oauth2_token(self, credential, _requests, audience, clock_skew_in_seconds=0):
        assert credential and str(audience) == "client-ish"
        if self.error:
            raise self.error
        return self.result


class _FakeProvider(google_oauth_module.GoogleProvider):
    """Real GoogleProvider.verify_credential logic, with a stubbed id_token lib."""

    def __init__(self, id_token=None, client_id="client-ish"):
        self.client_id = client_id
        self._requests = None
        self._id_token = id_token


def test_google_verifies_signature_issuer_audience_expiry(monkeypatch):
    """The underlying google-auth call enforces signature, issuer, audience and
    expiration; the wrapper must surface every failure with an explicit code.

    The audience is passed from GOOGLE_CLIENT_ID (never from the client) and
    unverifiable or expired credentials are rejected, so a tampered credential
    cannot mint a session.
    """
    import backend.google_oauth as go

    for error, code in [
        (ValueError("Invalid token"), "google_verify_failed"),
        (RuntimeError("refresh"), "google_verify_unavailable"),
    ]:
        fake = _FakeProvider(id_token=_FakeVerify(error=error))
        monkeypatch.setattr(go, "_provider", fake)
        with pytest.raises(APIError) as exc:
            go.get_google_provider().verify_credential("eyJtYW5nbGVkfdkfd")
        assert exc.value.code == code, (error, exc.value.code)


def test_google_rejects_unconfigured_and_unverified_email(monkeypatch):
    import backend.google_oauth as go

    unconfigured = _FakeProvider(id_token=_FakeVerify(), client_id="")
    monkeypatch.setattr(go, "_provider", unconfigured)
    with pytest.raises(APIError) as exc:
        go.get_google_provider().verify_credential("token")
    assert exc.value.code == "google_not_configured"

    fake = _FakeProvider(id_token=_FakeVerify(result={
        "sub": "g_1", "email": "a@x.in", "email_verified": False,
        "name": "A", "picture": ""}))
    monkeypatch.setattr(go, "_provider", fake)
    with pytest.raises(APIError) as exc:
        go.get_google_provider().verify_credential("token")
    assert exc.value.code == "google_email_unverified"


def test_google_extracts_email_only_from_verified_claims(client, db, monkeypatch):
    import backend.google_oauth as go

    claims = {"sub": "g_sub_42", "email": "verified@google.in",
              "email_verified": True, "name": "From Google",
              "picture": "https://p.example/a.png"}
    fake = _FakeProvider(id_token=_FakeVerify(result=claims))
    monkeypatch.setattr(go, "_provider", fake)

    resp = client.post("/api/auth/google", json={"credential": "fake-credential"})
    assert resp.status_code == 200, resp.get_json()
    # no email/name was sent by the client at all -> extracted from claims
    assert resp.get_json()["user"]["email"] == "verified@google.in"
    assert resp.get_json()["user"]["name"] == "From Google"
    assert db.users.find_one({"google_id": "g_sub_42", "email": "verified@google.in"})


def test_providers_endpoint_publishes_google_client_id_but_never_the_secret(client, monkeypatch):
    """The SPA needs the (public) client ID to render the GIS button, so the API
    publishes it. The client SECRET must never appear in any response."""
    import backend.config as cfgmod
    import backend.google_oauth as go

    client_id = "123.apps.googleusercontent.com"
    monkeypatch.setattr(cfgmod.Config, "GOOGLE_CLIENT_ID", client_id)
    monkeypatch.setattr(cfgmod.Config, "GOOGLE_CLIENT_SECRET", "super-secret-value")
    monkeypatch.setattr(go, "_provider", None)

    r = client.get("/api/auth/providers")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["providers"]["google"] is True
    assert body["providers"]["google_client_id"] == client_id
    assert body["providers"]["password"] is True
    assert "super-secret-value" not in r.get_data(as_text=True)

    # unconfigured deployments report it honestly so the UI can hide the button
    monkeypatch.setattr(cfgmod.Config, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(go, "_provider", None)
    body2 = client.get("/api/auth/providers").get_json()
    assert body2["providers"]["google"] is False
    assert body2["providers"]["google_client_id"] == ""


def test_providers_endpoint_is_public(client):
    assert client.get("/api/auth/providers").status_code == 200


# ------------------------------------------------------------------ Part 13
def test_idor_matrix(client, db, driver, rider, vehicle):
    """User B must never read or mutate user A's resources (403/404 alike)."""
    r = make_ride(client, driver["auth"], vehicle, fare=60, seats=3)
    ride = r.get_json()["ride"]
    ride_id = ride["id"]

    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride_id, "seats": 1})
    bid = b.get_json()["booking"]["id"]

    attacker = _make_user(db, "Intruder", "intruder@test.in")
    arb = login(client, "intruder@test.in")
    a_auth = auth(arb)

    # driver-owned ride: only the owner may mutate / see owner data
    assert client.patch(f"/api/rides/{ride_id}", headers=a_auth,
                        json={"fare_per_seat": 1}).status_code in (403, 404)
    assert client.delete(f"/api/rides/{ride_id}", headers=a_auth).status_code in (403, 404)
    assert client.post(f"/api/rides/{ride_id}/location", headers=a_auth,
                       json={"lat": 1, "lng": 2}).status_code in (403, 404)
    assert client.get(f"/api/rides/{ride_id}/location", headers=a_auth).status_code in (403, 404)

    # driver-only passenger list
    assert client.get(f"/api/bookings/for-ride/{ride_id}", headers=a_auth).status_code in (403, 404)

    # riders may not cancel each other's bookings / query by ride
    assert client.delete(f"/api/bookings/{bid}", headers=a_auth).status_code in (403, 404)
    assert client.post(f"/api/bookings/{bid}/verify", headers=a_auth,
                       json={}).status_code in (403, 404)

    # vehicles: owner-only read/update/delete and document upload/download
    vid = vehicle["id"]
    assert client.get(f"/api/vehicles/{vid}", headers=a_auth).status_code in (403, 404)
    assert client.patch(f"/api/vehicles/{vid}", headers=a_auth,
                        json={"vehicle_model": "stolen"}).status_code in (403, 404)
    assert client.delete(f"/api/vehicles/{vid}", headers=a_auth).status_code in (403, 404)
    up = client.post("/api/uploads/vehicle-doc", headers=a_auth,
                     data={"vehicle_id": vid,
                           "file": (io.BytesIO(_ONE_PX_PNG), "doc.png")},
                     content_type="multipart/form-data")
    assert up.status_code in (403, 404)
    assert client.get(f"/api/uploads/vehicle-doc/{vid}/dl", headers=a_auth).status_code == 404

    # admin endpoints remain staff-only (403), never reach the handler
    assert client.get("/api/admin/overview", headers=a_auth).status_code == 403
    assert client.post("/api/admin/payouts", headers=a_auth,
                       json={"user_id": "000000000000000000000000"}).status_code == 403


# ------------------------------------------------------------------ Part 15
def _upload(client, auth, vid, name, data, doc="dl"):
    return client.post("/api/uploads/vehicle-doc", headers=auth,
                       data={"vehicle_id": vid, "doc": doc,
                             "file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def _png():
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 400


def test_upload_rejects_double_extension_and_unknown_ext(client, db, driver, rider, vehicle):
    vid = vehicle["id"]
    for name, data in [
        ("scan.jpg.exe", _png()),
        ("scan.exe", _png()),
        ("scan.txt", _png()),
        ("scan.png.bak", _png()),
    ]:
        r = _upload(client, driver["auth"], vid, name, data)
        assert r.status_code == 422, (name, r.get_json())
        assert r.get_json()["error"]["code"] == "invalid_file"


def test_upload_rejects_magic_extension_mismatch(client, db, driver, rider, vehicle):
    vid = vehicle["id"]
    # PNG bytes masquerading as a PDF
    r = _upload(client, driver["auth"], vid, "doc.pdf", _png())
    assert r.status_code == 422
    assert r.get_json()["error"]["code"] == "invalid_file"
    # a fake PDF (magic ok) named as an image
    fake_pdf = b"%PDF-1.7\n" + b"MZ" + b"\x00" * 64
    r2 = _upload(client, driver["auth"], vid, "doc.png", fake_pdf)
    assert r2.status_code == 422


def test_upload_path_traversal_names_are_harmless(client, db, driver, rider, vehicle):
    """Traversal strings in the file name never reach the filesystem: stored
    keys are always server-generated under docs/<kind>/<user>/<random>."""
    vid = vehicle["id"]
    r = _upload(client, driver["auth"], vid, "../../etc/cron.d/vuln.png", _png())
    assert r.status_code == 200, r.get_json()
    vehicle_doc = db.vehicles.find_one({"_id": ObjectId(vid)})["dl_document"]
    assert vehicle_doc["key"].startswith("docs/dl/")
    assert ".." not in vehicle_doc["key"]


def test_upload_oversize_rejected(client, db, driver, rider, vehicle, monkeypatch):
    monkeypatch.setitem(client.application.config, "MAX_CONTENT_LENGTH", 600)
    vid = vehicle["id"]
    r = _upload(client, driver["auth"], vid, "big.png", _png() * 50)
    assert r.status_code in (413, 422)
    assert db.vehicles.find_one({"_id": ObjectId(vid)}).get("dl_document") is None


def test_upload_accepts_case_insensitive_extension(client, db, driver, rider, vehicle):
    r = _upload(client, driver["auth"], vehicle["id"], "SCAN.PNG", _png())
    assert r.status_code == 200, r.get_json()


# ------------------------------------------------------------------ Part 38
def test_auth_bypass_on_protected_routes(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle)
    ride_id = r.get_json()["ride"]["id"]
    for method, path, body in [
        ("post", "/api/bookings", {"ride_id": ride_id, "seats": 1}),
        ("get", "/api/bookings/mine", None),
        ("post", "/api/rides", {"vehicle_id": vehicle["id"]}),
        ("delete", "/api/bookings/000000000000000000000000", None),
        ("patch", "/api/auth/password", {"current_password": "x", "new_password": "Password1234"}),
    ]:
        resp = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert resp.status_code in (400, 401, 404, 405, 503), (method, path, resp.status_code)


def test_jwt_tamper_and_role_upgrade_rejected(client, db, driver, rider, vehicle):
    jwt_tok = client.get("/api/auth/me", headers=rider["auth"]).status_code
    assert jwt_tok == 200

    # flip signature / claim bytes -> token_invalid
    parts = token_of(login(client, "rider@test.in")).split(".")
    tampered = parts[0] + "." + parts[1] + "." + "c2ln" + "nje2pwn"
    import base64 as _b64
    import json as _json

    body_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    payload = _json.loads(_b64.urlsafe_b64decode(body_b64))
    payload["role"] = "admin"
    forged_payload = _b64.urlsafe_b64encode(_json.dumps(payload).encode()).rstrip(b"=").decode()
    forged = f"{parts[0]}.{forged_payload}.{parts[-1]}"

    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {tampered}"}).status_code == 401
    assert client.get("/api/admin/overview", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_injection_payloads_are_parameterised(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle)
    ride_id = r.get_json()["ride"]["id"]
    # Mongo operator injection in idempotency_key / string fields -> ignored
    b = client.post("/api/bookings", headers=rider["auth"], json={
        "ride_id": ride_id, "seats": 1,
        "idempotency_key": {"$ne": None},
    })
    assert b.status_code in (400, 201, 422), (b.status_code, b.get_json())
    # ObjectId injection in a path param
    bad = client.delete("/api/bookings/$where", headers=rider["auth"])
    assert bad.status_code in (400, 404)
    # No-SQL style query in a query-string role filter
    import urllib.parse
    resp = client.get("/api/admin/users?role[$ne]=admin", headers=rider["auth"])
    assert resp.status_code == 403  # blocked before reaching the handler


def test_cookie_only_request_cannot_authorize(client, db, driver, rider, vehicle):
    """The refresh cookie alone (classic CSRF vector) is never enough to mutate:
    state-changing routes require the Bearer access token."""
    resp = client.post("/api/auth/password",
                       json={"current_password": PASSWORD, "new_password": "Password1234"})
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "auth_required"


def test_verify_price_manipulation_rejected(client, db, driver, rider, vehicle):
    """Client-supplied amounts/statuses are ignored; truth comes from the order."""
    ride, booking, pay = _book_pending(client, driver, rider, vehicle, fare=120)
    v = client.post(f"/api/bookings/{booking['id']}/verify", headers=rider["auth"],
                    json={"payment_id": pay["order_id"], "signature": "sig",
                          "amount": 1.0, "status": "success"})
    # demo verifies without reading the tampered amount; still confirms
    assert v.status_code == 200
    pay_db = db.payments.find_one({"booking_id": ObjectId(booking["id"])})
    assert pay_db["amount"] == 120.0


# ------------------------------------------------------------------ Part 37
def test_reconcile_balanced_after_payment_then_refund(client, db, admin, driver, rider, vehicle):
    r = client.get("/api/admin/reconcile", headers=admin["auth"])
    assert r.status_code == 200
    assert r.get_json()["status"] == "balanced"
    assert r.get_json()["issues"] == []

    ride, _b, booked = _book_and_verify(client, driver, rider, vehicle, fare=100)
    r = client.get("/api/admin/reconcile", headers=admin["auth"])
    body = r.get_json()
    assert body["status"] == "balanced", body
    assert body["info"]["gmv"] == 100.0

    # full refund reverses the ledger -> books balanced again
    c = client.delete(f"/api/bookings/{booked['id']}", headers=rider["auth"])
    assert c.status_code == 200
    body = client.get("/api/admin/reconcile", headers=admin["auth"]).get_json()
    assert body["status"] == "balanced", body
    assert abs(body["info"]["refunds_processed"] - 1) == 0


def test_reconcile_flags_success_payment_without_ledger(client, db, admin, driver, rider, vehicle):
    """A success payment that never posted a ledger row must be surfaced."""
    pay = db.payments.insert_one({
        "booking_id": ObjectId(), "order_id": "po_ghost", "provider": "demo",
        "provider_reference": "po_ghost_ref", "amount": 50.0, "currency": "INR",
        "status": "success", "idempotency_key": "po_ghost_idem",
        "created_at": __import__("backend.timeutil", fromlist=["utc_now"]).utc_now(),
        "updated_at": __import__("backend.timeutil", fromlist=["utc_now"]).utc_now(),
    })
    r = client.get("/api/admin/reconcile", headers=admin["auth"])
    body = r.get_json()
    assert body["status"] == "attention"
    assert any(iss["kind"] == "success_payment_unledgered" for iss in body["issues"])
    db.payments.delete_one({"_id": pay.inserted_id})


# ------------------------------------------------------------------ Part 33
def test_admin_overview_extras_populated(client, db, admin, driver, rider, vehicle):
    """Run a safety report + SOS + block + rating, then confirm the overview
    surfaces every moderation/quality signal the dashboard needs."""
    client.post("/api/safety/report", headers=rider["auth"], json={
        "target_user_id": str(driver["user"]["_id"]), "reason": "unsafe driving",
        "details": "driving aggressively"})

    ride, _b, _pay = _book_pending(client, driver, rider, vehicle, fare=60)
    client.post("/api/safety/sos", headers=rider["auth"], json={"ride_id": ride["id"]})
    v = client.post(f"/api/bookings/{_b['id']}/verify", headers=rider["auth"], json={})
    assert v.status_code == 200, v.get_json()

    # ratings only open once the ride departs -> rewind the departure time
    from backend.timeutil import utc_now
    db.rides.update_one({"_id": ObjectId(ride["id"])},
                        {"$set": {"departure_at": utc_now() - timedelta(hours=1)}})
    rating = client.post("/api/ratings", headers=rider["auth"], json={
        "booking_id": _b["id"], "rated_user_id": str(driver["user"]["_id"]),
        "rating": 5, "review": "Great ride"})
    assert rating.status_code == 201, rating.get_json()

    resp = client.get("/api/admin/overview", headers=admin["auth"])
    assert resp.status_code == 200, resp.get_json()
    o = resp.get_json()["overview"]
    assert o["safety_reports"] == 1
    assert o["reviews"] == 1
    assert o["avg_rating"] == 5.0
    assert o["user_blocks"] == 0
    assert o["payments_success"] == 1
    assert o["bookings_refunded"] == 0
    assert o["sos_incidents"] == 1


# ------------------------------------------------------------------ Part 22
def test_search_ranks_by_match_score(client, db, driver, vehicle):
    """Relevance scoring: closer origin + cheaper + nearer departure wins,
    `sort=score` returns results ranked by match_score desc."""
    driver2 = _make_user(db, "Driver Two", "driver2@test.in")
    resp2 = login(client, "driver2@test.in")
    auth2 = auth(resp2)
    v2 = client.post("/api/vehicles", headers=auth2, json={
        "vehicle_type": "4-wheeler", "vehicle_number": "KA99XX0002",
        "vehicle_model": "Car Two", "seat_count": 4})
    assert v2.status_code == 201, v2.get_json()
    # Scoring test, not a KYC test -- clear all three publish gates so they stay
    # out of the way. Each gate has its own tests.
    satisfy_other_publish_gates(db, driver2["_id"], v2.get_json()["vehicle"]["id"])

    r1 = make_ride(client, driver["auth"], vehicle,
                   origin=("Near Pt", 12.90, 77.60), dest=("Dest", 13.00, 77.70),
                   hh=9, mm=0, fare=50)
    r2 = make_ride(client, auth2, v2.get_json()["vehicle"],
                   origin=("Far Pt", 13.10, 77.80), dest=("Dest", 13.00, 77.70),
                   hh=20, mm=0, fare=100)
    assert r1.status_code == 201 and r2.status_code == 201

    s = client.get("/api/rides/search?lat=12.90&lng=77.60&radius_km=40"
                   "&time_from=08:00&max_fare=150&sort=score")
    assert s.status_code == 200, s.get_json()
    body = s.get_json()
    assert body["total"] == 2
    scores = [it["match_score"] for it in body["data"]]
    assert all(isinstance(x, int) and 0 <= x <= 100 for x in scores)
    assert scores == sorted(scores, reverse=True)
    assert body["data"][0]["match_score"] > body["data"][1]["match_score"]
    assert body["data"][0]["origin"]["label"] == "Near Pt"


# ------------------------------------------------------------------ Part 32
def _next_monday():
    from datetime import date
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def test_recurring_creates_series_and_is_idempotent(client, db, driver, vehicle):
    start = _next_monday()
    payload = {
        "vehicle_id": vehicle["id"],
        "origin": {"label": "Home", "lat": 12.90, "lng": 77.60},
        "destination": {"label": "Office", "lat": 13.00, "lng": 77.70},
        "departure_time": "07:30", "timezone": "Asia/Kolkata",
        "days": ["mon", "wed", "fri"], "weeks": 2,
        "start_date": start.isoformat(),
        "seats_total": 3, "fare_per_seat": 80,
        "recurring_key": "commute-home-office-1",
    }
    r = client.post("/api/rides/recurring", headers=driver["auth"], json=payload)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["count"] == 6 and len(body["created"]) == 6
    assert body["skipped"] == []

    rides = list(db.rides.find({"recurring_key": "commute-home-office-1"}))
    assert len(rides) == 6
    assert {rd["status"] for rd in rides} == {"published"}
    for rd in rides:
        assert rd["departure_time"] == "07:30"
        assert rd["fare_per_seat"] == 80
        assert rd["seats_available"] == 3

    # same series retried -> every slot is a duplicate, nothing new created
    r2 = client.post("/api/rides/recurring", headers=driver["auth"], json=payload)
    assert r2.status_code == 201
    b2 = r2.get_json()
    assert b2["count"] == 0 and b2["created"] == []
    assert all(sk["reason"] == "duplicate" for sk in b2["skipped"])
    assert len(list(db.rides.find({"recurring_key": "commute-home-office-1"}))) == 6


def test_recurring_skips_conflicts_without_failing_batch(client, db, driver, vehicle):
    start = _next_monday()
    day_offset = (start - date.today()).days
    # block the first Monday 07:30 slot with a single overlapping ride
    clash = make_ride(client, driver["auth"], vehicle, hh=7, mm=30, day=day_offset)
    assert clash.status_code == 201

    payload = {
        "vehicle_id": vehicle["id"],
        "origin": {"label": "Home", "lat": 12.90, "lng": 77.60},
        "destination": {"label": "Office", "lat": 13.00, "lng": 77.70},
        "departure_time": "07:30", "timezone": "Asia/Kolkata",
        "days": ["mon", "wed", "fri"], "weeks": 2,
        "start_date": start.isoformat(),
        "seats_total": 3, "fare_per_seat": 80,
        "recurring_key": "commute-conflict-1",
    }
    r = client.post("/api/rides/recurring", headers=driver["auth"], json=payload)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["count"] == 5 and len(body["created"]) == 5
    conflict = [sk for sk in body["skipped"] if sk["reason"] == "conflict"]
    assert len(conflict) == 1
    assert conflict[0]["date"] == start.isoformat()


def test_recurring_validation(client, driver, vehicle):
    base = {
        "vehicle_id": vehicle["id"],
        "origin": {"label": "Home", "lat": 12.90, "lng": 77.60},
        "destination": {"label": "Office", "lat": 13.00, "lng": 77.70},
        "departure_time": "07:30", "timezone": "Asia/Kolkata",
        "days": ["mon"], "weeks": 2,
        "start_date": _next_monday().isoformat(),
        "seats_total": 3, "fare_per_seat": 80,
    }
    bad_days = dict(base, days=["funday"])
    r = client.post("/api/rides/recurring", headers=driver["auth"], json=bad_days)
    assert r.status_code == 422 and r.get_json()["error"]["code"] == "invalid_days"

    empty_days = dict(base, days=[])
    r = client.post("/api/rides/recurring", headers=driver["auth"], json=empty_days)
    assert r.status_code == 422

    big = dict(base, weeks=99)
    r = client.post("/api/rides/recurring", headers=driver["auth"], json=big)
    assert r.status_code == 422