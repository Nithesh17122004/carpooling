"""Auth/session security tests: rotation, revocation, password change,
logout-all, fake Google rejection, and rate limiting."""

import requests


def test_register_login_me_flow(client, db):
    r = client.post("/api/auth/register", json={
        "name": "New User", "email": "new@test.in",
        "password": "Password123"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body.get("token")
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert resp.status_code == 200
    assert resp.get_json()["user"]["email"] == "new@test.in"


def test_weak_password_rejected(client, db):
    r = client.post("/api/auth/register", json={
        "name": "Weak", "email": "weak@test.in", "password": "short"})
    assert r.status_code == 422
    assert r.get_json()["error"]["code"] == "weak_password"


def test_login_wrong_password_not_enumeration(client, db):
    from backend.tests.conftest import _make_user, login

    _make_user(db, "E", "enum@test.in")
    a = login(client, "enum@test.in", "WrongPass1").get_json()["error"]
    b = login(client, "nobody@test.in", "WrongPass1").get_json()["error"]
    assert a["code"] == "invalid_credentials" and b["code"] == "invalid_credentials"
    assert a["message"] == b["message"]


def test_refresh_rotation_revokes_old(client, db):
    from backend.tests.conftest import login, register

    register(client, "rot@test.in")
    r = login(client, "rot@test.in")
    assert r.status_code == 200
    old_cookie = r.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    r2 = client.post("/api/auth/refresh")
    assert r2.status_code == 200
    new_cookie = r2.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    assert new_cookie != old_cookie

    # replay the pre-rotation cookie via a fresh client (no jar contamination)
    fresh = client.application.test_client()
    r3 = fresh.post("/api/auth/refresh", headers={"Cookie": f"rm_refresh={old_cookie}"})
    assert r3.status_code == 401


def test_logout_revokes_refresh(client, db):
    from backend.tests.conftest import login, register

    register(client, "out@test.in")
    r = login(client, "out@test.in")
    assert r.status_code == 200
    tok = r.get_json()["token"]
    client.set_cookie("rm_refresh", r.headers["Set-Cookie"].split(";")[0].split("=", 1)[1])

    r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert client.post("/api/auth/refresh").status_code == 401


def test_password_change_invalidates_all_sessions(client, db):
    from backend.tests.conftest import login, register

    register(client, "pw@test.in")
    r = login(client, "pw@test.in")
    old_token = r.get_json()["token"]
    auth_h = {"Authorization": f"Bearer {old_token}"}

    r = client.post("/api/auth/password", headers=auth_h, json={
        "current_password": "Password123", "new_password": "NewPass12345"})
    assert r.status_code == 200, r.get_json()

    # the pre-change access token must now be dead (token_version bumped)
    assert client.get("/api/auth/me", headers=auth_h).status_code == 401
    # and the refresh cookie is revoked
    assert client.post("/api/auth/refresh").status_code == 401
    # the new password works
    assert login(client, "pw@test.in", "NewPass12345").status_code == 200


def test_logout_all_revokes(client, db):
    from backend.tests.conftest import login, register

    register(client, "all@test.in")
    r = login(client, "all@test.in")
    old_token = r.get_json()["token"]
    auth_h = {"Authorization": f"Bearer {old_token}"}
    assert client.post("/api/auth/logout-all", headers=auth_h).status_code == 200
    assert client.get("/api/auth/me", headers=auth_h).status_code == 401
    assert client.post("/api/auth/refresh").status_code == 401


def test_google_email_not_accepted(client, db):
    """The legacy demo bypass (client sends email/name) must fail."""
    r = client.post("/api/auth/google", json={"email": "admin@evil.com", "name": "Fake"})
    assert r.status_code in (400, 422, 503)
    if r.status_code == 422:
        assert r.get_json()["error"]["code"] == "missing_fields"


def test_google_credential_without_config(client, db):
    """With GOOGLE_CLIENT_ID unset the server refuses creds cleanly."""
    r = client.post("/api/auth/google", json={"credential": "fake.jwt.value"})
    assert r.status_code in (503, 400, 401, 502)
    if r.status_code == 503:
        assert r.get_json()["error"]["code"] == "google_not_configured"


def test_login_rate_limit(app, client, db):
    from backend.ratelimit import reset_memory_buckets
    from backend.tests.conftest import _make_user

    _make_user(db, "RL", "rl@test.in")
    # use a dedicated client on a private IP so shared buckets don't leak in
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "10.1.2.3"
    # A wide window keeps the bucket from rolling over mid-test (the default 60s
    # fixed window could reset the counter between two requests in the same
    # test), and clearing state keeps this independent of test ordering.
    original_limit = app.config["RATE_LIMIT_AUTH"]
    original_window = app.config.get("RATE_LIMIT_WINDOW_SECONDS")
    app.config["RATE_LIMIT_AUTH"] = 3
    app.config["RATE_LIMIT_WINDOW_SECONDS"] = 3600
    reset_memory_buckets()
    try:
        body = {"email": "rl@test.in", "password": "Password123"}
        codes = [c.post("/api/auth/login", json=body).status_code for _ in range(4)]
        assert codes[:3] == [200, 200, 200], codes
        assert codes[3] == 429, codes
        # the 429 is structured JSON and advertises a retry delay
        blocked = c.post("/api/auth/login", json=body)
        assert blocked.get_json()["error"]["code"] == "rate_limited"
        assert int(blocked.headers["Retry-After"]) > 0
    finally:
        app.config["RATE_LIMIT_AUTH"] = original_limit
        app.config["RATE_LIMIT_WINDOW_SECONDS"] = original_window
        reset_memory_buckets()