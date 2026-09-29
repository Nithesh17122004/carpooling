"""Commission invariants: exactly 30%, frozen at payment creation, and
reproducible from the immutable ledger.

These tests are the financial core of the platform. They assert three separate
properties so a regression in one cannot hide a break in another:

  1. ARITHMETIC  - fee == 30% of gross for a wide range of fares, with no
                   minimum floor, and gross == fee + net always.
  2. IMMUTABILITY- the rate and the rupee amounts are stored on the payment
                   document, so changing the commission config afterwards cannot
                   rewrite an already-settled transaction or its refund.
  3. RECONCILIATION - the ledger, the payment document, admin reconciliation
                   and the driver statement all agree on the same numbers.
"""

from bson import ObjectId

from backend.ledger import commission_for_payment, platform_fee, quote_commission
from backend.tests.conftest import make_ride, _make_user, login


def _book_and_verify(client, rider_auth, ride_id, seats=1):
    r = client.post("/api/bookings", headers=rider_auth,
                    json={"ride_id": ride_id, "seats": seats})
    assert r.status_code == 201, r.get_json()
    b = r.get_json()["booking"]
    rv = client.post(f"/api/bookings/{b['id']}/verify", headers=rider_auth, json={})
    assert rv.status_code == 200, rv.get_json()
    return rv.get_json()["booking"]


# ------------------------------------------------------------------ arithmetic
def test_commission_is_exactly_thirty_percent(app):
    """No minimum floor, no rounding drift: fee is precisely 30% of gross."""
    with app.app_context():
        for gross in (1, 7, 10, 33, 50, 99, 100, 150, 333, 1000, 1234.56, 99999.99):
            gross = float(gross)
            fee = platform_fee(gross)
            assert abs(fee - round(gross * 0.30, 2)) < 0.005, (gross, fee)
            assert fee < gross, "commission can never consume the whole fare"


def test_quote_commission_split_is_self_consistent(app):
    """gross == fee + net, and the driver keeps exactly 70%."""
    with app.app_context():
        for gross in (10, 49.99, 100, 250.5, 1000):
            split = quote_commission(gross)
            assert abs(split["gross"] - round(split["platform_fee"] + split["driver_net"], 2)) < 0.005
            assert abs(split["driver_net"] - round(float(gross) * 0.70, 2)) < 0.005
            assert split["commission_rate_percent"] == 30.0
            assert split["currency"] == "INR"


def test_platform_fee_never_exceeds_gross(app):
    """A pathological 100% config still cannot create a negative driver net."""
    with app.app_context():
        original = app.config["PLATFORM_FEE_PERCENT"]
        try:
            app.config["PLATFORM_FEE_PERCENT"] = 100
            assert platform_fee(250) == 250
            assert quote_commission(250)["driver_net"] == 0
        finally:
            app.config["PLATFORM_FEE_PERCENT"] = original


# ---------------------------------------------------------------- immutability
def test_split_is_frozen_on_the_payment_document(client, db, driver, rider, vehicle):
    """Rate and amounts are persisted at order creation, not derived later."""
    ride = make_ride(client, driver["auth"], vehicle, seats=2, fare=200).get_json()["ride"]
    booking = _book_and_verify(client, rider["auth"], ride["id"])

    payment = db.payments.find_one({"booking_id": ObjectId(booking["id"])})
    assert payment is not None
    assert payment["amount"] == 200
    assert payment["platform_fee"] == 60, "30% of 200"
    assert payment["driver_net"] == 140
    assert payment["commission_rate_percent"] == 30.0
    assert payment["commission_frozen_at"] is not None

    with client.application.app_context():
        assert commission_for_payment(payment)["frozen"] is True


def test_commission_config_change_cannot_rewrite_history(client, db, driver, rider, vehicle, app):
    """A settled payment keeps its original split after the rate is changed.

    This is the single most important financial property: platform policy may
    change tomorrow, but money already collected must never be recomputed.
    """
    ride = make_ride(client, driver["auth"], vehicle, seats=2, fare=100).get_json()["ride"]
    booking = _book_and_verify(client, rider["auth"], ride["id"])
    oid = ObjectId(booking["id"])
    payment_id = db.payments.find_one({"booking_id": oid})["_id"]

    before = db.payments.find_one({"_id": payment_id})
    original = app.config["PLATFORM_FEE_PERCENT"]
    try:
        app.config["PLATFORM_FEE_PERCENT"] = 5
        with app.app_context():
            assert platform_fee(100) == 5, "live config did change"
            reread = commission_for_payment(db.payments.find_one({"_id": payment_id}))
            assert reread["platform_fee"] == 30, "settled commission must not follow config"
            assert reread["driver_net"] == 70
    finally:
        app.config["PLATFORM_FEE_PERCENT"] = original

    after = db.payments.find_one({"_id": payment_id})
    assert after["platform_fee"] == before["platform_fee"] == 30
    assert after["driver_net"] == before["driver_net"] == 70

    # the ledger must also still reflect the original 30% split
    fee_row = db.ledger_entries.find_one({"booking_id": str(oid), "entry_type": "PLATFORM_FEE"})
    assert fee_row["amount"] == -30
    assert fee_row["meta"]["commission_rate_percent"] == 30.0


def test_legacy_payment_without_frozen_split_is_flagged_not_rewritten(client, db, driver, rider, vehicle, app):
    """A payment from before the freeze is recomputed for display only, and is
    reported as unfrozen so reconciliation can surface it."""
    with app.app_context():
        legacy = {"amount": 100, "currency": "INR", "provider": "demo", "status": "success"}
        split = commission_for_payment(legacy)
        assert split["frozen"] is False
        assert split["platform_fee"] == 30


def test_refund_reversal_uses_the_frozen_rate_not_current_config(client, db, driver, rider, vehicle, app):
    """A full refund must reverse exactly the commission that was charged, even
    if the commission rate changed between capture and refund."""
    import datetime
    # depart well outside the cancellation cutoff so this is a FULL refund
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=20)
    ride = client.post("/api/rides", headers=driver["auth"], json={
        "vehicle_id": vehicle["id"],
        "origin": {"label": "Near", "address": "Near", "lat": 12.9, "lng": 77.6},
        "destination": {"label": "Far", "address": "Far", "lat": 13.0, "lng": 77.7},
        "departure_date": now.date().isoformat(),
        "departure_time": now.strftime("%H:%M"),
        "timezone": "UTC",
        "seats_total": 2,
        "fare_per_seat": 80,
    }).get_json()["ride"]

    booking = _book_and_verify(client, rider["auth"], ride["id"])
    oid = ObjectId(booking["id"])

    original = app.config["PLATFORM_FEE_PERCENT"]
    try:
        app.config["PLATFORM_FEE_PERCENT"] = 45  # hostile: policy "changed" mid-flight
        cr = client.delete(f"/api/bookings/{booking['id']}", headers=rider["auth"])
        assert cr.status_code == 200, cr.get_json()
    finally:
        app.config["PLATFORM_FEE_PERCENT"] = original

    rows = {r["entry_type"]: r["amount"]
            for r in db.ledger_entries.find({"booking_id": str(oid)})}
    # 30% of 80 = 24 charged, and 24 reversed -> platform nets to zero
    assert rows["PLATFORM_FEE"] == -24
    assert rows["PLATFORM_FEE_REVERSAL"] == 24
    assert abs(rows["PLATFORM_FEE"] + rows["PLATFORM_FEE_REVERSAL"]) < 0.01


# --------------------------------------------------------------- reconciliation
def test_ledger_identity_holds_for_every_payment(client, db, driver, rider, vehicle):
    """Across several fares, the three ledger rows must always satisfy
    gross == commission + driver net and match the frozen split."""
    fares = [30, 45, 60, 175, 240]
    for i, fare in enumerate(fares):
        _make_user(db, f"Rider {fare}", f"rider{fare}@test.in")
        ra = {"Authorization": f"Bearer {login(client, f'rider{fare}@test.in').get_json()['token']}"}
        # distinct departure days: one vehicle cannot run overlapping rides
        ride = make_ride(client, driver["auth"], vehicle, seats=3, fare=fare,
                         day=10 + i).get_json()["ride"]
        booking = _book_and_verify(client, ra, ride["id"])
        oid = str(ObjectId(booking["id"]))

        rows = {r["entry_type"]: r["amount"]
                for r in db.ledger_entries.find({"booking_id": oid})}
        gross = rows["PASSENGER_PAYMENT"]
        fee = abs(rows["PLATFORM_FEE"])
        net = rows["DRIVER_PAYABLE"]
        assert abs(gross - fare) < 0.01
        assert abs(fee - round(fare * 0.30, 2)) < 0.01, (fare, fee)
        assert abs(gross - round(fee + net, 2)) < 0.01, (fare, fee, net)


def test_reconciliation_stays_balanced_under_30_percent(client, db, driver, rider, vehicle):
    """Mixed capture + full refund + partial refund must leave the books
    balanced, and reconciliation must report no commission drift."""
    from backend.tests.conftest import _make_user as mk, login as li

    _make_user(db, "Admin", "admin-recon@test.in", role="admin")
    admin_auth = {"Authorization": f"Bearer {login(client, 'admin-recon@test.in').get_json()['token']}"}

    ride = make_ride(client, driver["auth"], vehicle, seats=4, fare=100).get_json()["ride"]
    booking = _book_and_verify(client, rider["auth"], ride["id"])
    cr = client.delete(f"/api/bookings/{booking['id']}", headers=rider["auth"])
    assert cr.status_code == 200, cr.get_json()

    body = client.get("/api/admin/reconcile", headers=admin_auth).get_json()
    assert body["status"] == "balanced", body
    assert not [i for i in body["issues"] if i["kind"] == "commission_mismatch"]
    assert not [i for i in body["issues"] if i["kind"] == "split_unbalanced"]
    assert not body["issues"], body["issues"]


def test_commission_change_does_not_create_false_drift(client, db, driver, rider, vehicle, app):
    """Reconciliation compares against frozen values, so an unchanged but
    differently-configured commission must NOT be reported as an issue."""
    from backend.tests.conftest import _make_user as mk, login as li

    _make_user(db, "Admin", "admin-drift@test.in", role="admin")
    admin_auth = {"Authorization": f"Bearer {login(client, 'admin-drift@test.in').get_json()['token']}"}

    ride = make_ride(client, driver["auth"], vehicle, seats=2, fare=100).get_json()["ride"]
    _book_and_verify(client, rider["auth"], ride["id"])

    original = app.config["PLATFORM_FEE_PERCENT"]
    try:
        app.config["PLATFORM_FEE_PERCENT"] = 12
        body = client.get("/api/admin/reconcile", headers=admin_auth).get_json()
    finally:
        app.config["PLATFORM_FEE_PERCENT"] = original

    assert body["status"] == "balanced", body
    assert not [i for i in body["issues"] if "commission" in i["kind"]]


# ----------------------------------------------------------------- statements
def test_driver_statement_and_payout_agree_with_the_frozen_split(client, db, driver, rider, vehicle):
    """The driver-facing number, the payable the admin can settle, and the
    frozen split must be the same value."""
    from backend.tests.conftest import _make_user as mk, login as li

    _make_user(db, "Admin", "admin-payout@test.in", role="admin")
    admin_auth = {"Authorization": f"Bearer {login(client, 'admin-payout@test.in').get_json()['token']}"}

    ride = make_ride(client, driver["auth"], vehicle, seats=2, fare=500).get_json()["ride"]
    booking = _book_and_verify(client, rider["auth"], ride["id"])
    payment = db.payments.find_one({"booking_id": ObjectId(booking["id"])})

    stats = client.get("/api/profile/stats", headers=driver["auth"]).get_json()["stats"]
    assert abs(stats["total_earnings"] - payment["driver_net"]) < 0.01
    assert payment["driver_net"] == 350  # 70% of 500

    created = client.post("/api/admin/payouts", headers=admin_auth,
                          json={"user_id": str(driver["user"]["_id"])})
    assert created.status_code in (200, 201), created.get_json()
    assert created.get_json()["payout"]["amount"] == 350
