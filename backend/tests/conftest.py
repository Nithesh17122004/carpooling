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
    "blocks", "reports", "webhook_events",
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
def driver(client, db):
    user = _make_user(db, "Driver One", "driver@test.in")
    resp = login(client, "driver@test.in")
    return {"user": user, "auth": auth(resp), "token": token_of(resp)}


@pytest.fixture
def rider(client, db):
    user = _make_user(db, "Rider One", "rider@test.in")
    resp = login(client, "rider@test.in")
    return {"user": user, "auth": auth(resp), "token": token_of(resp)}


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
    # Most tests exercise the ordinary (verified) driver path. The KYC gate is
    # covered directly in test_kyc.py; marking the shared fixture verified here
    # keeps the gate genuinely active for the rest of the suite rather than
    # quietly disabling it.
    from bson import ObjectId

    db.vehicles.update_one({"_id": ObjectId(vid)},
                           {"$set": {"verification_status": "verified"}})
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