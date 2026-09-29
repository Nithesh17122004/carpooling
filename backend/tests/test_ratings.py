"""Ratings integrity: post-trip only, parties only, once per direction,
with aggregate recompute on the user record."""

from datetime import timedelta

from backend.timeutil import utc_now
from backend.tests.conftest import make_ride


def _confirmed_booking(client, db, driver, rider, vehicle, fare=100):
    r = make_ride(client, driver["auth"], vehicle, fare=fare)
    ride = r.get_json()["ride"]
    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": 1})
    assert b.status_code == 201, b.get_json()
    body = b.get_json()
    v = client.post(f"/api/bookings/{body['booking']['id']}/verify",
                    headers=rider["auth"], json={"payment_id": "pay_r1", "signature": "sig_r1"})
    assert v.status_code == 200, v.get_json()
    assert v.get_json()["booking"]["status"] == "confirmed"
    return ride["id"], body["booking"]["id"]


def _force_departed(db, ride_id):
    db.rides.update_one({"_id": db_module_id(ride_id)},
                        {"$set": {"departure_at": utc_now() - timedelta(minutes=10)}})


def db_module_id(rid):
    from bson import ObjectId
    return ObjectId(rid)


def test_rider_rates_driver_after_departure(client, db, driver, rider, vehicle):
    from bson import ObjectId
    ride_id, booking_id = _confirmed_booking(client, db, driver, rider, vehicle)
    _force_departed(db, ride_id)

    resp = client.post("/api/ratings", headers=rider["auth"], json={
        "booking_id": booking_id, "rated_user_id": str(driver["user"]["_id"]),
        "rating": 4, "review": "Smooth ride.",
    })
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["rating"]["rating"] == 4

    owner = db.users.find_one({"_id": driver["user"]["_id"]})
    assert owner["rating"] == 4.0
    assert owner["rating_count"] == 1
    assert db.ratings.count_documents({}) == 1

    user_ratings = client.get(f"/api/ratings/user/{driver['user']['_id']}",
                              headers=rider["auth"]).get_json()["data"]
    assert len(user_ratings) == 1
    assert user_ratings[0]["review"] == "Smooth ride."


def test_before_departure_rejected(client, db, driver, rider, vehicle):
    ride_id, booking_id = _confirmed_booking(client, db, driver, rider, vehicle)
    r = client.post("/api/ratings", headers=rider["auth"], json={
        "booking_id": booking_id, "rated_user_id": str(driver["user"]["_id"]), "rating": 5})
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "not_departed"
    assert db.ratings.count_documents({}) == 0


def test_cancelled_ride_not_rateable(client, db, driver, rider, vehicle):
    ride_id, booking_id = _confirmed_booking(client, db, driver, rider, vehicle)
    _force_departed(db, ride_id)
    db.rides.update_one({"_id": db_module_id(ride_id)}, {"$set": {"status": "cancelled"}})
    r = client.post("/api/ratings", headers=rider["auth"], json={
        "booking_id": booking_id, "rated_user_id": str(driver["user"]["_id"]), "rating": 5})
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "not_eligible"


def test_stranger_and_self_ratings_rejected(client, db, driver, rider, vehicle):
    from backend.tests.conftest import _make_user, login, auth
    stranger_user = _make_user(db, "Stranger", "stranger@test.in")
    sresp = login(client, "stranger@test.in")
    stranger = {"user": stranger_user, "auth": auth(sresp)}

    ride_id, booking_id = _confirmed_booking(client, db, driver, rider, vehicle)
    _force_departed(db, ride_id)

    # stranger cannot rate a trip they were not part of
    r = client.post("/api/ratings", headers=stranger["auth"], json={
        "booking_id": booking_id, "rated_user_id": str(rider["user"]["_id"]), "rating": 3})
    assert r.status_code == 403

    # self-rating rejected (rider rates themself)
    r = client.post("/api/ratings", headers=rider["auth"], json={
        "booking_id": booking_id, "rated_user_id": str(rider["user"]["_id"]), "rating": 5})
    assert r.status_code in (403, 422)

    # rating the wrong party rejected
    r = client.post("/api/ratings", headers=rider["auth"], json={
        "booking_id": booking_id, "rated_user_id": str(db.users.find_one(
            {"email": "stranger@test.in"})["_id"]), "rating": 3})
    assert r.status_code == 422


def test_duplicate_rating_rejected(client, db, driver, rider, vehicle):
    ride_id, booking_id = _confirmed_booking(client, db, driver, rider, vehicle)
    _force_departed(db, ride_id)
    payload = {"booking_id": booking_id,
               "rated_user_id": str(driver["user"]["_id"]), "rating": 5}
    assert client.post("/api/ratings", headers=rider["auth"], json=payload).status_code == 201
    dup = client.post("/api/ratings", headers=rider["auth"], json=payload)
    assert dup.status_code == 409
    assert dup.get_json()["error"]["code"] == "already_rated"


def test_driver_rates_rider_and_both_aggregate(client, db, driver, rider, vehicle):
    ride_id, booking_id = _confirmed_booking(client, db, driver, rider, vehicle)
    _force_departed(db, ride_id)

    r = client.post("/api/ratings", headers=rider["auth"], json={
        "booking_id": booking_id, "rated_user_id": str(driver["user"]["_id"]), "rating": 4})
    assert r.status_code == 201
    r = client.post("/api/ratings", headers=driver["auth"], json={
        "booking_id": booking_id, "rated_user_id": str(rider["user"]["_id"]), "rating": 5})
    assert r.status_code == 201

    assert db.users.find_one({"_id": driver["user"]["_id"]})["rating"] == 4.0
    assert db.users.find_one({"_id": rider["user"]["_id"]})["rating"] == 5.0
    assert db.ratings.count_documents({}) == 2


def test_only_confirmed_bookings_rateable(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle, fare=100)
    ride = r.get_json()["ride"]
    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": 1})
    booking_id = b.get_json()["booking"]["id"]
    _force_departed(db, ride["id"])
    resp = client.post("/api/ratings", headers=rider["auth"], json={
        "booking_id": booking_id, "rated_user_id": str(driver["user"]["_id"]), "rating": 5})
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "not_eligible"