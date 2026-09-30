"""Vehicle registration certificate (RC) verification.

The existing vehicle KYC checks a driving licence and insurance. The RC is a
separate document with a different failure mode -- it proves the *vehicle* is
registered and roadworthy, not that the person may drive -- so it gets its own
three-state machine rather than being folded into the existing four-state one.

    (none) --upload+submit--> pending --approve--> approved
                                |                    |
                            reject                  | (new document)
                                v                    v
                            rejected <---------------+

The states are exactly the `pending | approved | rejected` vocabulary the review
queue and the driver UI share. `not_uploaded` is deliberately *not* a state here:
it is the absence of a state, and conflating "never uploaded" with "rejected"
would tell a driver to re-submit a document nobody has looked at yet.

Documents use the same private, encrypted, never-publicly-addressable storage as
identity papers (`backend.documents`), and the publish gate in `kyc.py` refuses
any vehicle whose RC is not approved.
"""

import secrets

from flask import current_app

from .db import get_db, utcnow
from .documents import (
    store_private_document,
    delete_private_document,
    public_metadata,
)
from .errors import APIError
from .timeutil import iso_utc

RC_PENDING = "pending"
RC_APPROVED = "approved"
RC_REJECTED = "rejected"

RC_STATES = {RC_PENDING, RC_APPROVED, RC_REJECTED}

_RC_TRANSITIONS = {
    RC_PENDING: {RC_APPROVED, RC_REJECTED},
    RC_APPROVED: {RC_PENDING},   # a replacement document re-opens review
    RC_REJECTED: {RC_PENDING},
}

# The queue admins actually work through: documents waiting on a human.
RC_REVIEW_QUEUE = {RC_PENDING}

_MAX_REASON = 300

# Field names a client must not use to smuggle data into the RC record.
_FORBIDDEN_FIELDS = ("account_number", "ifsc", "upi", "card_number", "cvv",
                     "secret", "token", "password", "api_key")


def enforce_publish():
    return bool(current_app.config.get("RC_VERIFY_ENFORCE_PUBLISH", True))


def rc_state(vehicle):
    """Current RC state, or None when no RC has ever been submitted."""
    if not vehicle:
        return None
    doc = vehicle.get("rc_document") or {}
    if not doc.get("key"):
        return None
    return doc.get("verification_status") or RC_PENDING


def can_transition_rc(source, target):
    if source is None:
        return target == RC_PENDING
    return target in _RC_TRANSITIONS.get(source, set())


def assert_no_banking_data(payload):
    if not isinstance(payload, dict):
        return
    lowered = {str(k).lower() for k in payload}
    bad = sorted(lowered.intersection(_FORBIDDEN_FIELDS))
    if bad:
        raise APIError(
            "Banking details and secrets must not be sent to this service.", 422,
            code="banking_data_rejected", details={"rejected_fields": bad})


def submit_rc(vehicle_id, user_id, *, data, declared_filename=None, rc_number=None):
    """Upload (or replace) the RC and put the vehicle into the review queue.

    A replacement always returns the vehicle to `pending`, even from `approved`:
    an approved vehicle that then swaps in a different registration certificate
    is exactly the attack the gate exists to stop.
    """
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id, "user_id": user_id})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")

    source = rc_state(vehicle)
    if source == RC_PENDING:
        raise APIError("This RC is already under review.", 409, code="rc_in_review")

    doc_id = secrets.token_hex(12)
    meta = store_private_document(user_id, "rc", doc_id, data, declared_filename)
    meta.update({
        "verification_status": RC_PENDING,
        "rc_number_masked": _mask(rc_number),
        "submitted_at": utcnow(),
        "reviewed_at": None,
        "reviewed_by": None,
        "reason": None,
    })
    previous = (vehicle.get("rc_document") or {}).get("key")

    updated = db.vehicles.find_one_and_update(
        {"_id": vehicle_id, "user_id": user_id},
        {"$set": {"rc_document": meta, "updated_at": utcnow()}},
    )
    if updated is None:
        delete_private_document(meta.get("key"))
        raise APIError("Vehicle not found.", 404, code="not_found")
    if previous and previous != meta.get("key"):
        delete_private_document(previous)
    return db.vehicles.find_one({"_id": vehicle_id})


def _mask(value):
    text = "".join(ch for ch in str(value or "") if ch.isalnum())
    if not text:
        return None
    return ("*" * max(len(text) - 4, 0)) + text[-4:]


def review_rc(vehicle_id, decision, reviewer_id, reason=None):
    """Admin decision on a pending RC. The only path to `approved`."""
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")

    decision = (decision or "").strip().lower()
    if decision in ("approve", "approved", "verify", "verified"):
        target = RC_APPROVED
    elif decision in ("reject", "rejected"):
        target = RC_REJECTED
    else:
        raise APIError("decision must be 'approve' or 'reject'.", 422,
                       code="validation_error", details={"fields": ["decision"]})

    source = rc_state(vehicle)
    if not can_transition_rc(source, target):
        raise APIError(
            "An RC in '%s' cannot be reviewed." % (source or "no document"), 409,
            code="rc_invalid_transition", details={"from": source, "to": target})
    if target == RC_REJECTED and not (reason or "").strip():
        raise APIError("A reason is required when rejecting an RC.", 422,
                       code="rc_reason_required")

    now = utcnow()
    db.vehicles.update_one(
        {"_id": vehicle_id, "rc_document.verification_status": source},
        {"$set": {
            "rc_document.verification_status": target,
            "rc_document.reviewed_at": now,
            "rc_document.reviewed_by": reviewer_id,
            "rc_document.reason": (str(reason).strip()[:_MAX_REASON] or None)
            if target == RC_REJECTED else None,
            "updated_at": now,
        }},
    )
    from . import audit

    audit.record("identity.rc.review", domain=audit.IDENTITY, actor_id=reviewer_id,
                 target_type="vehicle", target_id=vehicle_id,
                 meta={"to": target, "from": source}, reason=reason)
    return db.vehicles.find_one({"_id": vehicle_id})


def assert_rc_approved(vehicle):
    """Publish gate for the RC."""
    if not enforce_publish():
        return
    state = rc_state(vehicle)
    if state == RC_APPROVED:
        return
    details = {"rc_status": state or "not_submitted"}
    if state == RC_PENDING:
        raise APIError(
            "This vehicle's registration certificate is still being verified.",
            403, code="rc_pending_review", details=details)
    if state == RC_REJECTED:
        raise APIError(
            "This vehicle's registration certificate was rejected. Upload a "
            "clearer document and resubmit.", 403, code="rc_rejected",
            details={**details, "reason": (vehicle.get("rc_document") or {}).get("reason")})
    raise APIError(
        "Upload this vehicle's registration certificate before publishing rides.",
        403, code="rc_required", details=details)


def rc_summary(vehicle):
    """Owner-facing view. No storage key, no bytes."""
    doc = (vehicle or {}).get("rc_document") or {}
    state = rc_state(vehicle)
    return {
        "rc_status": state,
        "has_document": bool(doc.get("key")),
        "document": public_metadata(doc),
        "rc_number_masked": doc.get("rc_number_masked"),
        "can_submit": state in (None, RC_REJECTED),
        "in_review": state == RC_PENDING,
        "reason": doc.get("reason"),
        "submitted_at": iso_utc(doc.get("submitted_at")),
        "reviewed_at": iso_utc(doc.get("reviewed_at")),
    }


def admin_rc_view(vehicle):
    doc = (vehicle or {}).get("rc_document") or {}
    return {
        "vehicle_id": str(vehicle["_id"]),
        "vehicle_number": vehicle.get("vehicle_number"),
        "user_id": str(vehicle.get("user_id")),
        "rc_status": rc_state(vehicle),
        "rc_number_masked": doc.get("rc_number_masked"),
        "document": public_metadata(doc),
        "reviewer_id": str(doc["reviewed_by"]) if doc.get("reviewed_by") else None,
        "reviewed_at": iso_utc(doc.get("reviewed_at")),
        "submitted_at": iso_utc(doc.get("submitted_at")),
        "reason": doc.get("reason"),
    }


def review_queue(limit=50, skip=0):
    """Pending RCs across all vehicles, newest first."""
    db = get_db()
    rows = list(db.vehicles.find({"rc_document.verification_status": RC_PENDING})
                .sort("rc_document.submitted_at", 1)
                .skip(max(skip, 0)).limit(min(max(limit, 1), 200)))
    return [admin_rc_view(v) for v in rows]
