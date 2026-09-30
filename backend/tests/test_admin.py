"""Admin RBAC and payout lifecycle: staff-only gating, create-payout from
ledger balance, and the idempotent payout advance endpoint with audit trail."""

from bson import ObjectId
import pytest

from backend.tests.conftest import _make_user, login, auth, make_ride


def _confirmed_driver_payment(client, db, driver, rider, vehicle, fare=100):
    r = make_ride(client, driver["auth"], vehicle, fare=fare)
    ride = r.get_json()["ride"]
    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": 1})
    assert b.status_code == 201, b.get_json()
    v = client.post(f"/api/bookings/{b.get_json()['booking']['id']}/verify",
                    headers=rider["auth"], json={"payment_id": "pay_demo1", "signature": "sig_demo1"})
    assert v.status_code == 200, v.get_json()
    assert v.get_json()["booking"]["status"] == "confirmed"
    return ride


@pytest.fixture
def admin(client, db):
    user = _make_user(db, "Admin One", "admin@test.in", role="admin")
    resp = login(client, "admin@test.in")
    assert resp.status_code == 200, resp.get_json()
    return {"user": user, "auth": auth(resp), "token": resp.get_json()["token"]}


def test_non_admin_cannot_access_admin_endpoints(client, admin, rider):
    for path in ("/api/admin/overview", "/api/admin/users",
                 "/api/admin/audit", "/api/admin/payments"):
        resp = client.get(path, headers=rider["auth"])
        assert resp.status_code == 403, (path, resp.get_json())
    resp = client.post("/api/admin/payouts", headers=rider["auth"], json={"user_id": "x"})
    assert resp.status_code == 403
    resp = client.patch("/api/admin/payouts/000000000000000000000000", headers=rider["auth"],
                        json={"status": "paid"})
    assert resp.status_code == 403


def test_create_and_advance_payout(client, db, admin, driver, rider, vehicle):
    """A payout reserves the driver's balance, is submitted to the provider, and
    becomes paid ONLY when the provider confirms it."""
    _confirmed_driver_payment(client, db, driver, rider, vehicle, fare=100)
    driver_id = str(driver["user"]["_id"])

    created = client.post("/api/admin/payouts", headers=admin["auth"], json={"user_id": driver_id})
    assert created.status_code in (200, 201), created.get_json()
    payout = created.get_json()["payout"]
    assert payout["amount"] == 70  # 100 gross - exactly 30% commission
    assert payout["status"] == "pending"

    # creating a payout does not move money: it is still not paid
    assert db.payouts.find_one({"reference": payout["reference"]})["paid_at"] is None

    # an admin may NOT mark it paid -- only the provider can do that
    shortcut = client.patch(f"/api/admin/payouts/{payout['id']}", headers=admin["auth"],
                            json={"status": "paid"})
    assert shortcut.status_code == 409
    assert shortcut.get_json()["error"]["code"] == "payout_confirmation_required"

    # submit -> processing
    submitted = client.post(f"/api/admin/payouts/{payout['id']}/submit", headers=admin["auth"])
    assert submitted.status_code == 200, submitted.get_json()
    assert submitted.get_json()["payout"]["status"] == "processing"

    # submit is idempotent (safe to retry after a timeout)
    again = client.post(f"/api/admin/payouts/{payout['id']}/submit", headers=admin["auth"])
    assert again.status_code == 200
    assert again.get_json()["payout"]["status"] == "processing"
    assert db.ledger_entries.count_documents({"entry_type": "DRIVER_PAYOUT"}) == 1

    # an unrelated status is rejected
    bad = client.patch(f"/api/admin/payouts/{payout['id']}", headers=admin["auth"],
                       json={"status": "disbursed"})
    assert bad.status_code == 422

    missing = client.post("/api/admin/payouts/000000000000000000000000/submit",
                          headers=admin["auth"])
    assert missing.status_code == 404

    entry = db.payouts.find_one({"_id": ObjectId(payout["id"])})
    assert entry["status"] == "processing"
    assert db.audit_logs.count_documents({"action": "financial.payout.create"}) == 1


def test_no_balance_payout_rejected(client, admin, driver):
    resp = client.post("/api/admin/payouts", headers=admin["auth"],
                       json={"user_id": str(driver["user"]["_id"])})
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "no_balance"


def test_unknown_user_payout_rejected(client, admin):
    resp = client.post("/api/admin/payouts", headers=admin["auth"],
                       json={"user_id": "000000000000000000000000"})
    assert resp.status_code == 404


def test_analytics_endpoint(client, db, admin, driver, rider, vehicle):
    ride = client.post("/api/rides", headers=driver["auth"], json={
        "vehicle_id": vehicle["id"],
        "origin": {"label": "A", "address": "A", "lat": 12.9, "lng": 77.6},
        "destination": {"label": "B", "address": "B", "lat": 13.0, "lng": 77.7},
        "departure_date": "2099-12-31", "departure_time": "09:00",
        "timezone": "Asia/Kolkata", "seats_total": 4, "fare_per_seat": 100})
    ride = ride.get_json()["ride"]
    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": 2})
    assert b.status_code == 201
    v = client.post(f"/api/bookings/{b.get_json()['booking']['id']}/verify",
                    headers=rider["auth"], json={"payment_id": "pay_a1", "signature": "sig_a1"})
    assert v.status_code == 200

    r = client.get("/api/admin/analytics", headers=admin["auth"])
    assert r.status_code == 200, r.get_json()
    a = r.get_json()["analytics"]
    assert a["active"]["dau"] >= 1  # admin logged in = today
    assert a["rides"]["bookings_confirmed"] == 1
    assert a["rides"]["fill_rate"] == 0.5  # 2 of 4 seats
    assert a["payments"]["success_count"] == 1
    assert a["payments"]["gmv"] == 200


# -------------------------------------------------------------- reconciliation
def test_a_reconciliation_run_is_recorded(client, db, admin, driver, rider, vehicle):
    """Returning a report is not the same as recording one. Without a stored
    run there is no evidence a check ever happened, and no way to see that
    today's clean result was not also clean last month."""
    _confirmed_driver_payment(client, db, driver, rider, vehicle, fare=100)

    r = client.get("/api/admin/reconcile", headers=admin["auth"])
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    run = body["run"]
    assert run["status"] == body["status"]
    assert run["issue_count"] == len(body["issues"])

    stored = db.reconciliations.find_one({"run_id": run["id"]})
    assert stored is not None, "the run was returned but not stored"
    assert stored["ran_by"] == str(admin["user"]["_id"])
    assert stored["info"]["gmv"] == 100
    # The snapshot carries the counts it was computed from, so two runs can be
    # compared instead of taken on trust.
    assert stored["issues"] == body["issues"]
    assert stored["ran_at"] is not None


def test_reconciliation_is_audited(client, db, admin, driver, rider, vehicle):
    _confirmed_driver_payment(client, db, driver, rider, vehicle, fare=100)
    client.get("/api/admin/reconcile", headers=admin["auth"])
    row = db.audit_logs.find_one({"action": "financial.reconcile"})
    assert row is not None
    assert row["actor_role"] == "admin"
    assert row["meta"]["status"] == "balanced"


def test_each_reconciliation_run_gets_its_own_record(client, db, admin, driver, rider, vehicle):
    _confirmed_driver_payment(client, db, driver, rider, vehicle, fare=100)
    a = client.get("/api/admin/reconcile", headers=admin["auth"]).get_json()["run"]["id"]
    b = client.get("/api/admin/reconcile", headers=admin["auth"]).get_json()["run"]["id"]
    assert a != b
    assert db.reconciliations.count_documents({}) == 2


def test_reconciliation_flags_a_payment_in_the_wrong_currency(client, db, admin,
                                                               driver, rider, vehicle):
    """A foreign-currency row makes every sum above it meaningless, so it has
    to be an issue in its own right rather than a footnote under the totals."""
    _confirmed_driver_payment(client, db, driver, rider, vehicle, fare=100)
    before = client.get("/api/admin/reconcile", headers=admin["auth"]).get_json()
    assert before["status"] == "balanced", before["issues"]

    db.payments.update_one({}, {"$set": {"currency": "USD"}})

    after = client.get("/api/admin/reconcile", headers=admin["auth"]).get_json()
    assert after["status"] == "attention"
    kinds = {i["kind"] for i in after["issues"]}
    assert "mixed_currency" in kinds, after["issues"]


def test_a_stored_reconciliation_run_never_blocks_the_report(client, db, admin, monkeypatch):
    """Losing the audit trail is bad; failing the report because of it is worse.
    An operator needs the numbers even when the write side is broken."""
    from pymongo.collection import Collection

    def boom(self, *a, **k):
        raise RuntimeError("write failed")

    monkeypatch.setattr(Collection, "insert_one", boom)
    r = client.get("/api/admin/reconcile", headers=admin["auth"])
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["ok"] is True
