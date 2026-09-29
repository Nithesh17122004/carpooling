"""Data-visibility rules: private fields must never leak into ride payloads;
search must filter by radius and distance."""

from backend.tests.conftest import _make_user, make_ride

_PRIVATE = {"email", "phone", "dl_document_path", "dl_document_name",
            "vehicle_number", "password_hash"}


def test_public_search_hides_private_fields(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle)
    assert r.status_code == 201, r.get_json()
    ride = r.get_json()["ride"]
    hits = client.get("/api/rides/search").get_json()["data"]
    hit = next(x for x in hits if x["id"] == ride["id"])
    for key in _PRIVATE:
        assert key not in hit
    # owner snapshot allows name/rating/photo only
    assert {"id", "name", "photo_url", "rating", "total_rides"}.issuperset(hit["owner"].keys())
    assert "email" not in hit["owner"]


def test_owner_ride_payload_still_private(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle)
    ride = r.get_json()["ride"]
    mine = client.get(f"/api/rides/{ride['id']}", headers=driver["auth"]).get_json()["ride"]
    # even the owner's ride payload hides contact details (they live in /me)
    assert "email" not in mine.get("owner", {})
    # and /me does reveal the private fields
    me = client.get("/api/auth/me", headers=driver["auth"]).get_json()["user"]
    assert me["email"]


def test_search_radius_excludes_far_rides(client, db, driver, rider, vehicle):
    make_ride(client, driver["auth"], vehicle,
              origin=("Mumbai", 19.07, 72.87), dest=("Pune", 18.52, 73.85))
    r = client.get("/api/rides/search", query_string={"lat": 12.9, "lng": 77.6, "radius_km": 50})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"] == []


def test_search_returns_distance(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle,
                  origin=("A", 12.90, 77.60), dest=("B", 12.95, 77.65))
    assert r.status_code == 201, r.get_json()
    hits = client.get("/api/rides/search").get_json()["data"]
    hit = hits[0]
    assert "distance_km" in hit
    assert "duration_minutes" not in hit  # not exposed in list payloads


def test_cancel_state_transitions(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle, seats=1)
    ride = r.get_json()["ride"]
    assert ride["status"] == "published"
    b = client.post("/api/bookings", headers=rider["auth"],
                    json={"ride_id": ride["id"], "seats": 1})
    assert b.status_code == 201
    bd = client.post(f"/api/bookings/{b.get_json()['booking']['id']}/verify",
                     headers=rider["auth"], json={})
    assert bd.get_json()["booking"]["status"] == "confirmed"
    detail = client.get(f"/api/rides/{ride['id']}").get_json()["ride"]
    assert detail["status"] == "full"


def test_vehicle_plate_hidden_in_search(client, db, driver, rider, vehicle):
    r = make_ride(client, driver["auth"], vehicle)
    ride = r.get_json()["ride"]
    hits = client.get("/api/rides/search").get_json()["data"]
    hit = next(x for x in hits if x["id"] == ride["id"])
    # model/type are public, the registration plate is not
    assert "vehicle" in hit and "number" not in hit["vehicle"]
    assert "number" not in hit


def test_vehicle_plate_visible_only_to_owner_and_confirmed_riders(client, db, driver, rider,
                                                                  vehicle):
    r = make_ride(client, driver["auth"], vehicle)
    ride = r.get_json()["ride"]
    ride_id = ride["id"]

    anon = client.get(f"/api/rides/{ride_id}").get_json()["ride"]
    assert "number" not in anon["vehicle"]

    owner = client.get(f"/api/rides/{ride_id}", headers=driver["auth"]).get_json()["ride"]
    assert owner["vehicle"]["number"] == vehicle["vehicle_number"]

    # a rider who never booked this ride must not see the plate
    _make_user(db, "Curious", "curious@test.in")
    from backend.tests.conftest import login
    utok = login(client, "curious@test.in").get_json()["token"]
    uauth = {"Authorization": f"Bearer {utok}"}
    other = client.get(f"/api/rides/{ride_id}", headers=uauth).get_json()["ride"]
    assert "number" not in other["vehicle"]

    booked = client.post("/api/bookings", headers=rider["auth"],
                         json={"ride_id": ride_id, "seats": 1})
    assert booked.status_code == 201
    bv = client.post(f"/api/bookings/{booked.get_json()['booking']['id']}/verify",
                     headers=rider["auth"], json={})
    assert bv.status_code == 200
    confirmed = client.get(f"/api/rides/{ride_id}", headers=rider["auth"]).get_json()["ride"]
    assert confirmed["vehicle"]["number"] == vehicle["vehicle_number"]