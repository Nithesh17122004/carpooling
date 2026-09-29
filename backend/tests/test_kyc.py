"""Driver KYC: collection, review, the publish gate, and the anti-swap rule.

The whole point of the control is that a driver cannot mark their own vehicle as
verified, and cannot keep a verification after changing the documents it was
based on. Those two properties are what most of this file asserts.
"""

import base64
import io

import pytest
from bson import ObjectId

from backend.kyc import (
    KYC_REJECTED,
    KYC_SUBMITTED,
    KYC_UNVERIFIED,
    KYC_VERIFIED,
    kyc_state,
    missing_requirements,
)
from backend.tests.conftest import _make_user, auth, login, make_ride

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture
def admin(client, db):
    user = _make_user(db, "KYC Admin", "kyc_admin@test.in", role="admin")
    return {"user": user, "auth": auth(login(client, "kyc_admin@test.in"))}


def _upload(client, auth_hdr, vehicle_id, kind):
    resp = client.post("/api/uploads/vehicle-doc", headers=auth_hdr,
                       data={"vehicle_id": vehicle_id, "doc": kind,
                             "file": (io.BytesIO(PNG), f"{kind}.png")},
                       content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _complete_kyc(client, db, driver, seq, number="KA01AB1234"):
    """A vehicle with licence + insurance details AND both documents on file."""
    created = client.post("/api/vehicles", headers=driver["auth"], json={
        "vehicle_type": "4-wheeler", "vehicle_number": f"KA01ZZ{seq:04d}",
        "vehicle_model": "KYC Car", "seat_count": 4,
        "dl_number": "KA0520120012345", "insurance_number": "POL-2026-7788"})
    assert created.status_code == 201, created.get_json()
    vid = created.get_json()["vehicle"]["id"]
    _upload(client, driver["auth"], vid, "dl")
    _upload(client, driver["auth"], vid, "insurance")
    return vid


# ------------------------------------------------------- a new vehicle is unverified
def test_a_new_vehicle_starts_unverified(client, driver):
    created = client.post("/api/vehicles", headers=driver["auth"], json={
        "vehicle_type": "4-wheeler", "vehicle_number": "KA02BB2222",
        "vehicle_model": "Fresh Car", "seat_count": 4})
    assert created.status_code == 201
    vehicle = created.get_json()["vehicle"]
    assert vehicle["verification_status"] == KYC_UNVERIFIED
    assert vehicle["kyc"]["missing"]


def test_a_driver_cannot_self_declare_verification(client, db, driver):
    """There is no client-settable path to `verified`, on create or on update."""
    created = client.post("/api/vehicles", headers=driver["auth"], json={
        "vehicle_type": "4-wheeler", "vehicle_number": "KA03CC3333",
        "seat_count": 4, "verification_status": "verified",
        "kyc_reviewed_by": "me"})
    assert created.status_code == 201
    vid = created.get_json()["vehicle"]["id"]
    assert db.vehicles.find_one({"_id": ObjectId(vid)})["verification_status"] == KYC_UNVERIFIED

    patched = client.patch(f"/api/vehicles/{vid}", headers=driver["auth"],
                           json={"verification_status": "verified", "kyc_reason": "trust me"})
    assert patched.status_code == 200
    stored = db.vehicles.find_one({"_id": ObjectId(vid)})
    assert stored["verification_status"] == KYC_UNVERIFIED
    assert stored.get("kyc_reason") in (None, "")


# ------------------------------------------------------------- the publish gate
def test_an_unverified_driver_cannot_publish(client, db, driver, vehicle):
    vid = vehicle["id"]
    db.vehicles.update_one({"_id": ObjectId(vid)},
                           {"$set": {"verification_status": KYC_UNVERIFIED}})
    resp = make_ride(client, driver["auth"], vehicle)
    assert resp.status_code == 403, resp.get_json()
    assert resp.get_json()["error"]["code"] == "kyc_required"
    # and no ride was written
    assert db.rides.count_documents({"owner_id": driver["user"]["_id"]}) == 0


def test_a_driver_waiting_on_review_is_told_to_wait(client, db, driver, vehicle):
    db.vehicles.update_one({"_id": ObjectId(vehicle["id"])},
                           {"$set": {"verification_status": KYC_SUBMITTED}})
    resp = make_ride(client, driver["auth"], vehicle)
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "kyc_pending_review"


def test_a_rejected_driver_is_told_why(client, db, driver, vehicle):
    db.vehicles.update_one({"_id": ObjectId(vehicle["id"])},
                           {"$set": {"verification_status": KYC_REJECTED,
                                     "kyc_reason": "insurance expired in March"}})
    resp = make_ride(client, driver["auth"], vehicle)
    assert resp.status_code == 403
    err = resp.get_json()["error"]
    assert err["code"] == "kyc_rejected"
    assert err["details"]["reason"] == "insurance expired in March"


def test_a_verified_driver_can_publish(client, db, driver, vehicle):
    db.vehicles.update_one({"_id": ObjectId(vehicle["id"])},
                           {"$set": {"verification_status": KYC_VERIFIED}})
    assert make_ride(client, driver["auth"], vehicle).status_code == 201


# ------------------------------------------------------------------- submission
def test_submission_requires_both_documents_and_details(client, db, driver):
    created = client.post("/api/vehicles", headers=driver["auth"], json={
        "vehicle_type": "4-wheeler", "vehicle_number": "KA04DD4444", "seat_count": 4})
    vid = created.get_json()["vehicle"]["id"]

    early = client.post(f"/api/vehicles/{vid}/submit-verification",
                        headers=driver["auth"], json={})
    assert early.status_code == 422
    assert early.get_json()["error"]["code"] == "kyc_documents_missing"
    assert set(early.get_json()["error"]["details"]["missing"]) == {
        "dl_number", "insurance_number", "dl_document", "insurance_document"}

    # details alone are not enough
    client.patch(f"/api/vehicles/{vid}", headers=driver["auth"], json={
        "dl_number": "KA0520120099995", "insurance_number": "POL-2026-1234"})
    still = client.post(f"/api/vehicles/{vid}/submit-verification",
                        headers=driver["auth"], json={})
    assert still.status_code == 422
    assert set(still.get_json()["error"]["details"]["missing"]) == {
        "dl_document", "insurance_document"}


def test_a_complete_packet_can_be_submitted(client, db, driver):
    vid = _complete_kyc(client, db, driver, 1)
    status = client.get(f"/api/vehicles/{vid}/kyc", headers=driver["auth"])
    assert status.status_code == 200
    assert status.get_json()["kyc"]["can_submit"] is True
    assert status.get_json()["kyc"]["missing"] == []

    submitted = client.post(f"/api/vehicles/{vid}/submit-verification",
                            headers=driver["auth"], json={})
    assert submitted.status_code == 200, submitted.get_json()
    assert submitted.get_json()["vehicle"]["verification_status"] == KYC_SUBMITTED
    assert db.vehicles.find_one({"_id": ObjectId(vid)})["kyc_submitted_at"] is not None


def test_double_submission_is_refused(client, db, driver):
    vid = _complete_kyc(client, db, driver, 2)
    client.post(f"/api/vehicles/{vid}/submit-verification", headers=driver["auth"], json={})
    again = client.post(f"/api/vehicles/{vid}/submit-verification",
                        headers=driver["auth"], json={})
    assert again.status_code == 409
    assert again.get_json()["error"]["code"] == "kyc_in_review"


def test_only_the_owner_can_submit(client, db, driver, rider):
    vid = _complete_kyc(client, db, driver, 3)
    resp = client.post(f"/api/vehicles/{vid}/submit-verification",
                       headers=rider["auth"], json={})
    assert resp.status_code == 404   # not found for this owner, not a leak


# --------------------------------------------------------------------- review
def test_admin_approval_verifies_the_vehicle(client, db, admin, driver):
    vid = _complete_kyc(client, db, driver, 4)
    client.post(f"/api/vehicles/{vid}/submit-verification", headers=driver["auth"], json={})

    approved = client.post(f"/api/admin/vehicles/{vid}/verify", headers=admin["auth"],
                           json={"decision": "approve"})
    assert approved.status_code == 200, approved.get_json()
    assert approved.get_json()["vehicle"]["verification_status"] == KYC_VERIFIED
    assert db.audit_logs.count_documents({"action": "vehicle.verify"}) == 1
    assert make_ride(client, driver["auth"],
                     client.get(f"/api/vehicles/{vid}", headers=driver["auth"]
                                ).get_json()["vehicle"]).status_code == 201


def test_rejection_requires_a_reason(client, db, admin, driver):
    vid = _complete_kyc(client, db, driver, 5)
    client.post(f"/api/vehicles/{vid}/submit-verification", headers=driver["auth"], json={})

    silent = client.post(f"/api/admin/vehicles/{vid}/verify", headers=admin["auth"],
                         json={"decision": "reject"})
    assert silent.status_code == 422
    assert silent.get_json()["error"]["code"] == "kyc_reason_required"

    rejected = client.post(f"/api/admin/vehicles/{vid}/verify", headers=admin["auth"],
                           json={"decision": "reject", "reason": "RC is unreadable"})
    assert rejected.status_code == 200
    assert rejected.get_json()["vehicle"]["verification_status"] == KYC_REJECTED
    assert rejected.get_json()["vehicle"]["kyc_reason"] == "RC is unreadable"


def test_a_rejected_driver_can_resubmit(client, db, admin, driver):
    vid = _complete_kyc(client, db, driver, 6)
    client.post(f"/api/vehicles/{vid}/submit-verification", headers=driver["auth"], json={})
    client.post(f"/api/admin/vehicles/{vid}/verify", headers=admin["auth"],
                json={"decision": "reject", "reason": "blurry photo"})

    retry = client.post(f"/api/vehicles/{vid}/submit-verification",
                        headers=driver["auth"], json={})
    assert retry.status_code == 200
    assert retry.get_json()["vehicle"]["verification_status"] == KYC_SUBMITTED
    # the stale rejection reason is cleared so the driver is not confused
    assert db.vehicles.find_one({"_id": ObjectId(vid)})["kyc_reason"] is None


def test_a_pending_vehicle_cannot_be_reviewed(client, db, admin, driver):
    vid = _complete_kyc(client, db, driver, 7)   # never submitted
    resp = client.post(f"/api/admin/vehicles/{vid}/verify", headers=admin["auth"],
                       json={"decision": "approve"})
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "kyc_invalid_transition"


def test_a_driver_cannot_review_their_own_vehicle(client, db, driver):
    vid = _complete_kyc(client, db, driver, 8)
    client.post(f"/api/vehicles/{vid}/submit-verification", headers=driver["auth"], json={})
    resp = client.post(f"/api/admin/vehicles/{vid}/verify", headers=driver["auth"],
                       json={"decision": "approve"})
    assert resp.status_code == 403


# ----------------------------------------------------- the anti-swap guarantee
def test_reuploading_a_document_revokes_verification(client, db, admin, driver):
    """The rule that stops a driver getting approved once and then swapping the
    documents the approval was based on."""
    vid = _complete_kyc(client, db, driver, 9)
    client.post(f"/api/vehicles/{vid}/submit-verification", headers=driver["auth"], json={})
    client.post(f"/api/admin/vehicles/{vid}/verify", headers=admin["auth"],
                json={"decision": "approve"})
    assert db.vehicles.find_one({"_id": ObjectId(vid)})["verification_status"] == KYC_VERIFIED

    # a different insurance document arrives
    resp = _upload(client, driver["auth"], vid, "insurance")
    assert resp["verification_status"] == KYC_UNVERIFIED

    stored = db.vehicles.find_one({"_id": ObjectId(vid)})
    assert stored["verification_status"] == KYC_UNVERIFIED
    assert stored["kyc_reviewed_by"] is None
    # and they can no longer publish
    vehicle = client.get(f"/api/vehicles/{vid}", headers=driver["auth"]).get_json()["vehicle"]
    assert make_ride(client, driver["auth"], vehicle).status_code == 403


def test_reuploading_the_same_doc_type_also_revokes(client, db, admin, driver):
    vid = _complete_kyc(client, db, driver, 10)
    client.post(f"/api/vehicles/{vid}/submit-verification", headers=driver["auth"], json={})
    client.post(f"/api/admin/vehicles/{vid}/verify", headers=admin["auth"],
                json={"decision": "approve"})
    _upload(client, driver["auth"], vid, "dl")
    assert db.vehicles.find_one({"_id": ObjectId(vid)})["verification_status"] == KYC_UNVERIFIED


# ------------------------------------------------------------------- privacy
def test_documents_are_never_served_from_the_public_uploads_route(client, db, driver):
    """KYC documents must not be reachable through the public /uploads route.

    That route exists for avatars; if a document key ever leaked into it, every
    licence in the fleet would become world-readable.
    """
    from backend import storage

    vid = _complete_kyc(client, db, driver, 13)
    key = db.vehicles.find_one({"_id": ObjectId(vid)})["dl_document"]["key"]
    assert storage.is_public_avatar(key) is False

    anon = client.get(f"/uploads/{key}")
    assert anon.status_code == 404, anon.get_json()


def test_a_rider_cannot_read_a_drivers_documents(client, db, driver, rider):
    vid = _complete_kyc(client, db, driver, 12)
    denied = client.get(f"/api/uploads/vehicle-doc/{vid}/dl", headers=rider["auth"])
    assert denied.status_code in (403, 404), denied.get_json()
    # the owner can
    owner = client.get(f"/api/uploads/vehicle-doc/{vid}/dl", headers=driver["auth"])
    assert owner.status_code == 200


def test_anonymous_cannot_read_documents(client, db, driver):
    vid = _complete_kyc(client, db, driver, 14)
    assert client.get(f"/api/uploads/vehicle-doc/{vid}/dl").status_code in (401, 403, 404)
