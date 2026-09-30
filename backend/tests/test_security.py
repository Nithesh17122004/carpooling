"""HTTP security surface: JSON error contract, security headers, request-id
sanitisation, and rate-limit behaviour that ignores spoofable proxy headers.

Rate-limit tests each use a dedicated driver identity + pinned REMOTE_ADDR so
the in-process memory bucket for one test can never pollute another.
"""

from backend.tests.conftest import _make_user, login, make_ride


def _isolated_driver(db, client, seq):
    """Create a unique driver + vehicle + ride for an isolated limiter test."""
    email = f"rl{seq}@test.in"
    _make_user(db, f"RL Driver {seq}", email)
    tok = login(client, email).get_json()["token"]
    hdrs = {"Authorization": f"Bearer {tok}"}
    v = client.post("/api/vehicles", headers=hdrs, json={
        "vehicle_type": "4-wheeler",
        "vehicle_number": f"RL{seq:02d}XX4444",
        "vehicle_model": "Rate Car",
        "seat_count": 4,
    })
    assert v.status_code == 201, v.get_json()
    # This fixture is about rate limits, not KYC, so all three publish gates are
    # cleared to keep them out of the way. Each gate has its own tests.
    from bson import ObjectId

    from backend.tests.conftest import satisfy_other_publish_gates

    user = db.users.find_one({"email": email})
    satisfy_other_publish_gates(db, user["_id"],
                               ObjectId(v.get_json()["vehicle"]["id"]))
    r = make_ride(client, hdrs, v.get_json()["vehicle"])
    ride_id = r.get_json()["ride"]["id"]
    return hdrs, ride_id


def test_json_errors_are_structured(client):
    assert client.get("/api/definitely-not-a-route").status_code == 404
    body = client.get("/api/definitely-not-a-route").get_json()
    assert body.get("ok") is False
    assert "code" in body["error"] and "message" in body["error"]

    r405 = client.post("/api/health", json={})
    assert r405.status_code == 405
    assert r405.is_json
    assert "code" in r405.get_json()["error"]


def test_security_headers_and_no_cache(client):
    r = client.get("/api/health")
    for header in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
        "Content-Security-Policy",
        "X-Request-Id",
        "Cache-Control",
    ):
        assert header in r.headers
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"


def test_client_request_id_is_sanitised(client):
    nasty = 'ab<cd>df"gh|ij%kl z'
    r = client.get("/api/health", headers={"X-Request-Id": nasty})
    rid = r.headers.get("X-Request-Id")
    # only [0-9A-Za-z:_-] survive, truncated to 64 chars
    assert all((ch.isalnum() or ch in ":_-") for ch in rid)
    assert len(rid) <= 64
    assert rid == "abcddfghijklz"


def test_rate_limit_ignores_x_forwarded_for_spoofing(client, db, monkeypatch):
    """The limiter keys on request.remote_addr only. Rotating X-Forwarded-For
    must not reset the counter (no ProxyFix configured -> header ignored)."""
    hdrs, ride_id = _isolated_driver(db, client, 1)
    monkeypatch.setitem(client.application.config, "RATE_LIMIT_STRICT", 4)
    ip = "198.51.100.77"

    codes = []
    retry_after = None
    for i in range(6):
        spoof_headers = dict(hdrs)
        spoof_headers["X-Forwarded-For"] = f"{i}.{i}.{i}.{i}:9999"
        resp = client.post(f"/api/rides/{ride_id}/location",
                           headers=spoof_headers,
                           json={"lat": 12.90, "lng": 77.60},
                           environ_overrides={"REMOTE_ADDR": ip})
        codes.append(resp.status_code)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")

    assert codes == [200, 200, 200, 200, 429, 429], codes
    assert retry_after is not None and retry_after.isdigit()


def test_rate_limit_isolated_per_endpoint(client, db, monkeypatch):
    """A spoof-immune counter on one endpoint does not throttle neighbours."""
    hdrs, ride_id = _isolated_driver(db, client, 2)
    ip = "198.51.100.78"
    monkeypatch.setitem(client.application.config, "RATE_LIMIT_STRICT", 2)

    for _ in range(3):
        assert client.post(f"/api/rides/{ride_id}/location", headers=hdrs,
                           json={"lat": 12.90, "lng": 77.60},
                           environ_overrides={"REMOTE_ADDR": ip}).status_code in (200, 429)

    # a different endpoint using the same client IP still works normally
    assert client.post("/api/health", json={},
                       environ_overrides={"REMOTE_ADDR": ip}).status_code == 405


def test_429_response_is_json_with_retry_after(client, db, monkeypatch):
    monkeypatch.setitem(client.application.config, "RATE_LIMIT_STRICT", 3)
    hdrs, ride_id = _isolated_driver(db, client, 3)
    ip = "198.51.100.79"

    last = None
    for _ in range(5):
        resp = client.post(f"/api/rides/{ride_id}/location", headers=hdrs,
                           json={"lat": 12.90, "lng": 77.60},
                           environ_overrides={"REMOTE_ADDR": ip})
        last = resp
    assert last.status_code == 429
    assert last.is_json
    assert last.get_json()["error"]["code"] == "rate_limited"
    assert last.headers.get("Retry-After", "").isdigit()