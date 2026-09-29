"""Realtime location: driver-only writes, confirmed-rider reads, tracking stops
once the ride leaves a live state, and history is sampled (not per-ping)."""

from bson import ObjectId

from backend.tests.conftest import make_ride
import backend.blueprints.realtime as rtl


def _ride(client, driver, vehicle):
    r = make_ride(client, driver["auth"], vehicle, seats=2)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["ride"]


def _confirmed(client, db, driver, rider, vehicle):
    ride = _ride(client, driver, vehicle)
    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": 1})
    assert b.status_code == 201, b.get_json()
    v = client.post(f"/api/bookings/{b.get_json()['booking']['id']}/verify",
                    headers=rider["auth"], json={})
    assert v.status_code == 200, v.get_json()
    return ride


def test_unconfirmed_rider_cannot_watch(client, db, driver, rider, vehicle):
    ride = _ride(client, driver, vehicle)
    resp = client.get(f"/api/rides/{ride['id']}/location", headers=rider["auth"])
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "forbidden"


def test_confirmed_rider_can_stream(client, db, driver, rider, vehicle):
    ride = _confirmed(client, db, driver, rider, vehicle)
    resp = client.get(f"/api/rides/{ride['id']}/location", headers=rider["auth"])
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.headers.get("Cache-Control") == "no-store"
    resp.close()


def test_only_driver_posts_scaling_ping(client, db, driver, rider, vehicle):
    ride = _ride(client, driver, vehicle)
    denied = client.post(f"/api/rides/{ride['id']}/location", headers=rider["auth"],
                         json={"lat": 12.90, "lng": 77.60})
    assert denied.status_code == 403
    ok = client.post(f"/api/rides/{ride['id']}/location", headers=driver["auth"],
                     json={"lat": 12.90, "lng": 77.60})
    assert ok.status_code == 200
    assert ok.get_json()["saved"] is True


def test_pings_are_sampled_into_history(client, db, driver, rider, vehicle, monkeypatch):
    monkeypatch.setattr(rtl, "_SAMPLE_SECONDS", 0)
    ride = _ride(client, driver, vehicle)
    for i in range(3):
        resp = client.post(f"/api/rides/{ride['id']}/location", headers=driver["auth"],
                           json={"lat": 12.90 + i * 0.001, "lng": 77.60})
        assert resp.status_code == 200
    assert db.locations.count_documents({"ride_id": ObjectId(ride["id"])}) == 3


def test_tracking_rejected_after_cancel(client, db, driver, rider, vehicle):
    ride = _ride(client, driver, vehicle)
    cr = client.delete(f"/api/rides/{ride['id']}", headers=driver["auth"])
    assert cr.status_code == 200

    ping = client.post(f"/api/rides/{ride['id']}/location", headers=driver["auth"],
                       json={"lat": 12.90, "lng": 77.60})
    assert ping.status_code == 409
    watch = client.get(f"/api/rides/{ride['id']}/location", headers=driver["auth"])
    assert watch.status_code == 409
    assert watch.get_json()["error"]["code"] == "trip_not_active"


def test_tracking_rejected_once_completed(client, db, driver, rider, vehicle):
    ride = _ride(client, driver, vehicle)
    db.rides.update_one({"_id": ObjectId(ride["id"])},
                        {"$set": {"status": "completed"}})
    ping = client.post(f"/api/rides/{ride['id']}/location", headers=driver["auth"],
                       json={"lat": 12.90, "lng": 77.60})
    assert ping.status_code == 409