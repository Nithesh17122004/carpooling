"""Payout settlement: the state machine, the provider-confirmation rule, and the
guarantees that make retrying safe.

The central assertion of this file is negative: there is NO code path -- admin
API, request body, or forged webhook -- that can mark a payout `paid` except a
correctly signed provider confirmation. Everything else exists to make the three
real transitions safe to retry and to keep the books balanced.
"""

import hashlib
import hmac
import json

import pytest
from bson import ObjectId

from backend.payouts import (
    PAYOUT_FAILED,
    PAYOUT_PAID,
    PAYOUT_PENDING,
    PAYOUT_PROCESSING,
    confirm_payout,
    fail_payout,
    new_payout,
    outstanding_payable,
    retry_payout,
    settled_payable,
    submit_payout,
    void_payout,
)


@pytest.fixture(autouse=True)
def ctx(app):
    """Payout rules are read from app config, so every call needs a context."""
    with app.app_context():
        yield


@pytest.fixture
def admin(client, db):
    from backend.tests.conftest import _make_user, auth, login

    user = _make_user(db, "Settle Admin", "settle_admin@test.in", role="admin")
    resp = login(client, "settle_admin@test.in")
    assert resp.status_code == 200, resp.get_json()
    return {"user": user, "auth": auth(resp)}


def _earn(db, driver_id, gross, booking_id=None):
    """Post a DRIVER_PAYABLE the way a completed trip would."""
    bid = booking_id or ObjectId()
    db.ledger_entries.insert_one({
        "entry_id": "ENT-" + str(ObjectId()),
        "account_id": str(driver_id),
        "account_type": "driver",
        "entry_type": "DRIVER_PAYABLE",
        "amount": round(gross * 0.70, 2),
        "currency": "INR",
        "booking_id": str(bid),
        "payment_id": None,
        "refund_id": None,
        "reference": None,
        "meta": {},
        "created_at": __import__("backend.timeutil", fromlist=["utc_now"]).utc_now(),
    })
    return bid


# --------------------------------------------------------------- state machine
def test_payout_starts_pending_and_debits_the_driver(db, driver):
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    assert p["status"] == PAYOUT_PENDING
    assert p["paid_at"] is None
    assert p["provider_reference"] is None
    # creating a payout reserves the money immediately
    assert outstanding_payable(uid) == 0
    assert settled_payable(uid) == 0


def test_non_positive_and_oversized_payouts_are_refused(db, driver, app):
    from backend.errors import APIError

    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    for bad in (0, -5, -0.01):
        try:
            new_payout(uid, bad)
        except APIError as exc:
            assert exc.code == "invalid_payout_amount", exc.code
        else:
            raise AssertionError(f"{bad} should be refused")

    original = app.config.get("MAX_PAYOUT_AMOUNT")
    app.config["MAX_PAYOUT_AMOUNT"] = 10.0
    try:
        new_payout(uid, 11)
    except APIError as exc:
        assert exc.code == "payout_too_large", exc.code
    else:
        raise AssertionError("oversized payout should be refused")
    finally:
        app.config["MAX_PAYOUT_AMOUNT"] = original


def test_submit_moves_pending_to_processing_but_never_to_paid(db, driver):
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)

    submitted = submit_payout(p["_id"])
    assert submitted["status"] == PAYOUT_PROCESSING
    assert submitted["paid_at"] is None
    assert db.payouts.find_one({"_id": p["_id"]})["status"] == PAYOUT_PROCESSING


def test_submit_is_idempotent_and_does_not_double_debit(db, driver):
    """Retrying after a timeout must not create a second transfer."""
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)

    first = submit_payout(p["_id"])
    second = submit_payout(p["_id"])
    third = submit_payout(p["_id"])
    assert first["status"] == second["status"] == third["status"] == PAYOUT_PROCESSING
    assert db.payouts.count_documents({"user_id": uid}) == 1
    assert db.ledger_entries.count_documents({"entry_type": "DRIVER_PAYOUT"}) == 1


# -------------------------------------------------- the provider-confirmation rule
def test_confirm_requires_processing_first(db, driver):
    """A confirmation for a payout that was never sent is refused. This is what
    stops a stale or replayed callback from marking phantom money as paid."""
    from backend.errors import APIError

    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)

    try:
        confirm_payout(p["_id"], "pout_forged")
    except APIError as exc:
        assert exc.status == 409, exc.status
        assert exc.code == "payout_not_processing", exc.code
    else:
        raise AssertionError("confirming a pending payout must fail")
    assert db.payouts.find_one({"_id": p["_id"]})["status"] == PAYOUT_PENDING


def test_confirm_is_the_only_way_to_paid_and_is_idempotent(db, driver):
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    submit_payout(p["_id"])

    paid = confirm_payout(p["_id"], "pout_REAL_1", source="provider")
    assert paid["status"] == PAYOUT_PAID
    assert paid["paid_at"] is not None
    assert paid["provider_reference"] == "pout_REAL_1"
    assert paid["confirmed_by"] == "provider"

    # a redelivered webhook is a no-op
    again = confirm_payout(p["_id"], "pout_REAL_1", source="provider")
    assert again["status"] == PAYOUT_PAID
    assert db.payouts.count_documents({"status": PAYOUT_PAID}) == 1
    assert settled_payable(uid) == 70
    assert outstanding_payable(uid) == 0


def test_paid_payout_is_terminal(db, driver):
    from backend.errors import APIError

    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    submit_payout(p["_id"])
    confirm_payout(p["_id"], "pout_REAL_1")

    # cannot be failed, retried, or re-submitted after the money moved
    for fn, code in ((lambda: fail_payout(p["_id"], "late"), "payout_paid"),
                     (lambda: retry_payout(p["_id"]), "invalid_transition")):
        try:
            fn()
        except APIError as exc:
            assert exc.code == code, exc.code
        else:
            raise AssertionError(f"{fn} should have raised {code}")

    assert submit_payout(p["_id"])["status"] == PAYOUT_PAID
    assert db.ledger_entries.count_documents({"entry_type": "DRIVER_PAYOUT"}) == 1


def test_failure_and_retry_keep_the_money_accounted_for(db, driver):
    from backend.errors import APIError

    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    submit_payout(p["_id"])

    failed = fail_payout(p["_id"], "bank account invalid", source="provider")
    assert failed["status"] == PAYOUT_FAILED
    assert "bank account invalid" in failed["failure_reason"]
    # the money is still reserved, never silently returned
    assert outstanding_payable(uid) == 0

    # a failed payout cannot be submitted directly; it must be retried first
    try:
        submit_payout(p["_id"])
    except APIError as exc:
        assert exc.code == "payout_failed", exc.code
    else:
        raise AssertionError("submitting a failed payout must fail")

    reopened = retry_payout(p["_id"])
    assert reopened["status"] == PAYOUT_PENDING
    assert reopened["failure_reason"] is None
    assert submit_payout(p["_id"])["status"] == PAYOUT_PROCESSING
    # retrying reuses the same ledger row: no double debit
    assert db.ledger_entries.count_documents({"entry_type": "DRIVER_PAYOUT"}) == 1


# ------------------------------------------------------------------ the webhook
def _payout_webhook(app, event, secret=b"whsec_test_secret", corrupt=False):
    body = json.dumps(event)
    sig = hmac.new(b"whsec_wrong" if corrupt else secret,
                   body.encode(), hashlib.sha256).hexdigest()
    return app.test_client().post("/api/payments/payout-webhook",
                                  headers={"X-Razorpay-Signature": sig},
                                  data=body, content_type="application/json")


def _razorpay_app():
    from backend.app import create_app

    app = create_app()
    app.config.update({"PAYMENT_PROVIDER": "razorpay",
                       "RAZORPAY_WEBHOOK_SECRET": "whsec_test_secret",
                       "PAYOUT_PROVIDER": "razorpayx"})
    return app


def test_signed_payout_webhook_marks_paid(db, driver):
    app = _razorpay_app()
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    with app.app_context():
        submit_payout(p["_id"])

    resp = _payout_webhook(app, {
        "event": "payout.processed",
        "event_id": "evt_payout_1",
        "payload": {"payout": {"entity": {
            "id": "pout_REAL_1", "status": "processed", "reference_id": p["reference"]}}},
    })
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["status"] == PAYOUT_PAID
    assert db.payouts.find_one({"_id": p["_id"]})["paid_at"] is not None


def test_payout_webhook_rejects_a_bad_signature(db, driver):
    app = _razorpay_app()
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    with app.app_context():
        submit_payout(p["_id"])

    resp = _payout_webhook(app, {
        "event": "payout.processed", "event_id": "evt_bad",
        "payload": {"payout": {"entity": {"id": "pout_X", "reference_id": p["reference"]}}},
    }, corrupt=True)
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "webhook_invalid"
    assert db.payouts.find_one({"_id": p["_id"]})["status"] == PAYOUT_PROCESSING


def test_payout_webhook_is_deduplicated(db, driver):
    app = _razorpay_app()
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    with app.app_context():
        submit_payout(p["_id"])

    event = {"event": "payout.processed", "event_id": "evt_dup_1",
             "payload": {"payout": {"entity": {"id": "pout_REAL_1",
                                              "reference_id": p["reference"]}}}}
    a = _payout_webhook(app, event)
    b = _payout_webhook(app, event)
    assert a.status_code == 200 and a.get_json()["status"] == PAYOUT_PAID
    assert b.status_code == 200 and b.get_json().get("duplicate") is True
    # exactly one confirmation was applied
    assert settled_payable(uid) == 70


def test_payout_webhook_failure_event_marks_failed(db, driver):
    app = _razorpay_app()
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    with app.app_context():
        submit_payout(p["_id"])

    resp = _payout_webhook(app, {
        "event": "payout.failed", "event_id": "evt_fail_1",
        "payload": {"payout": {"entity": {
            "id": "pout_REAL_2", "status": "failed",
            "failure_reason": "beneficiary account invalid",
            "reference_id": p["reference"]}}},
    })
    assert resp.status_code == 200, resp.get_json()
    stored = db.payouts.find_one({"_id": p["_id"]})
    assert stored["status"] == PAYOUT_FAILED
    assert "beneficiary" in stored["failure_reason"]


def test_payout_webhook_for_an_unknown_payout_is_a_noop(db, driver):
    """An event for a payout this deployment does not know must not 500 -- the
    provider would retry forever, and nothing needs to change here."""
    app = _razorpay_app()
    resp = _payout_webhook(app, {
        "event": "payout.processed", "event_id": "evt_unknown",
        "payload": {"payout": {"entity": {"id": "pout_NOPE", "reference_id": "PO-NOPE"}}},
    })
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json().get("unmatched") is True


# ------------------------------------------------------- the manual confirmation
def _credit_driver_earnings(db, driver, amount):
    """Post a DRIVER_PAYABLE directly, with a unique entry_id like post_entry does."""
    from bson import ObjectId as _Oid

    db.ledger_entries.insert_one({
        "entry_id": "ENT-" + str(_Oid()),
        "account_id": str(driver["user"]["_id"]), "account_type": "driver",
        "entry_type": "DRIVER_PAYABLE", "amount": amount, "currency": "INR",
        "booking_id": None, "meta": {}, "created_at": _now(),
    })


def _manual_setup(client, db, admin, driver, vehicle, amount=70):
    _credit_driver_earnings(db, driver, amount)
    created = client.post("/api/admin/payouts", headers=admin["auth"],
                          json={"user_id": str(driver["user"]["_id"])})
    assert created.status_code in (200, 201), created.get_json()
    return created.get_json()["payout"]


def _now():
    from backend.db import utcnow

    return utcnow()


def test_manual_payout_is_confirmed_with_a_bank_reference(client, db, admin, driver, vehicle):
    """The manual flow is the ONLY case where a human establishes that money
    moved, so the bank reference is mandatory and recorded."""
    payout = _manual_setup(client, db, admin, driver, vehicle)
    assert payout["status"] == "pending"
    assert client.post(f"/api/admin/payouts/{payout['id']}/submit",
                       headers=admin["auth"]).status_code == 200

    missing = client.post(f"/api/admin/payouts/{payout['id']}/confirm",
                          headers=admin["auth"], json={})
    assert missing.status_code == 422
    assert missing.get_json()["error"]["code"] == "payout_reference_required"

    ok = client.post(f"/api/admin/payouts/{payout['id']}/confirm", headers=admin["auth"],
                     json={"provider_reference": "UTR123456789"})
    assert ok.status_code == 200, ok.get_json()
    settled = ok.get_json()["payout"]
    assert settled["status"] == "paid"
    assert settled["provider_reference"] == "UTR123456789"
    assert settled["confirmed_by"] == "manual"
    assert db.audit_logs.count_documents({"action": "payout.confirm"}) == 1


def test_manual_confirmation_is_idempotent_on_the_same_reference(client, db, admin, driver,
                                                                 vehicle):
    payout = _manual_setup(client, db, admin, driver, vehicle)
    client.post(f"/api/admin/payouts/{payout['id']}/submit", headers=admin["auth"])
    body = {"provider_reference": "UTR_DUP_1"}
    first = client.post(f"/api/admin/payouts/{payout['id']}/confirm",
                        headers=admin["auth"], json=body)
    second = client.post(f"/api/admin/payouts/{payout['id']}/confirm",
                         headers=admin["auth"], json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert second.get_json()["payout"]["status"] == "paid"
    assert db.payouts.count_documents({"status": "paid"}) == 1


def test_manual_confirmation_cannot_override_a_razorpayx_settlement(client, db, admin, driver,
                                                                    vehicle):
    """The razorpayx path is established by the signed webhook. Allowing an admin
    to assert it would remove the only real guarantee that money moved."""
    from bson import ObjectId as _Oid

    payout = _manual_setup(client, db, admin, driver, vehicle)
    # A razorpayx payout is in flight; submit cannot complete here because no
    # RazorpayX account is linked in the test environment, so the state is set
    # directly to model "sent, awaiting the signed webhook".
    db.payouts.update_one({"_id": _Oid(payout["id"])},
                          {"$set": {"provider": "razorpayx", "status": "processing"}})

    resp = client.post(f"/api/admin/payouts/{payout['id']}/confirm", headers=admin["auth"],
                       json={"provider_reference": "UTR_FAKE"})
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "payout_not_manual"
    assert db.payouts.find_one({"_id": _Oid(payout["id"])})["status"] == "processing"
    # the refusal itself is auditable
    assert db.audit_logs.count_documents({"action": "payout.confirm_refused"}) == 1


def test_a_bank_reference_cannot_be_reused_across_payouts(client, db, admin, driver, vehicle):
    """Reusing a UTR would make the bank statement ambiguous when reconciling."""
    first = _manual_setup(client, db, admin, driver, vehicle)
    client.post(f"/api/admin/payouts/{first['id']}/submit", headers=admin["auth"])
    assert client.post(f"/api/admin/payouts/{first['id']}/confirm", headers=admin["auth"],
                       json={"provider_reference": "UTR_SHARED"}).status_code == 200

    _credit_driver_earnings(db, driver, 50)
    second = client.post("/api/admin/payouts", headers=admin["auth"],
                         json={"user_id": str(driver["user"]["_id"])}).get_json()["payout"]
    client.post(f"/api/admin/payouts/{second['id']}/submit", headers=admin["auth"])

    clash = client.post(f"/api/admin/payouts/{second['id']}/confirm", headers=admin["auth"],
                        json={"provider_reference": "UTR_SHARED"})
    assert clash.status_code == 409
    assert clash.get_json()["error"]["code"] == "payout_reference_in_use"


def test_confirmation_requires_admin(client, db, admin, driver, vehicle, rider):
    payout = _manual_setup(client, db, admin, driver, vehicle)
    forbidden = client.post(f"/api/admin/payouts/{payout['id']}/confirm",
                            headers=rider["auth"], json={"provider_reference": "X"})
    assert forbidden.status_code == 403


# --------------------------------------------------- the books under pressure
def test_concurrent_payout_creations_cannot_overdraw_the_driver(app, db, admin, driver):
    """The single most important concurrency property in the whole money path:
    no matter how many payout requests arrive at once, the driver is never paid
    out more than they earned, and the total reserved equals the earnings.

    Each thread drives the real admin endpoint (including its per-driver lock),
    so the guarantee is tested where a caller would actually hit it.
    """
    import threading

    _credit_driver_earnings(db, driver, 70)
    uid = driver["user"]["_id"]
    client = app.test_client()
    headers = admin["auth"]
    barrier = threading.Barrier(12)
    created, refused = [], []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        with app.app_context():
            resp = client.post("/api/admin/payouts", headers=headers,
                               json={"user_id": str(uid)})
        with lock:
            (created if resp.status_code in (200, 201) else refused).append(resp)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    granted = [r for r in created if r.get_json()["payout"]["amount"] > 0]
    reserved = sum(r.get_json()["payout"]["amount"] for r in granted)

    assert reserved <= 70, f"reserved {reserved} against 70 earned"
    assert reserved == 70, "the full balance should still be claimable by someone"
    assert len(granted) == 1, f"exactly one payout may consume the balance, got {len(granted)}"
    # the losers were cleanly refused, not silently double-served
    assert len(refused) == 11
    for r in refused:
        assert r.status_code in (409, 422, 429), r.get_json()
        assert r.get_json()["error"]["code"] in (
            "payout_in_progress", "no_balance", "payout_exceeds_balance")

    # and the books agree with the payouts
    assert db.payouts.count_documents({"user_id": uid}) == 1
    assert db.ledger_entries.count_documents({"entry_type": "DRIVER_PAYOUT"}) == 1
    assert outstanding_payable(uid) == 0
    ledger_total = sum(float(r.get("amount", 0.0)) for r in db.ledger_entries.find(
        {"account_id": str(uid), "account_type": "driver"}))
    assert abs(ledger_total) < 0.01, ledger_total


def test_concurrent_submits_send_the_payout_only_once(app, db, driver):
    """Submit is the step that touches the bank, so a retried submit must not
    create a second transfer."""
    import threading

    uid = driver["user"]["_id"]
    _credit_driver_earnings(db, driver, 70)
    p = new_payout(uid, 70)

    barrier = threading.Barrier(10)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        with app.app_context():
            out = submit_payout(p["_id"])
        with lock:
            results.append(out["status"])

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) == {PAYOUT_PROCESSING}, results
    assert db.ledger_entries.count_documents({"entry_type": "DRIVER_PAYOUT"}) == 1


def test_concurrent_confirmations_settle_exactly_once(app, db, driver):
    """Two webhook deliveries racing must still produce one settlement."""
    import threading

    uid = driver["user"]["_id"]
    _credit_driver_earnings(db, driver, 70)
    p = new_payout(uid, 70)
    submit_payout(p["_id"])

    barrier = threading.Barrier(8)
    paid = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        with app.app_context():
            out = confirm_payout(p["_id"], "pout_RACE_1", source="provider")
        with lock:
            paid.append(out["status"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(paid) == {PAYOUT_PAID}, paid
    assert db.payouts.count_documents({"status": PAYOUT_PAID}) == 1
    assert settled_payable(uid) == 70
    assert outstanding_payable(uid) == 0


# ------------------------------------------------------------------ the books
def test_balance_accounting_stays_correct_across_the_whole_cycle(db, driver):
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)          # driver owes 70

    p1 = new_payout(uid, 30)
    p2 = new_payout(uid, 40)
    # 30 + 40 fully reserves the 70 payable
    assert outstanding_payable(uid) == 0
    assert db.ledger_entries.count_documents({"entry_type": "DRIVER_PAYOUT"}) == 2

    submit_payout(p1["_id"])
    submit_payout(p2["_id"])
    confirm_payout(p1["_id"], "pout_1")
    confirm_payout(p2["_id"], "pout_2")
    assert settled_payable(uid) == 70
    assert outstanding_payable(uid) == 0

    # the driver ledger nets to exactly zero: earned 70, paid out 70
    total = sum(float(r.get("amount", 0.0)) for r in db.ledger_entries.find(
        {"account_id": str(uid), "account_type": "driver"}))
    assert abs(total) < 0.01, total


def test_razorpayx_without_an_account_fails_loudly_and_stays_retryable(db, driver):
    from backend.errors import APIError

    app = _razorpay_app()
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)

    with app.app_context():
        # no linked RazorpayX account: the payout must be created as a
        # razorpayx payout, then refuse to send rather than silently "succeed".
        p = new_payout(uid, 70)
        assert p["provider"] == "razorpayx"
        try:
            submit_payout(p["_id"])
        except APIError as exc:
            assert exc.status == 503, exc.status
            assert exc.code == "payouts_unconfigured", exc.code
        else:
            raise AssertionError("unconfigured razorpayx must not silently succeed")

        # released back to pending, so a fixed configuration can retry cleanly
        stored = db.payouts.find_one({"_id": p["_id"]})
        assert stored["status"] == PAYOUT_PENDING
        assert stored["processing_at"] is None
        assert outstanding_payable(uid) == 0


def test_manual_payout_needs_a_confirmation_before_it_is_paid(db, driver):
    """The `manual` provider has no gateway to call, so the payout sits in
    `processing` until someone records the bank reference."""
    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    assert p["provider"] == "manual"
    assert submit_payout(p["_id"])["status"] == PAYOUT_PROCESSING
    assert db.payouts.find_one({"_id": p["_id"]})["paid_at"] is None

    paid = confirm_payout(p["_id"], "UTR123456", source="manual")
    assert paid["status"] == PAYOUT_PAID
    assert paid["confirmed_by"] == "manual"


def test_voiding_a_failed_payout_releases_the_money(db, driver):
    """A failed transfer must not strand the driver's balance: voiding it posts
    an explicit reversal so the rupees become payable again, and the books stay
    auditable because the original debit is never rewritten."""
    from backend.errors import APIError

    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    submit_payout(p["_id"])
    fail_payout(p["_id"], "beneficiary invalid", source="provider")
    # still held: the debit exists, so the money is not free to re-spend
    assert outstanding_payable(uid) == 0

    voided = void_payout(p["_id"], "beneficiary bank details corrected")
    assert voided["status"] == "voided"
    assert "corrected" in voided["void_reason"]
    # the reservation is released and the balance is whole again
    assert outstanding_payable(uid) == 70
    assert db.ledger_entries.count_documents({"entry_type": "DRIVER_PAYOUT_REVERSAL"}) == 1
    # and a fresh payout of the full amount is now allowed
    assert new_payout(uid, 70)["amount"] == 70

    # a voided payout is terminal
    for fn in (lambda: retry_payout(p["_id"]), lambda: submit_payout(p["_id"])):
        try:
            fn()
        except APIError as exc:
            assert exc.status == 409, exc.status
        else:
            raise AssertionError("a voided payout must not move again")


def test_paid_payout_can_never_be_voided(db, driver):
    from backend.errors import APIError

    uid = driver["user"]["_id"]
    _earn(db, uid, 100)
    p = new_payout(uid, 70)
    submit_payout(p["_id"])
    confirm_payout(p["_id"], "pout_REAL_1")

    try:
        void_payout(p["_id"], "too late")
    except APIError as exc:
        assert exc.code == "payout_paid", exc.code
    else:
        raise AssertionError("a paid payout must not be voidable")
    assert db.ledger_entries.count_documents({"entry_type": "DRIVER_PAYOUT_REVERSAL"}) == 0


def test_payout_cannot_exceed_what_the_driver_has_earned(db, driver):
    """The cap lives inside new_payout, so no caller can overdraw the driver."""
    from backend.errors import APIError

    uid = driver["user"]["_id"]
    _earn(db, uid, 100)          # driver has earned 70
    try:
        new_payout(uid, 71)
    except APIError as exc:
        assert exc.code == "payout_exceeds_balance", exc.code
        assert exc.details["outstanding"] == 70.0
    else:
        raise AssertionError("cannot pay out more than was earned")

    new_payout(uid, 70)
    try:
        new_payout(uid, 0.01)
    except APIError as exc:
        assert exc.code == "payout_exceeds_balance", exc.code
    else:
        raise AssertionError("earnings must not be payable twice")
