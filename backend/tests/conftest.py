"""Pytest fixtures.

Runs against a dedicated local database `ridemate_test` so the live demo DB
is never touched. Rate limits are raised for other tests; the dedicated
rate-limit test monkeypatches them back down.
"""

import os

os.environ["MONGO_DB_NAME"] = "ridemate_test"
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["PAYMENT_PROVIDER"] = "demo"
os.environ["FLASK_ENV"] = "development"
os.environ["JWT_SECRET"] = "test-only-secret"

import pytest  # noqa: E402
from datetime import date, timedelta, time  # noqa: E402

from backend import db as dbmodule  # noqa: E402
from backend.app import create_app  # noqa: E402

MODULES = [
    "users", "vehicles", "rides", "bookings", "payments", "refunds",
    "ledger_entries", "payouts", "refresh_tokens", "notifications",
    "notification_preferences", "locations", "audit_logs", "ratings",
    "blocks", "reports", "webhook_events", "reconciliations",
    "payment_reconciliations",
]


@pytest.fixture(scope="session")
def app():
    app = create_app()
    app.config.update({
        "RATE_LIMIT_DEFAULT": 100000,
        "RATE_LIMIT_AUTH": 100000,
        "RATE_LIMIT_STRICT": 100000,
        "SEATS_MAX_PER_BOOKING": 12,
    })
    yield app


@pytest.fixture(scope="session")
def db(app):
    yield dbmodule.get_db()


@pytest.fixture(autouse=True)
def clean_db(db):
    for name in MODULES:
        db[name].delete_many({})
    yield
    for name in MODULES:
        db[name].delete_many({})


@pytest.fixture
def client(app):
    return app.test_client()


PASSWORD = "Password123"


def _make_user(db, name, email, role="user"):
    from backend.security import hash_password
    from backend.timeutil import utc_now

    now = utc_now()
    doc = {
        "name": name,
        "email": email,
        "password_hash": hash_password(PASSWORD),
        "age": 28,
        "phone": "+919845000000",
        "gender": "male",
        "bio": "",
        "photo_url": "",
        "auth_provider": "local",
        "role": role,
        "rating": 0.0,
        "total_rides": 0,
        "token_version": 0,
        "email_verified": True,
        "phone_verified": True,
        "driver_verified": False,
        "licence_verified": False,
        "insurance_verified": False,
        "created_at": now,
        "updated_at": now,
        "last_login_at": now,
        "last_logout_at": None,
    }
    db.users.insert_one(doc)
    return doc


def register(client, email):
    return client.post("/api/auth/register", json={
        "name": email.split("@")[0],
        "email": email,
        "password": PASSWORD,
    })


def login(client, email, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def token_of(resp):
    return resp.get_json().get("token")


def auth(resp):
    return {"Authorization": f"Bearer {token_of(resp)}"}


@pytest.fixture
def rider(client, db):
    user = _make_user(db, "Rider One", "rider@test.in")
    resp = login(client, "rider@test.in")
    return {"user": user, "auth": auth(resp), "token": token_of(resp)}


def _make_driver(db, client, name, email):
    """Create a user and promote them the way the product actually does.

    Nobody self-declares a role: registration creates a plain `user`, and an
    admin has to call PATCH /api/admin/users/<uid>/role. Tests that build a
    "driver" without doing that are testing a user, and any gate keyed on the
    driver role will quietly 403 them.
    """
    user = _make_user(db, name, email)
    admin = _make_user(db, f"{name} Admin", f"admin-for-{email}", role="admin")
    tok = auth(login(client, f"admin-for-{email}"))
    resp = client.patch(f"/api/admin/users/{user['_id']}/role", headers=tok,
                        json={"role": "driver"})
    assert resp.status_code == 200, resp.get_json()
    del admin
    return db.users.find_one({"_id": user["_id"]})


@pytest.fixture
def driver(app, client, db):
    """The default test driver: onboarded, KYC-clear and able to be paid.

    Payout creation is gated on verified payout onboarding, so a driver without
    it cannot reach the money path at all. Making the *default* driver onboarded
    keeps the shared fixture a realistic happy-path driver instead of forcing
    every payout test to opt in. Tests that exercise the gate itself use
    `driver_without_onboarding`, so the gate stays genuinely active rather than
    being quietly disabled for the suite.
    """
    user = _make_driver(db, client, "Driver One", "driver@test.in")
    resp = login(client, "driver@test.in")
    handle = {"user": user, "auth": auth(resp), "token": token_of(resp)}
    _complete_onboarding(app, user["_id"])
    return handle


@pytest.fixture
def driver_without_onboarding(app, client, db):
    """A driver who has NOT set up a payout account.

    Used to assert that the settlement gate actually refuses to pay. Kept
    separate from `driver` so the refusal is proven by a test rather than
    assumed because the default fixture happens to be onboarded.
    """
    user = _make_driver(db, client, "Driver No Onboarding",
                        "driver-no-onboarding@test.in")
    resp = login(client, "driver-no-onboarding@test.in")
    return {"user": user, "auth": auth(resp), "token": token_of(resp)}


def _complete_onboarding(app, user_id):
    """Drive the real onboarding state machine to `verified`.

    Uses the module's own transitions rather than writing the status directly, so
    a broken transition fails here instead of being masked by a fixture.
    """
    from backend import onboarding

    with app.app_context():
        onboarding.begin_onboarding(user_id, account_last4="1234")
        onboarding.submit_onboarding(user_id, contact_id="cont_TEST",
                                     linked_account_id="la_TEST")
        onboarding.verify_onboarding(user_id, reviewer_id="admin-test")


def satisfy_other_publish_gates(db, user_id, vehicle_id, *, vehicle_kyc=True):
    """Clear the publish gates a caller did not come here to test.

    Publication requires three independent things: a verified driver identity, a
    verified vehicle (DL + insurance), and an approved RC. A test focused on one
    of them must clear the others, or its publish assertion 403s for an
    unrelated reason and proves nothing.

    `vehicle_kyc=False` leaves the vehicle gate alone, for the tests that are
    specifically about it.
    """
    from bson import ObjectId

    from backend.timeutil import utc_now

    db.users.update_one({"_id": user_id}, {"$set": {
        "kyc_status": "verified",
        "kyc_reviewed_at": utc_now(),
        "kyc_reviewed_by": "admin-test",
    }})
    fields = {
        "rc_document": {
            "doc_id": "rctest",
            "key": "private/rc/%s/rctest.pdf" % user_id,
            "content_type": "application/pdf",
            "size": 1,
            "encrypted": False,
            "uploaded_at": utc_now(),
            "verification_status": "approved",
            "reviewed_at": utc_now(),
            "reviewed_by": "admin-test",
            "reason": None,
        },
    }
    if vehicle_kyc:
        fields["verification_status"] = "verified"
    db.vehicles.update_one({"_id": ObjectId(str(vehicle_id))}, {"$set": fields})


@pytest.fixture
def onboarded_driver(app, driver):
    """Explicit alias: reads better than `driver` in payout-focused tests."""
    return driver


@pytest.fixture
def vehicle(driver, client, db):
    resp = client.post("/api/vehicles", headers=driver["auth"], json={
        "vehicle_type": "4-wheeler",
        "vehicle_number": "KA99XX9999",
        "vehicle_model": "Test Car",
        "seat_count": 4,
    })
    assert resp.status_code == 201, resp.get_json()
    vid = resp.get_json()["vehicle"]["id"]
    # Ride publication is gated on THREE independent facts: the driver is
    # verified, the vehicle (DL + insurance) is verified, and the RC is approved.
    # Most tests exercise the ordinary compliant path, so the shared fixtures are
    # made compliant here. Each gate is covered directly in test_kyc.py,
    # test_identity_kyc.py and test_rc.py -- marking them satisfied in the shared
    # fixture keeps the gates genuinely active for the rest of the suite rather
    # than quietly disabling them.
    satisfy_other_publish_gates(db, driver["user"]["_id"], vid)
    return resp.get_json()["vehicle"]


def future_date(days=10):
    return (date.today() + timedelta(days=days)).isoformat()


def make_ride(client, driver_auth, vehicle, origin=("A Loc", 12.90, 77.60),
              dest=("B Loc", 13.00, 77.70), day=10, hh=9, mm=0,
              seats=2, fare=100, tz="Asia/Kolkata"):
    return client.post("/api/rides", headers=driver_auth, json={
        "vehicle_id": vehicle["id"],
        "origin": {"label": origin[0], "address": origin[0], "lat": origin[1], "lng": origin[2]},
        "destination": {"label": dest[0], "address": dest[0], "lat": dest[1], "lng": dest[2]},
        "departure_date": future_date(day),
        "departure_time": f"{hh:02d}:{mm:02d}",
        "timezone": tz,
        "seats_total": seats,
        "fare_per_seat": fare,
    })