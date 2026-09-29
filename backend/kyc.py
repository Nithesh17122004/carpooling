"""Driver KYC: document collection, review workflow, and the publish gate.

A driver is only allowed to take paying passengers once their vehicle has been
verified. Verification is deliberately NOT self-asserted -- a driver cannot set
their own `verification_status` to `verified` through any endpoint -- because
the whole point of the control is that a human reviewer looked at the licence
and insurance.

State machine:

    unverified --submit--> submitted --approve--> verified
                          |     ^                   |
             reject ------+     |   re-upload       |
                 |             +-------------------+
                 v
             rejected --submit--> submitted

Two rules that matter most:

* **Every document re-upload resets verification to `unverified`.** Otherwise a
  driver could get a vehicle approved once and then swap in different documents,
  which is the obvious way to defeat the control.
* **Approval requires both documents present.** A verified vehicle with no
  insurance on file is worse than an unverified one, because it looks trustworthy.
"""

from flask import current_app

from .db import get_db, utcnow
from .errors import APIError

KYC_UNVERIFIED = "unverified"
KYC_SUBMITTED = "submitted"
KYC_VERIFIED = "verified"
KYC_REJECTED = "rejected"

KYC_STATES = {KYC_UNVERIFIED, KYC_SUBMITTED, KYC_VERIFIED, KYC_REJECTED}

# Only these two document slots participate in verification.
REQUIRED_DOCUMENTS = ("dl_document", "insurance_document")
REQUIRED_FIELDS = ("dl_number", "insurance_number")

_KYC_TRANSITIONS = {
    KYC_UNVERIFIED: {KYC_SUBMITTED},
    KYC_REJECTED: {KYC_SUBMITTED},
    KYC_SUBMITTED: {KYC_VERIFIED, KYC_REJECTED},
    KYC_VERIFIED: {KYC_UNVERIFIED},   # a new document re-opens verification
}


def kyc_enforced():
    """Whether the publish gate is active. On by default: a deployment that
    silently accepts unverified drivers is the failure mode this module exists
    to prevent, so switching it off is an explicit, visible act."""
    return bool(current_app.config.get("KYC_ENFORCE_PUBLISH", True))


def kyc_state(vehicle):
    return (vehicle or {}).get("verification_status") or KYC_UNVERIFIED


def can_transition_kyc(source, target):
    return target in _KYC_TRANSITIONS.get(source, set())


def missing_requirements(vehicle):
    """What is still needed before this vehicle can be submitted for review.

    Returned as a list rather than a boolean so the driver UI can show exactly
    what to fix instead of a generic "verification failed".
    """
    missing = []
    for field in REQUIRED_FIELDS:
        if not (vehicle.get(field) or "").strip():
            missing.append(field)
    for doc in REQUIRED_DOCUMENTS:
        stored = vehicle.get(doc) or {}
        if not stored.get("key"):
            missing.append(doc)
    return missing


def submit_for_verification(vehicle_id, user_id):
    """Move an owned vehicle into the review queue.

    Refuses to submit while documents are missing, so an admin never opens a
    review that is guaranteed to be rejected.
    """
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id, "user_id": user_id})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")

    state = kyc_state(vehicle)
    if state == KYC_VERIFIED:
        raise APIError("This vehicle is already verified.", 409, code="kyc_already_verified")
    if not can_transition_kyc(state, KYC_SUBMITTED):
        raise APIError("Verification is already under review.", 409, code="kyc_in_review")

    missing = missing_requirements(vehicle)
    if missing:
        raise APIError(
            "Add your licence and insurance details and documents before "
            "submitting for verification.", 422, code="kyc_documents_missing",
            details={"missing": missing})

    db.vehicles.update_one(
        {"_id": vehicle_id, "user_id": user_id},
        {"$set": {"verification_status": KYC_SUBMITTED,
                  "kyc_submitted_at": utcnow(),
                  "kyc_reviewed_at": None,
                  "kyc_reviewed_by": None,
                  "kyc_reason": None,
                  "updated_at": utcnow()}})
    return db.vehicles.find_one({"_id": vehicle_id})


def review_vehicle(vehicle_id, decision, reviewer_id, reason=None):
    """Admin decision on a submitted vehicle: `approve` or `reject`."""
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")

    decision = (decision or "").strip().lower()
    if decision in ("approve", "approved", "verify", "verified"):
        target = KYC_VERIFIED
    elif decision in ("reject", "rejected"):
        target = KYC_REJECTED
    else:
        raise APIError("decision must be 'approve' or 'reject'.", 422,
                       code="validation_error", details={"fields": ["decision"]})

    if not can_transition_kyc(kyc_state(vehicle), target):
        raise APIError(
            f"A vehicle in '{kyc_state(vehicle)}' cannot be reviewed.", 409,
            code="kyc_invalid_transition",
            details={"from": kyc_state(vehicle), "to": target})

    if target == KYC_REJECTED and not (reason or "").strip():
        # A rejection the driver cannot act on is a dead end.
        raise APIError("A reason is required when rejecting a vehicle.", 422,
                       code="kyc_reason_required")

    db.vehicles.update_one(
        {"_id": vehicle_id},
        {"$set": {"verification_status": target,
                  "kyc_reviewed_at": utcnow(),
                  "kyc_reviewed_by": reviewer_id,
                  "kyc_reason": (reason or None) if target == KYC_REJECTED else None,
                  "updated_at": utcnow()}})
    return db.vehicles.find_one({"_id": vehicle_id})


def reset_on_document_change(vehicle_id, user_id, doc_field):
    """Any new document re-opens verification.

    This is what stops a driver getting a vehicle approved once and then
    replacing the approved documents with something else.
    """
    db = get_db()
    if doc_field not in REQUIRED_DOCUMENTS:
        return None
    return db.vehicles.find_one_and_update(
        {"_id": vehicle_id, "user_id": user_id},
        {"$set": {"verification_status": KYC_UNVERIFIED,
                  "kyc_submitted_at": None,
                  "kyc_reviewed_at": None,
                  "kyc_reviewed_by": None,
                  "kyc_reason": None}})


def assert_can_publish(vehicle):
    """Gate ride publication on a verified vehicle.

    A vehicle with an in-flight review, or one rejected outright, is refused
    with a distinct reason so the driver knows whether to wait or resubmit.
    """
    if not kyc_enforced():
        return
    state = kyc_state(vehicle)
    if state == KYC_VERIFIED:
        return

    if state == KYC_SUBMITTED:
        raise APIError(
            "This vehicle is still being verified. You can publish once the "
            "review is complete.", 403, code="kyc_pending_review",
            details={"verification_status": state})
    if state == KYC_REJECTED:
        raise APIError(
            "This vehicle's verification was rejected. Update the details and "
            "resubmit.", 403, code="kyc_rejected",
            details={"verification_status": state, "reason": vehicle.get("kyc_reason")})
    raise APIError(
        "Verify your vehicle before publishing rides.", 403,
        code="kyc_required",
        details={"verification_status": state,
                 "missing": missing_requirements(vehicle)})


def kyc_summary(vehicle):
    """The view a driver needs: current state plus exactly what is outstanding."""
    state = kyc_state(vehicle)
    return {
        "verification_status": state,
        "can_submit": state in (KYC_UNVERIFIED, KYC_REJECTED)
                      and not missing_requirements(vehicle),
        "missing": missing_requirements(vehicle),
        "reason": vehicle.get("kyc_reason"),
        "submitted_at": vehicle.get("kyc_submitted_at"),
        "reviewed_at": vehicle.get("kyc_reviewed_at"),
    }
