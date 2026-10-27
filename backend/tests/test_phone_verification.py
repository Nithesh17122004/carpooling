"""Server-side phone verification requires a Firebase-verified phone claim."""

from backend.tests.conftest import _make_user, login


def _linked_user(db, email="phone-otp@test.in"):
    user = _make_user(db, "Phone User", email)
    db.users.update_one({"_id": user["_id"]}, {"$set": {"firebase_uid": "firebase-phone-user"}})
    return db.users.find_one({"_id": user["_id"]})


def test_phone_verified_only_after_matching_firebase_phone_claim(client, db, monkeypatch):
    user = _linked_user(db)
    db.users.update_one({"_id": user["_id"]}, {"$set": {"phone_verified": False}})
    session = login(client, user["email"])
    token = session.get_json()["token"]
    monkeypatch.setattr(
        "backend.blueprints.auth.verify_firebase_id_token",
        lambda _token: {
            "sub": "firebase-phone-user",
            "email": user["email"],
            "email_verified": True,
            "phone_number": "+919876543210",
        },
    )

    response = client.post(
        "/api/auth/phone/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={"id_token": "x" * 40},
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["user"]["phone"] == "+919876543210"
    assert response.get_json()["user"]["phone_verified"] is True
    stored = db.users.find_one({"_id": user["_id"]})
    assert stored["phone_verified"] is True


def test_phone_verification_rejects_different_firebase_uid(client, db, monkeypatch):
    user = _linked_user(db, "phone-mismatch@test.in")
    db.users.update_one({"_id": user["_id"]}, {"$set": {"phone_verified": False}})
    session = login(client, user["email"])
    token = session.get_json()["token"]
    monkeypatch.setattr(
        "backend.blueprints.auth.verify_firebase_id_token",
        lambda _token: {
            "sub": "some-other-firebase-user",
            "email": user["email"],
            "email_verified": True,
            "phone_number": "+919876543210",
        },
    )

    response = client.post(
        "/api/auth/phone/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={"id_token": "x" * 40},
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "firebase_account_mismatch"
    assert db.users.find_one({"_id": user["_id"]})["phone_verified"] is False


def test_phone_verification_rejects_token_without_phone_claim(client, db, monkeypatch):
    user = _linked_user(db, "phone-no-claim@test.in")
    db.users.update_one({"_id": user["_id"]}, {"$set": {"phone_verified": False}})
    session = login(client, user["email"])
    token = session.get_json()["token"]
    monkeypatch.setattr(
        "backend.blueprints.auth.verify_firebase_id_token",
        lambda _token: {
            "sub": "firebase-phone-user",
            "email": user["email"],
            "email_verified": True,
        },
    )

    response = client.post(
        "/api/auth/phone/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={"id_token": "x" * 40},
    )

    assert response.status_code == 422
    assert db.users.find_one({"_id": user["_id"]})["phone_verified"] is False


def test_changing_phone_resets_verified_flag(client, db):
    user = _linked_user(db, "phone-change@test.in")
    db.users.update_one({"_id": user["_id"]}, {"$set": {"phone_verified": True}})
    session = login(client, user["email"])
    token = session.get_json()["token"]

    response = client.patch(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"phone": "+919876543211"},
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["user"]["phone_verified"] is False
