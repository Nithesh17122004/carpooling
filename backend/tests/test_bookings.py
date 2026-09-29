"""Booking correctness: concurrency, idempotency, seat accounting, refund
policy, cancellation-after-departure, and ledger consistency under load."""

import threading
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from backend.timeutil import utc_now
from backend.tests.conftest import register, login, token_of, auth, make_ride, future_date


def _book_and_verify(client, rider_auth, ride_id, seats=1):
    r = client.post("/api/bookings", headers=rider_auth,
                    json={"ride_id": ride_id, "seats": seats})
    assert r.status_code == 201, r.get_json()
    b = r.get_json()["booking"]
    rv = client.post(f"/api/bookings/{b['id']}/verify", headers=rider_auth, json={})
    assert rv.status_code == 200, rv.get_json()
    return rv.get_json()["booking"]


def test_full_booking_lifecycle(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle, seats=2, fare=100)
    assert r.status_code == 201, r.get_json()
    ride = r.get_json()["ride"]
    assert ride["status"] == "published"

    b = _book_and_verify(client, rider["auth"], ride["id"])
    assert b["status"] == "confirmed"
    assert b["amount"] == 100

    detail = client.get(f"/api/rides/{ride['id']}", headers=driver["auth"]).get_json()["ride"]
    assert detail["seats_available"] == ride["seats_available"] - 1

    # driver balance reflects net payable (gross - exactly 30% platform commission)
    stats = client.get("/api/profile/stats", headers=driver["auth"]).get_json()["stats"]
    expected = round(100 * 0.70, 2)
    assert abs(stats["total_earnings"] - expected) < 0.01, stats

    # one outstanding payment recorded in the ledger
    oid = ObjectId(b["id"])
    assert db.ledger_entries.count_documents({"booking_id": str(oid)}) == 3
    assert db.payments.count_documents({"booking_id": oid}) == 1


def test_overbooking_rejected(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle, seats=1, fare=90)
    ride = r.get_json()["ride"]

    ob = client.post("/api/bookings", headers=rider["auth"],
                     json={"ride_id": ride["id"], "seats": 1})
    assert ob.status_code == 201
    ob2 = client.post("/api/bookings", headers=rider["auth"],
                      json={"ride_id": ride["id"], "seats": 1})
    assert ob2.status_code == 409
    assert ob2.get_json()["error"]["code"] in ("already_booked", "seats_unavailable")


def test_cannot_book_own_ride(client, db, driver, vehicle):
    r = make_ride(client, driver["auth"], vehicle)
    r2 = client.post("/api/bookings", headers=driver["auth"],
                     json={"ride_id": r.get_json()["ride"]["id"], "seats": 1})
    assert r2.status_code == 400 or r2.status_code == 409


def test_booking_unique_per_rider(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle, seats=3)
    ride_id = r.get_json()["ride"]["id"]
    assert client.post("/api/bookings", headers=rider["auth"],
                       json={"ride_id": ride_id, "seats": 1}).status_code == 201
    again = client.post("/api/bookings", headers=rider["auth"],
                        json={"ride_id": ride_id, "seats": 1})
    assert again.status_code == 409
    assert again.get_json()["error"]["code"] == "already_booked"


def test_cancel_full_refund_before_cutoff(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle, seats=2, fare=100)
    ride = r.get_json()["ride"]
    b = _book_and_verify(client, rider["auth"], ride["id"])
    before = ride["seats_available"]

    cr = client.delete(f"/api/bookings/{b['id']}", headers=rider["auth"])
    assert cr.status_code == 200, cr.get_json()
    cb = cr.get_json()["booking"]
    assert cb["status"] == "refunded"
    assert cb["refund_amount"] == 100  # > 60 min before departure -> 100%
    assert db.refunds.count_documents({}) == 1
    # seats restored
    detail = client.get(f"/api/rides/{ride['id']}").get_json()["ride"]
    assert detail["seats_available"] == before


def test_cancel_partial_refund_within_cutoff(client, db, driver, rider, vehicle):
    now = datetime.now(timezone.utc) + timedelta(minutes=20)
    riders_dep = now
    ride = client.post("/api/rides", headers=driver["auth"], json={
        "vehicle_id": vehicle["id"],
        "origin": {"label": "Near", "address": "Near", "lat": 12.9, "lng": 77.6},
        "destination": {"label": "Far", "address": "Far", "lat": 13.0, "lng": 77.7},
        # departure 20 minutes from now -> inside the default 60-minute cutoff
        "departure_date": riders_dep.date().isoformat(),
        "departure_time": riders_dep.strftime("%H:%M"),
        "timezone": "UTC",
        "seats_total": 2,
        "fare_per_seat": 100,
    })
    assert ride.status_code == 201, ride.get_json()
    ride_id = ride.get_json()["ride"]["id"]
    b = _book_and_verify(client, rider["auth"], ride_id, 1)

    cr = client.delete(f"/api/bookings/{b['id']}", headers=rider["auth"])
    assert cr.status_code == 200, cr.get_json()
    cb = cr.get_json()["booking"]
    assert cb["refund_amount"] == 50
    assert db.refunds.count_documents({}) == 1


def test_cancel_rejected_after_departure(client, db, driver, rider, vehicle):
    # a ride whose departure is in the past by the time we cancel
    r = make_ride(client, driver["auth"], vehicle)
    ride_id = r.get_json()["ride"]["id"]
    b = _book_and_verify(client, rider["auth"], ride_id, 1)

    db.rides.update_one({"_id": ObjectId(ride_id)},
                        {"$set": {"departure_at": utc_now() - timedelta(minutes=5),
                                  "status": "completed"}})
    cr = client.delete(f"/api/bookings/{b['id']}", headers=rider["auth"])
    assert cr.status_code == 409
    assert cr.get_json()["error"]["code"] == "ride_departed"


def test_concurrent_booking_never_overfills(client, db, driver, rider, vehicle):
    """2 seats; 100 simultaneous attempts by distinct riders -> exactly 2
    seat-holders, no negative seats, rejected attempts are clean 409s."""
    r = make_ride(client, driver["auth"], vehicle, seats=2, fare=80)
    ride_id = r.get_json()["ride"]["id"]

    n_riders = 100
    from flask import Flask
    from backend.security import issue_access_token
    from backend.tests.conftest import _make_user
    riders = []
    ctx = client.application.app_context()
    ctx.push()
    try:
        for i in range(n_riders):
            u = _make_user(db, f"R{i}", f"r{i}@thread.in")
            tok = issue_access_token(u)
            riders.append({"id": i, "email": f"r{i}@thread.in",
                           "auth": {"Authorization": f"Bearer {tok}"}})
    finally:
        ctx.pop()

    results = []
    lock = threading.Lock()

    def attempt(rr):
        rc = client.post("/api/bookings", headers=rr["auth"],
                         json={"ride_id": ride_id, "seats": 1})
        code = rc.status_code
        b = rc.get_json().get("booking") or {}
        err = (rc.get_json().get("error") or {}).get("code")
        with lock:
            results.append((code, b.get("status"), b.get("id"), err, rr["email"]))

    threads = [threading.Thread(target=attempt, args=(r_,))
               for r_ in riders]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created = [x for x in results if x[0] == 201]
    assert len(created) == 2, [x for x in results if x[3] in (None, "already_booked")][:10]
    # every created booking is unique
    ids = [x[2] for x in created]
    assert len(set(ids)) == 2
    # ride reflects exactly 2 seats claimed
    ride_now = client.get(f"/api/rides/{ride_id}").get_json()["ride"]
    assert ride_now["seats_available"] == 0
    assert ride_now["status"] == "full"
    assert ride_now["seats_available"] >= 0
    # rejected attempts are 409 not 5xx
    others = [x for x in results if x[0] != 201]
    assert all(x[0] == 409 for x in others), [x for x in others[:8]]
    assert all(x[0] == 409 for x in others)
    # exactly two booking rows exist (no leaked/failed rows)
    assert db.bookings.count_documents({}) == 2
    assert db.payments.count_documents({}) == 2


def test_rebook_allowed_after_cancel(client, db, driver, rider, vehicle):
    """A rider who cancelled may book the same ride again (partial index only
    de-duplicates ACTIVE bookings, not historical ones)."""
    r = make_ride(client, driver["auth"], vehicle, seats=3, fare=100)
    ride = r.get_json()["ride"]
    b = _book_and_verify(client, rider["auth"], ride["id"])
    cr = client.delete(f"/api/bookings/{b['id']}", headers=rider["auth"])
    assert cr.status_code == 200
    assert cr.get_json()["booking"]["status"] == "refunded"

    again = client.post("/api/bookings", headers=rider["auth"],
                        json={"ride_id": ride["id"], "seats": 1})
    assert again.status_code == 201, again.get_json()


def test_partial_refund_reverses_fee_proportionally(client, db, driver, rider, vehicle):
    """Partial (cutoff-window) refunds must reverse only their share of the fee
    recorded at payment time, so a full refund nets every account back to zero."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=20)
    ride = client.post("/api/rides", headers=driver["auth"], json={
        "vehicle_id": vehicle["id"],
        "origin": {"label": "Near", "address": "Near", "lat": 12.9, "lng": 77.6},
        "destination": {"label": "Far", "address": "Far", "lat": 13.0, "lng": 77.7},
        "departure_date": now.date().isoformat(),
        "departure_time": now.strftime("%H:%M"),
        "timezone": "UTC",
        "seats_total": 2,
        "fare_per_seat": 50,
    })
    assert ride.status_code == 201, ride.get_json()
    ride_id = ride.get_json()["ride"]["id"]
    b = _book_and_verify(client, rider["auth"], ride_id, 1)

    # gross 50 -> commission is exactly 30% = 15, driver net payable 35
    oid = ObjectId(b["id"])
    rows = {r["entry_type"]: r for r in db.ledger_entries.find({"booking_id": str(oid)})}
    assert abs(rows["PLATFORM_FEE"]["amount"] + 15) < 0.01
    assert abs(rows["DRIVER_PAYABLE"]["amount"] - 35) < 0.01

    # 50%-within-cutoff cancel -> refund 25 -> reverse 7.50 fee + 17.50 net
    cr = client.delete(f"/api/bookings/{b['id']}", headers=rider["auth"])
    assert cr.status_code == 200, cr.get_json()
    assert cr.get_json()["booking"]["refund_amount"] == 25

    all_rows = list(db.ledger_entries.find({"booking_id": str(oid)}))
    kinds = {r["entry_type"]: r["amount"] for r in all_rows}
    assert kinds["PLATFORM_FEE_REVERSAL"] == 7.5
    assert abs(kinds["DRIVER_PAYABLE_REVERSAL"] + 17.5) < 0.01
    assert kinds["PASSENGER_REFUND"] == -25
    # accounts retain exactly the un-refunded remainder (no drift)
    assert abs(kinds["PASSENGER_PAYMENT"] + kinds["PASSENGER_REFUND"] - 25) < 0.01  # rider keeps 25
    assert abs(kinds["PLATFORM_FEE"] + kinds["PLATFORM_FEE_REVERSAL"] + 7.5) < 0.01  # 30% of kept 25
    assert abs(kinds["DRIVER_PAYABLE"] + kinds["DRIVER_PAYABLE_REVERSAL"] - 17.5) < 0.01


def test_driver_cancel_refunds_everyone_to_zero(client, db, driver, rider, vehicle):
    """A driver-initiated ride cancellation refunds every confirmed rider 100%
    and the ledger nets each account back to zero."""
    r = make_ride(client, driver["auth"], vehicle, seats=2, fare=100)
    ride = r.get_json()["ride"]

    from backend.tests.conftest import _make_user, login
    b1 = _book_and_verify(client, rider["auth"], ride["id"])
    _make_user(db, "Rider Two", "rider2@test.in")
    auth2 = {"Authorization": f"Bearer {login(client, 'rider2@test.in').get_json()['token']}"}
    r2 = client.post("/api/bookings", headers=auth2,
                     json={"ride_id": ride["id"], "seats": 1})
    assert r2.status_code == 201, r2.get_json()
    b2 = client.post(f"/api/bookings/{r2.get_json()['booking']['id']}/verify",
                     headers=auth2, json={})
    assert b2.status_code == 200, b2.get_json()

    cr = client.delete(f"/api/rides/{ride['id']}", headers=driver["auth"])
    assert cr.status_code == 200, cr.get_json()

    d1 = client.get(f"/api/rides/{ride['id']}").get_json()["ride"]
    assert d1["status"] == "cancelled"
    assert d1["seats_available"] == d1["seats_total"]
    assert db.refunds.count_documents({}) == 2

    # every account nets exactly to zero after a FULL refund
    sums = {}
    for row in db.ledger_entries.find({}):
        key = (row["account_id"], row["account_type"])
        sums[key] = round(sums.get(key, 0.0) + row["amount"], 2)
    assert abs(sums.get(("PLATFORM", "platform"), 0.0)) < 0.01, sums
    assert abs(sums.get((str(driver["user"]["_id"]), "driver"), 0.0)) < 0.01, sums
    assert abs(sums.get((str(rider["user"]["_id"]), "passenger"), 0.0)) < 0.01, sums
    assert abs(sums.get((str(db.users.find_one({"email": "rider2@test.in"})["_id"]),
                         "passenger"), 0.0)) < 0.01, sums


def test_pending_payment_expires_and_seats_are_released(client, db, driver, rider, vehicle,
                                                        monkeypatch):
    """A booking stuck in pending_payment past PAYMENT_TTL_MINUTES is cancelled
    by the lazy sweep, returning its seats to the pool."""
    from backend.timeutil import utc_now
    from datetime import timedelta

    monkeypatch.setitem(client.application.config, "PAYMENT_TTL_MINUTES", 5)
    r = make_ride(client, driver["auth"], vehicle, seats=2, fare=80)
    ride = r.get_json()["ride"]
    assert ride["seats_available"] == 2

    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": 1})
    assert b.status_code == 201, b.get_json()
    bid = ObjectId(b.get_json()["booking"]["id"])

    # simulate browser abandonment: booking untouched past the TTL
    db.bookings.update_one({"_id": bid},
                           {"$set": {"updated_at": utc_now() - timedelta(minutes=20)}})
    assert db.rides.find_one({"_id": ObjectId(ride["id"])})["seats_available"] == 1

    # contact an endpoint that triggers the lazy sweep
    nxt = client.post("/api/bookings", headers=rider["auth"],
                      json={"ride_id": ride["id"], "seats": 1})
    stale = db.bookings.find_one({"_id": bid})
    assert stale["status"] == "cancelled"
    # seat was released then re-claimed for the new booking
    assert db.rides.find_one({"_id": ObjectId(ride["id"])})["seats_available"] == 1
    if nxt.status_code == 201:
        assert db.bookings.count_documents({"rider_id": rider["user"]["_id"],
                                            "status": {"$in": ["pending_payment", "payment_failed", "confirmed"]}}) == 1


def test_verify_after_departure_refunds_and_rejects(client, db, driver, rider, vehicle):
    """Payment settling after the ride has left must be reversed, not kept."""
    from backend.timeutil import utc_now
    from datetime import timedelta

    r = make_ride(client, driver["auth"], vehicle, seats=2, fare=90)
    ride = r.get_json()["ride"]
    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": 1})
    assert b.status_code == 201, b.get_json()
    bid = b.get_json()["booking"]["id"]

    # departure passes while the rider is still paying
    db.rides.update_one({"_id": ObjectId(ride["id"])},
                        {"$set": {"departure_at": utc_now() - timedelta(minutes=5)}})

    rv = client.post(f"/api/bookings/{bid}/verify", headers=rider["auth"], json={})
    assert rv.status_code == 409, rv.get_json()
    assert rv.get_json()["error"]["code"] == "ride_departed"
    booking = db.bookings.find_one({"_id": ObjectId(bid)})
    assert booking["status"] == "refunded"
    assert booking.get("refunded") is True
    assert db.refunds.count_documents({"booking_id": ObjectId(bid)}) == 1