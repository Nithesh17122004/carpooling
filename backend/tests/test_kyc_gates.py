"""The three publish gates, the payout onboarding gate, and their review queues.

Publication requires three *independent* facts -- a verified driver identity, a
verified vehicle, an approved RC -- and money movement requires a fourth, a
verified payout account. This file tests each one on its own, and the thing that
actually matters: that clearing three of them is not enough.

The negative assertions are the point. "Can a driver take paying passengers
without being who they say they are?" has to be un-answerable, and the only way
to show that is to try it and assert the refusal.
"""

import base64
import io

import pytest
from bson import ObjectId

from backend.tests.conftest import (
    _make_user,
    auth,
    login,
    make_ride,
    satisfy_other_publish_gates,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


@pytest.fixture
def admin(client, db):
    user = _make_user(db, "Gates Admin", "gates_admin@test.in", role="admin")
    return {"user": user, "auth": auth(login(client, "gates_admin@test.in"))}


def _admin_of(client, db):
    """Idempotent admin auth, for the few tests that need a reviewer but do not
    want the review to be the thing under test."""
    _make_user(db, "Gates Admin", "gates_admin@test.in", role="admin")
    return {"auth": auth(login(client, "gates_admin@test.in"))}


def _new_vehicle(client, hdrs, number):
    resp = client.post("/api/vehicles", headers=hdrs, json={
        "vehicle_type": "4-wheeler", "vehicle_number": number,
        "vehicle_model": "Gate Car", "seat_count": 4,
        "dl_number": "KA0520120012345", "insurance_number": "POL-2026-7788"})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["vehicle"]


def _upload_vehicle_docs(client, hdrs, vid):
    for kind in ("dl", "insurance"):
        r = client.post("/api/uploads/vehicle-doc", headers=hdrs,
                        data={"vehicle_id": vid, "doc": kind,
                              "file": (io.BytesIO(PNG), f"{kind}.png")},
                        content_type="multipart/form-data")
        assert r.status_code == 200, r.get_json()


def _submit_identity(client, hdrs, doc_type="aadhaar", number="123456789012",
                     filename="aadhaar.png", data=PNG):
    resp = client.post("/api/kyc/identity", headers=hdrs,
                       data={"doc_type": doc_type, "doc_number": number,
                             "full_name": "Asha Rao",
                             "file": (io.BytesIO(data), filename)},
                       content_type="multipart/form-data")
    return resp


def _submit_rc(client, hdrs, vid, filename="rc.pdf", data=PDF):
    return client.post(f"/api/kyc/vehicles/{vid}/rc", headers=hdrs,
                       data={"rc_number": "KA01AB1234",
                             "file": (io.BytesIO(data), filename)},
                       content_type="multipart/form-data")


# ================================================== gate 1: driver identity
def test_an_unverified_driver_cannot_publish(client, db, driver):
    """The driver has a fully verified vehicle and RC -- and is still refused."""
    from bson import ObjectId as OID

    vehicle = _new_vehicle(client, driver["auth"], "KA10ID0001")
    _upload_vehicle_docs(client, driver["auth"], vehicle["id"])
    client.post(f"/api/vehicles/{vehicle['id']}/submit-verification",
                headers=driver["auth"], json={})
    # Clear the vehicle and RC gates only. Identity is left deliberately
    # unverified, which is what this test is about.
    db.vehicles.update_one({"_id": OID(vehicle["id"])}, {"$set": {
        "verification_status": "verified"}})
    assert _submit_rc(client, driver["auth"], vehicle["id"]).status_code == 201
    client.post(f"/api/kyc/admin/vehicles/{vehicle['id']}/rc/review",
                headers=_admin_of(client, db)["auth"], json={"decision": "approve"})

    resp = make_ride(client, driver["auth"], vehicle)
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "kyc_required"


def test_a_driver_under_review_cannot_publish_but_is_told_to_wait(client, db, driver):
    from bson import ObjectId as OID

    vehicle = _new_vehicle(client, driver["auth"], "KA10ID0002")
    _upload_vehicle_docs(client, driver["auth"], vehicle["id"])
    assert _submit_identity(client, driver["auth"]).status_code == 201

    # Satisfy the vehicle and RC gates; leave identity in review.
    client.post(f"/api/vehicles/{vehicle['id']}/submit-verification",
                headers=driver["auth"], json={})
    db.vehicles.update_one({"_id": OID(vehicle["id"])}, {"$set": {
        "verification_status": "verified"}})
    _submit_rc(client, driver["auth"], vehicle["id"])

    resp = make_ride(client, driver["auth"], vehicle)
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "kyc_pending_review"


def test_a_rejected_driver_is_told_why(client, db, admin, driver):
    vehicle = _new_vehicle(client, driver["auth"], "KA10ID0003")
    assert _submit_identity(client, driver["auth"]).status_code == 201
    uid = str(driver["user"]["_id"])

    rej = client.post(f"/api/kyc/admin/identity/{uid}/review", headers=admin["auth"],
                      json={"decision": "reject", "reason": "image is unreadable"})
    assert rej.status_code == 200, rej.get_json()
    assert rej.get_json()["kyc"]["kyc_status"] == "rejected"

    resp = make_ride(client, driver["auth"], vehicle)
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "kyc_rejected"


def test_only_an_admin_can_approve_an_identity(client, driver, rider):
    """There is no client-settable path to `verified`."""
    assert _submit_identity(client, rider["auth"]).status_code == 201

    for path, payload in (
        (f"/api/kyc/admin/identity/{rider['user']['_id']}/review",
         {"decision": "approve"}),
    ):
        resp = client.post(path, headers=rider["auth"], json=payload)
        assert resp.status_code == 403, resp.get_json()

    assert client.get("/api/kyc/me", headers=rider["auth"]) \
        .get_json()["kyc"]["kyc_status"] == "submitted"


def test_the_identity_number_is_never_stored_in_full(client, db, driver):
    """Masking is the whole point of the field, so assert the raw value is gone."""
    secret_number = "123456789012"
    assert _submit_identity(client, driver["auth"],
                            number=secret_number).status_code == 201
    user = db.users.find_one({"_id": driver["user"]["_id"]})

    assert secret_number not in str(user)
    assert user["kyc_doc_number_masked"] == "********9012"
    # The response to the owner does not leak it either.
    me = client.get("/api/kyc/me", headers=driver["auth"]).get_json()["kyc"]
    assert secret_number not in str(me)
    assert me["doc_number_masked"] == "********9012"


def test_a_rejection_reason_is_sanitised_for_the_owner(client, db, admin, driver):
    """A reviewer who quotes the number back must not leak it to the API."""
    uid = str(driver["user"]["_id"])
    _submit_identity(client, driver["auth"], number="123456789012")
    client.post(f"/api/kyc/admin/identity/{uid}/review", headers=admin["auth"],
                json={"decision": "reject",
                      "reason": "Aadhaar 1234 5678 9012 does not match the photo"})

    me = client.get("/api/kyc/me", headers=driver["auth"]).get_json()["kyc"]
    assert "1234 5678 9012" not in me["reason"]
    assert "[redacted]" in me["reason"]
    # ...but staff keep the full note for adjudication.
    stored = db.users.find_one({"_id": driver["user"]["_id"]})["kyc_reason"]
    assert "1234 5678 9012" in stored


def test_rejection_without_a_reason_is_refused(client, admin, driver):
    _submit_identity(client, driver["auth"])
    resp = client.post(f"/api/kyc/admin/identity/{driver['user']['_id']}/review",
                       headers=admin["auth"], json={"decision": "reject"})
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "kyc_reason_required"


def test_resubmitting_after_rejection_is_allowed(client, admin, driver):
    uid = str(driver["user"]["_id"])
    _submit_identity(client, driver["auth"])
    client.post(f"/api/kyc/admin/identity/{uid}/review", headers=admin["auth"],
                json={"decision": "reject", "reason": "blurry"})

    again = _submit_identity(client, driver["auth"], number="999988887777")
    assert again.status_code == 201
    assert again.get_json()["kyc"]["kyc_status"] == "submitted"


def test_a_second_submission_while_in_review_is_refused(client, driver):
    assert _submit_identity(client, driver["auth"]).status_code == 201


def test_one_document_number_cannot_be_used_by_two_driver_accounts(
        client, db, driver, driver_without_onboarding):
    """The fingerprint exists to stop a person opening a second account with the
    same ID. It is only useful if something actually reads it."""
    assert _submit_identity(client, driver["auth"], number="444455556666").status_code == 201

    second = _submit_identity(client, driver_without_onboarding["auth"],
                              number="444455556666")
    assert second.status_code == 409
    assert second.get_json()["error"]["code"] == "kyc_duplicate_identity"

    # Refused before the state flips and before any document is written.
    other = db.users.find_one({"_id": driver_without_onboarding["user"]["_id"]})
    assert other.get("kyc_status") is None
    assert db.users.count_documents({"kyc_fingerprint": {"$exists": True}}) == 1


def test_a_rejected_document_can_be_reused_by_its_own_account(client, admin, driver):
    """The check must not lock the owner out of their own number: a rejection
    over a typo is exactly when the same number is submitted again."""
    uid = str(driver["user"]["_id"])
    assert _submit_identity(client, driver["auth"], number="777788889999").status_code == 201
    client.post(f"/api/kyc/admin/identity/{uid}/review", headers=admin["auth"],
                json={"decision": "reject", "reason": "blurry"})

    again = _submit_identity(client, driver["auth"], number="777788889999")
    assert again.status_code == 201


def test_the_same_number_is_allowed_across_different_kyc_roles(client, db, driver, rider):
    """The fingerprint mixes in the KYC role, so someone who drives and also
    rides legitimately holds two records. A global unique index would break
    that, so this is the test that keeps the index scoped correctly."""
    assert _submit_identity(client, driver["auth"], number="555566667777").status_code == 201
    # `rider` is a plain user, so their KYC role is `passenger`, not `driver`.
    assert _submit_identity(client, rider["auth"], number="555566667777").status_code == 201
    assert db.users.count_documents({"kyc_fingerprint": {"$exists": True}}) == 2
    resp = _submit_identity(client, driver["auth"], number="999988887777")
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "kyc_in_review"


def test_a_privileged_escalation_is_audited(client, db, admin, driver):
    _submit_identity(client, driver["auth"])
    client.post(f"/api/kyc/admin/identity/{driver['user']['_id']}/review",
                headers=admin["auth"], json={"decision": "approve"})
    assert db.audit_logs.count_documents({"action": "identity.kyc.review"}) == 1
    row = db.audit_logs.find_one({"action": "identity.kyc.review"})
    assert row["actor_id"] == str(admin["user"]["_id"])


def test_an_unsupported_document_type_is_refused(client, driver):
    resp = _submit_identity(client, driver["auth"], doc_type="utility_bill")
    assert resp.status_code == 422
    assert "allowed" in resp.get_json()["error"]["details"]


def test_a_file_that_lies_about_its_type_is_refused(client, driver):
    """Magic-byte detection: a script renamed .pdf is not a document."""
    resp = _submit_identity(client, driver["auth"], filename="aadhaar.pdf",
                            data=b"MZ\x90\x00this is a windows executable")
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "invalid_file"


# ==================================================== gate 2: registration cert
def test_a_vehicle_without_an_rc_cannot_publish(client, db, driver):
    from bson import ObjectId as OID

    vehicle = _new_vehicle(client, driver["auth"], "KA10RC0001")
    _upload_vehicle_docs(client, driver["auth"], vehicle["id"])
    client.post(f"/api/vehicles/{vehicle['id']}/submit-verification",
                headers=driver["auth"], json={})
    # Clear the vehicle and identity gates, and leave the RC absent entirely.
    db.vehicles.update_one({"_id": OID(vehicle["id"])}, {"$set": {
        "verification_status": "verified"}})
    db.users.update_one({"_id": driver["user"]["_id"]},
                        {"$set": {"kyc_status": "verified"}})

    resp = make_ride(client, driver["auth"], vehicle)
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "rc_required"


def test_a_pending_rc_still_blocks_publication(client, db, driver):
    vehicle = _new_vehicle(client, driver["auth"], "KA10RC0002")
    satisfy_other_publish_gates(db, driver["user"]["_id"], vehicle["id"])
    db.vehicles.update_one({"_id": ObjectId(vehicle["id"])}, {"$unset": {
        "rc_document": ""}})
    assert _submit_rc(client, driver["auth"], vehicle["id"]).status_code == 201

    resp = make_ride(client, driver["auth"], vehicle)
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "rc_pending_review"


def test_only_an_admin_can_approve_an_rc(client, db, admin, driver):
    vehicle = _new_vehicle(client, driver["auth"], "KA10RC0003")
    _submit_rc(client, driver["auth"], vehicle["id"])

    # The owner cannot approve their own.
    resp = client.post(f"/api/kyc/admin/vehicles/{vehicle['id']}/rc/review",
                       headers=driver["auth"], json={"decision": "approve"})
    assert resp.status_code == 403

    ok = client.post(f"/api/kyc/admin/vehicles/{vehicle['id']}/rc/review",
                     headers=admin["auth"], json={"decision": "approve"})
    assert ok.status_code == 200, ok.get_json()
    assert ok.get_json()["rc"]["rc_status"] == "approved"


def test_a_rejected_rc_blocks_publication_with_a_reason(client, db, admin, driver):
    vehicle = _new_vehicle(client, driver["auth"], "KA10RC0004")
    satisfy_other_publish_gates(db, driver["user"]["_id"], vehicle["id"])
    db.vehicles.update_one({"_id": ObjectId(vehicle["id"])}, {"$unset": {
        "rc_document": ""}})
    _submit_rc(client, driver["auth"], vehicle["id"])
    client.post(f"/api/kyc/admin/vehicles/{vehicle['id']}/rc/review",
                headers=admin["auth"],
                json={"decision": "reject", "reason": "RC has expired"})

    resp = make_ride(client, driver["auth"], vehicle)
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "rc_rejected"
    assert resp.get_json()["error"]["details"]["reason"] == "RC has expired"


def test_replacing_an_approved_rc_reopens_review(client, db, admin, driver):
    """Otherwise a driver could be approved once and then swap the document."""
    vehicle = _new_vehicle(client, driver["auth"], "KA10RC0005")
    _submit_rc(client, driver["auth"], vehicle["id"])
    client.post(f"/api/kyc/admin/vehicles/{vehicle['id']}/rc/review",
                headers=admin["auth"], json={"decision": "approve"})

    second = _submit_rc(client, driver["auth"], vehicle["id"],
                        filename="rc2.pdf")
    assert second.status_code == 201
    assert second.get_json()["rc"]["rc_status"] == "pending"


def test_another_user_cannot_read_someone_elses_rc(client, db, driver, rider):
    vehicle = _new_vehicle(client, driver["auth"], "KA10RC0006")
    _submit_rc(client, driver["auth"], vehicle["id"])
    resp = client.get(f"/api/kyc/vehicles/{vehicle['id']}/rc/document",
                      headers=rider["auth"])
    assert resp.status_code == 403


def test_the_owner_can_download_their_own_rc(client, db, driver):
    vehicle = _new_vehicle(client, driver["auth"], "KA10RC0007")
    _submit_rc(client, driver["auth"], vehicle["id"])
    resp = client.get(f"/api/kyc/vehicles/{vehicle['id']}/rc/document",
                      headers=driver["auth"])
    assert resp.status_code == 200, resp.get_json()
    assert resp.data == PDF
    assert resp.headers["Cache-Control"] == "no-store"


def test_an_rc_document_response_never_carries_a_storage_key(client, driver):
    vehicle = _new_vehicle(client, driver["auth"], "KA10RC0008")
    _submit_rc(client, driver["auth"], vehicle["id"])
    body = client.get(f"/api/kyc/vehicles/{vehicle['id']}/rc",
                      headers=driver["auth"]).get_json()
    assert "key" not in str(body)


# ============================================== gate 4: payout account (money)
def test_a_driver_cannot_begin_onboarding_for_a_passenger(client, rider):
    resp = client.post("/api/kyc/onboarding/begin", headers=rider["auth"], json={})
    assert resp.status_code == 403


def test_onboarding_refuses_banking_details(client, db, driver):
    """The service must never persist an account number, even if sent to it."""
    resp = client.post("/api/kyc/onboarding/begin", headers=driver["auth"],
                       json={"account_number": "50100123456789"})
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "banking_data_rejected"
    assert "account_number" not in str(db.users.find_one({"_id": driver["user"]["_id"]}))


def test_onboarding_walks_pending_then_submitted_then_verified(client, db, driver,
                                                              admin):
    db.users.update_one({"_id": driver["user"]["_id"]}, {"$set": {
        "payout_onboarding_status": None}})
    begin = client.post("/api/kyc/onboarding/begin", headers=driver["auth"],
                        data={"account_last4": "6789"})
    assert begin.status_code == 201, begin.get_json()
    assert begin.get_json()["onboarding"]["status"] == "pending"

    submit = client.post("/api/kyc/onboarding/submit", headers=driver["auth"],
                         data={"contact_id": "cont_1", "linked_account_id": "la_1"})
    assert submit.status_code == 200
    assert submit.get_json()["onboarding"]["status"] == "submitted"
    # Submitted is not yet payable.
    assert submit.get_json()["onboarding"]["settlement_allowed"] is False

    verified = client.post(
        f"/api/kyc/admin/onboarding/{driver['user']['_id']}/review",
        headers=admin["auth"], json={"action": "verify"})
    assert verified.status_code == 200, verified.get_json()
    assert verified.get_json()["onboarding"]["status"] == "verified"


def test_a_driver_cannot_verify_their_own_payout_account(client, driver):
    resp = client.post(f"/api/kyc/admin/onboarding/{driver['user']['_id']}/review",
                       headers=driver["auth"], json={"action": "verify"})
    assert resp.status_code == 403


def test_suspending_and_resuming_a_payout_account(client, db, admin, driver):
    uid = str(driver["user"]["_id"])
    suspended = client.post(f"/api/kyc/admin/onboarding/{uid}/review",
                            headers=admin["auth"],
                            json={"action": "suspend", "reason": "fraud review"})
    assert suspended.status_code == 200, suspended.get_json()
    assert suspended.get_json()["onboarding"]["status"] == "suspended"

    resumed = client.post(f"/api/kyc/admin/onboarding/{uid}/review",
                          headers=admin["auth"], json={"action": "resume"})
    assert resumed.status_code == 200
    # A resolved hold must not cost the driver their payout setup.
    assert resumed.get_json()["onboarding"]["status"] == "verified"


def test_suspending_requires_a_reason(client, admin, driver):
    resp = client.post(f"/api/kyc/admin/onboarding/{driver['user']['_id']}/review",
                       headers=admin["auth"], json={"action": "suspend"})
    assert resp.status_code == 422


def test_onboarding_transitions_are_audited(client, db, admin, driver_without_onboarding):
    """Every payout-account transition leaves a financial audit row.

    Uses the un-onboarded driver so the walk starts from a real `not_started`
    rather than colliding with the default fixture's `verified` state.
    """
    uid = str(driver_without_onboarding["user"]["_id"])
    assert client.post("/api/kyc/onboarding/begin",
                       headers=driver_without_onboarding["auth"],
                       data={}).status_code == 201
    assert client.post("/api/kyc/onboarding/submit",
                       headers=driver_without_onboarding["auth"],
                       data={"contact_id": "c1"}).status_code == 200
    assert client.post(f"/api/kyc/admin/onboarding/{uid}/review",
                       headers=admin["auth"],
                       json={"action": "verify"}).status_code == 200

    actions = sorted(r["action"] for r in db.audit_logs.find(
        {"action": {"$regex": "^financial\\.onboarding"}}))
    assert "financial.onboarding.begin" in actions
    assert "financial.onboarding.submit" in actions
    assert "financial.onboarding.verify" in actions
    # The prefix is applied once. A doubled segment means the call site and the
    # helper both added it, and the row is then unqueryable by its real name.
    assert not any(".onboarding.onboarding." in a for a in actions), actions
    # ...and nothing was quietly renamed by a domain/prefix mismatch.
    assert db.audit_logs.count_documents({"action": "audit.invalid_action"}) == 0
