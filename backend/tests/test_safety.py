"""Safety features: trusted contacts, reports, SOS, and blocks."""

from bson import ObjectId

from backend.tests.conftest import make_ride


def test_trusted_contacts_roundtrip(client, rider):
    resp = client.get("/api/safety/trusted-contacts", headers=rider["auth"])
    assert resp.status_code == 200
    assert resp.get_json()["data"] == []

    put = client.put("/api/safety/trusted-contacts", headers=rider["auth"], json={
        "contacts": [
            {"name": "Mom", "phone": "+919845000001"},
            {"name": "Roommate", "phone": "+919845000002"},
        ]})
    assert put.status_code == 200, put.get_json()
    assert len(put.get_json()["data"]) == 2

    got = client.get("/api/safety/trusted-contacts", headers=rider["auth"]).get_json()["data"]
    assert got[0]["name"] == "Mom"

    too_many = client.put("/api/safety/trusted-contacts", headers=rider["auth"], json={
        "contacts": [{"name": f"P{i}", "phone": f"+9198450000{i:02d}"} for i in range(6)]})
    assert too_many.status_code == 422

    bad = client.put("/api/safety/trusted-contacts", headers=rider["auth"], json={
        "contacts": [{"name": "X", "phone": "not-a-phone"}]})
    assert bad.status_code == 422


def test_report_flow(client, db, driver, rider):
    r = client.post("/api/safety/report", headers=rider["auth"], json={
        "target_user_id": str(driver["user"]["_id"]),
        "reason": "unsafe_driving",
        "details": "Harsh braking and speeding.",
    })
    assert r.status_code == 201, r.get_json()
    assert db.reports.count_documents({}) == 1
    report = db.reports.find_one({})
    assert report["status"] == "open"
    assert report["target_user_id"] == driver["user"]["_id"]
    assert db.notifications.count_documents({"notification_type": "admin_flag", "read": False}) == 1

    self_report = client.post("/api/safety/report", headers=rider["auth"], json={
        "target_user_id": str(rider["user"]["_id"]), "reason": "x"})
    assert self_report.status_code == 422

    unknown = client.post("/api/safety/report", headers=rider["auth"], json={
        "target_user_id": "000000000000000000000000", "reason": "x"})
    assert unknown.status_code == 404


def test_sos_flags_ride_and_audits(client, db, driver, rider, vehicle):
    ride = client.post("/api/rides", headers=driver["auth"], json={
        "vehicle_id": vehicle["id"],
        "origin": {"label": "A", "address": "A", "lat": 12.9, "lng": 77.6},
        "destination": {"label": "B", "address": "B", "lat": 13.0, "lng": 77.7},
        "departure_date": "2099-12-31", "departure_time": "09:00",
        "timezone": "Asia/Kolkata", "seats_total": 2, "fare_per_seat": 100}).get_json()["ride"]

    r = client.post("/api/safety/sos", headers=rider["auth"], json={
        "ride_id": ride["id"], "note": "Need help"})
    assert r.status_code == 200, r.get_json()
    flagged = db.rides.find_one({"_id": ObjectId(ride["id"])})
    assert flagged["sos_active"] is True
    assert db.audit_logs.count_documents({"action": "safety.sos"}) == 1
    assert db.notifications.count_documents({"notification_type": "admin_flag"}) == 1

    no_ride = client.post("/api/safety/sos", headers=rider["auth"], json={})
    assert no_ride.status_code == 200
    assert no_ride.get_json()["sos"]["ride_id"] is None


def test_block_cycle(client, db, driver, rider):
    target = str(driver["user"]["_id"])
    b = client.put(f"/api/safety/block/{target}", headers=rider["auth"])
    assert b.status_code == 200
    b2 = client.put(f"/api/safety/block/{target}", headers=rider["auth"])
    assert b2.status_code == 200
    assert db.blocks.count_documents({"blocker_id": rider["user"]["_id"]}) == 1

    blocked = client.get("/api/safety/blocked", headers=rider["auth"]).get_json()["data"]
    assert len(blocked) == 1
    assert blocked[0]["id"] == target

    u = client.delete(f"/api/safety/block/{target}", headers=rider["auth"])
    assert u.status_code == 200
    assert client.get("/api/safety/blocked", headers=rider["auth"]).get_json()["data"] == []

    self_block = client.put(f"/api/safety/block/{rider['user']['_id']}", headers=rider["auth"])
    assert self_block.status_code == 422

    unknown = client.put("/api/safety/block/000000000000000000000000", headers=rider["auth"])
    assert unknown.status_code == 404