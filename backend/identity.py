"""Identity KYC for drivers and passengers.

One state machine, two roles. Driver and passenger identity are the same
problem -- collect an approved identity document, let a human look at it, record
the decision -- so they share the machine and the storage model, and differ only
in which gate consumes the result (a driver must be approved to publish; a
passenger must be approved to book, and only when configured).

    unverified --submit--> submitted --approve--> verified
          ^                   |                     |
          |            reject |                     | (new document)
          |                   v                     |
          +--- resubmit --- rejected <---------------+

The state *strings* are the same four the existing vehicle KYC in
`backend/kyc.py` already uses, deliberately: one vocabulary across the whole
service means a query, a report or a support script written for vehicles also
works for people, and there is no window where a status is called
`"not_started"` on one path and `"unverified"` on another.

Privacy rules, all enforced here rather than in the views:

* **The document number is never stored in full.** Only the last four digits
  (so a reviewer and the owner can recognise it) and a keyed HMAC (so the same
  identity can be detected across accounts without the number being readable).
* **Reviewer notes are stored, and the reason shown to the owner is a safe
  projection.** A rejection with no actionable reason is a dead end, so one is
  required -- but it is also the field most likely to contain a copied identity
  number, so `safe_reason` is what the API returns, not the raw note.
* **Verification is a human decision.** There is deliberately no endpoint that
  lets an owner set their own status to `verified`; only `review_identity()`
  can, and it records the reviewer.
* **Re-uploading resets approval.** Otherwise a user could be approved once and
  then swap in a different document, which defeats the entire control.
"""

import hashlib
import hmac
import re
import secrets

from flask import current_app
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .db import get_db, utcnow
from .documents import (
    store_private_document,
    delete_private_document,
    public_metadata,
)
from .errors import APIError
from .timeutil import iso_utc

# The same vocabulary as kyc.py, on a different field (users vs vehicles).
KYC_UNVERIFIED = "unverified"
KYC_SUBMITTED = "submitted"
KYC_VERIFIED = "verified"
KYC_REJECTED = "rejected"

KYC_STATES = {KYC_UNVERIFIED, KYC_SUBMITTED, KYC_VERIFIED, KYC_REJECTED}

_KYC_TRANSITIONS = {
    KYC_UNVERIFIED: {KYC_SUBMITTED},
    KYC_REJECTED: {KYC_SUBMITTED, KYC_UNVERIFIED},
    KYC_SUBMITTED: {KYC_VERIFIED, KYC_REJECTED, KYC_UNVERIFIED},
    KYC_VERIFIED: {KYC_UNVERIFIED},     # a new document re-opens verification
}

ROLE_DRIVER = "driver"
ROLE_PASSENGER = "passenger"
_ROLES = (ROLE_DRIVER, ROLE_PASSENGER)

DOC_TYPES = ("aadhaar", "passport", "voter_id", "dl")
_DOC_NUMBER_RE = re.compile(r"^[A-Za-z0-9\- ]{4,32}$")
_MAX_REASON = 300


def allowed_doc_types():
    configured = current_app.config.get("KYC_IDENTITY_DOC_TYPES") or DOC_TYPES
    return tuple(d for d in DOC_TYPES if d in set(configured))


def kyc_state(user):
    return (user or {}).get("kyc_status") or KYC_UNVERIFIED


def _filter_in_states(user_id, states):
    """A Mongo filter matching users whose *effective* state is in `states`.

    It has to treat a missing `kyc_status` as `unverified`, exactly as
    kyc_state() does. Filtering on the raw field instead would mean a user who
    has never submitted can never submit: the field is absent, so the filter
    matches nothing and every first submission 409s.
    """
    states = set(states)
    clauses = [{"kyc_status": {"$in": sorted(states)}}]
    if KYC_UNVERIFIED in states:
        clauses.append({"kyc_status": {"$exists": False}})
        clauses.append({"kyc_status": None})
    return {"_id": user_id, "$or": clauses}


def can_transition_kyc(source, target):
    return target in _KYC_TRANSITIONS.get(source, set())


def is_verified(user):
    return kyc_state(user) == KYC_VERIFIED


# ------------------------------------------------------------------- redaction
def _mask(value):
    """Last four characters only. A full identity number never reaches the DB."""
    digits = re.sub(r"[^A-Za-z0-9]", "", str(value or ""))
    if not digits:
        return ""
    return ("*" * max(len(digits) - 4, 0)) + digits[-4:]


def _fingerprint(value, role):
    """Keyed HMAC of the identity number.

    Lets the service recognise a number that has already been submitted (a
    second account for the same person) without ever being able to recover the
    number itself. The key is the server secret, so this is not a plain hash
    that could be brute-forced from the database.
    """
    secret = current_app.config.get("JWT_SECRET") or current_app.config.get("SECRET_KEY") or ""
    normalised = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    return hmac.new(secret.encode("utf-8"), f"{role}:{normalised}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def safe_reason(raw):
    """A rejection reason that is safe to show the person it rejects.

    Reviewers routinely quote the document number back ("Aadhaar 1234 does not
    match"). The owner already knows their own number, so this strips anything
    that looks like one before it crosses the API boundary.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # Long digit runs and XXXX-XXXX patterns are identity numbers, not prose.
    text = re.sub(r"\b[0-9][0-9 \-]{7,}[0-9]\b", "[redacted]", text)
    text = re.sub(r"\b[A-Za-z]{4}\s?-\s?[A-Za-z]{4}\s?-?\s?[0-9]{4}\b", "[redacted]", text)
    return text[:_MAX_REASON]


# -------------------------------------------------------------------- intake
def submit_identity(user_id, *, doc_type, doc_number, data, declared_filename=None,
                    full_name=None, date_of_birth=None):
    """Attach an identity document and move the user into review.

    Creates the document first and only then flips the state, so a failed upload
    can never leave a user stuck in `pending` with nothing to review.
    """
    if doc_type not in allowed_doc_types():
        raise APIError(
            "Unsupported identity document type.", 422, code="validation_error",
            details={"allowed": list(allowed_doc_types())})

    db = get_db()
    user = db.users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")

    state = kyc_state(user)
    if state == KYC_SUBMITTED:
        raise APIError("Your identity is already under review.", 409, code="kyc_in_review")

    number = (doc_number or "").strip()
    if not number or not _DOC_NUMBER_RE.match(number):
        raise APIError("Enter a valid document number.", 422, code="validation_error",
                       details={"fields": ["doc_number"]})

    role = user.get("role") if user.get("role") in _ROLES else ROLE_PASSENGER

    # The same person must not open a second account with the same document.
    # Checked here for a usable message, and enforced by a unique index in db.py
    # for the race where two accounts submit the number at the same instant.
    fingerprint = _fingerprint(number, role)
    holder = db.users.find_one({"kyc_fingerprint": fingerprint,
                                "_id": {"$ne": user_id}})
    if holder is not None:
        raise APIError(
            "This document number is already linked to another account.", 409,
            code="kyc_duplicate_identity",
            details={"kyc_state": kyc_state(holder)})

    doc_id = secrets.token_hex(12)
    meta = store_private_document(
        user_id, "identity", doc_id, data, declared_filename,
        extra={
            "doc_type": doc_type,
            "doc_number_masked": _mask(number),
        },
    )

    now = utcnow()
    fields = {
        "kyc_status": KYC_SUBMITTED,
        "kyc_role": role,
        "kyc_document": meta,
        "kyc_doc_type": doc_type,
        "kyc_doc_number_masked": _mask(number),
        "kyc_fingerprint": fingerprint,
        "kyc_submitted_at": now,
        "kyc_updated_at": now,
        "kyc_reviewed_at": None,
        "kyc_reviewed_by": None,
        "kyc_reason": None,
        "kyc_rejection_code": None,
    }
    if full_name:
        fields["kyc_legal_name"] = str(full_name).strip()[:120]
    if date_of_birth:
        fields["kyc_date_of_birth"] = str(date_of_birth).strip()[:10]

    # A re-submission replaces the previous document: the old blob is deleted so
    # a rejected upload does not linger in storage forever.
    previous = (user.get("kyc_document") or {}).get("key")

    try:
        result = db.users.find_one_and_update(
            _filter_in_states(user_id, [KYC_UNVERIFIED, KYC_REJECTED, KYC_VERIFIED]),
            {"$set": fields},
        )
    except DuplicateKeyError:
        # The unique index is the real guard: the read above cannot see a
        # concurrent submission that has not committed yet. Losing that race is
        # a duplicate identity, not a transient conflict, so it gets the same
        # error the read path returns rather than a misleading "try again".
        delete_private_document(meta.get("key"))
        raise APIError(
            "This document number is already linked to another account.", 409,
            code="kyc_duplicate_identity") from None
    if result is None:
        # Lost a race against a concurrent submit; do not leave an orphan blob.
        delete_private_document(meta.get("key"))
        raise APIError("Your identity submission changed concurrently. Please retry.", 409,
                       code="kyc_state_conflict")

    if previous and previous != meta.get("key"):
        delete_private_document(previous)

    from . import audit

    audit.record("identity.kyc.submit", domain=audit.IDENTITY, actor_id=user_id,
                 actor_role=role, target_type="user", target_id=user_id,
                 meta={"doc_type": doc_type,
                       "doc_number_masked": fields["kyc_doc_number_masked"]})
    return db.users.find_one({"_id": user_id})


def review_identity(user_id, decision, reviewer_id, reason=None, rejection_code=None):
    """Admin decision. The only path to `approved`."""
    db = get_db()
    user = db.users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")

    decision = (decision or "").strip().lower()
    if decision in ("approve", "approved", "verify", "verified"):
        target = KYC_VERIFIED
    elif decision in ("reject", "rejected"):
        target = KYC_REJECTED
    elif decision in ("reset", "reset_to_unverified"):
        target = KYC_UNVERIFIED
    else:
        raise APIError("decision must be 'approve' or 'reject'.", 422,
                       code="validation_error", details={"fields": ["decision"]})

    source = kyc_state(user)
    if not can_transition_kyc(source, target):
        raise APIError(
            "An identity in '%s' cannot move to '%s'." % (source, target), 409,
            code="kyc_invalid_transition", details={"from": source, "to": target})

    if target == KYC_REJECTED and not (reason or "").strip():
        raise APIError("A reason is required when rejecting an identity.", 422,
                       code="kyc_reason_required")

    now = utcnow()
    fields = {
        "kyc_status": target,
        "kyc_updated_at": now,
        "kyc_reviewed_at": now if target != KYC_UNVERIFIED else None,
        "kyc_reviewed_by": reviewer_id if target != KYC_UNVERIFIED else None,
        # The raw reviewer note is retained for staff; the API only ever
        # surfaces safe_reason() of it.
        "kyc_reason": (str(reason).strip()[:_MAX_REASON] or None) if target == KYC_REJECTED else None,
        "kyc_rejection_code": (str(rejection_code).strip()[:40] or None) if target == KYC_REJECTED else None,
    }

    from . import audit

    if target == KYC_UNVERIFIED:
        # Releasing a document also deletes the blob: a reset must not leave a
        # readable copy of somebody's identity paper behind.
        previous = (user.get("kyc_document") or {}).get("key")
        fields["kyc_document"] = None
        updated = db.users.find_one_and_update(
            _filter_in_states(user_id, [source]), {"$set": fields},
            return_document=ReturnDocument.AFTER)
        if updated is None:
            raise APIError(
                "This identity changed while it was being reviewed. Please retry.", 409,
                code="kyc_state_conflict")
        audit.record("identity.kyc.review", domain=audit.IDENTITY, actor_id=reviewer_id,
                     target_type="user", target_id=user_id,
                     meta={"to": target, "from": source}, reason=reason)
        delete_private_document(previous)
        return updated

    # Conditional on the state we validated against, so two reviewers acting at
    # once cannot both write: the loser gets 409 instead of silently overwriting.
    updated = db.users.find_one_and_update(
        _filter_in_states(user_id, [source]), {"$set": fields},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise APIError(
            "This identity changed while it was being reviewed. Please retry.", 409,
            code="kyc_state_conflict")
    audit.record("identity.kyc.review", domain=audit.IDENTITY, actor_id=reviewer_id,
                 target_type="user", target_id=user_id,
                 meta={"to": target, "from": source}, reason=reason)
    return updated


def clear_identity_document(user_id):
    """Owner-initiated withdrawal: drops the document and returns to unverified."""
    db = get_db()
    user = db.users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")
    if kyc_state(user) == KYC_SUBMITTED:
        raise APIError("Your identity is under review and cannot be withdrawn.", 409,
                       code="kyc_in_review")
    previous = (user.get("kyc_document") or {}).get("key")
    db.users.update_one({"_id": user_id}, {"$set": {
        "kyc_status": KYC_UNVERIFIED,
        "kyc_document": None,
        "kyc_submitted_at": None,
        "kyc_reviewed_at": None,
        "kyc_reviewed_by": None,
        "kyc_reason": None,
        "kyc_rejection_code": None,
        "kyc_updated_at": utcnow(),
    }})
    delete_private_document(previous)
    return db.users.find_one({"_id": user_id})


# --------------------------------------------------------------------- gates
def booking_requires_verified_passenger():
    """Whether an unverified passenger may create a booking."""
    return bool(current_app.config.get("PASSENGER_KYC_ENFORCE_BOOKING", False))


def assert_can_book(user):
    if not booking_requires_verified_passenger():
        return
    if is_verified(user):
        return
    state = kyc_state(user)
    if state == KYC_SUBMITTED:
        raise APIError(
            "Your identity check is still under review. You can book once it "
            "is verified.", 403, code="kyc_pending_review",
            details={"kyc_status": state})
    raise APIError(
        "Verify your identity before booking a ride.", 403, code="kyc_required",
        details={"kyc_status": state})


def publish_requires_verified_driver():
    return bool(current_app.config.get("DRIVER_KYC_ENFORCE_PUBLISH", True))


def assert_can_publish(user):
    """Publishing requires an approved driver identity, independently of the
    vehicle check. Both gates must pass; satisfying one is not enough."""
    if not publish_requires_verified_driver():
        return
    if is_verified(user):
        return
    state = kyc_state(user)
    if state == KYC_SUBMITTED:
        raise APIError(
            "Your identity check is under review. You can publish once it is "
            "verified.", 403, code="kyc_pending_review",
            details={"kyc_status": state})
    if state == KYC_REJECTED:
        raise APIError(
            "Your identity verification was rejected. Upload a clearer document "
            "and resubmit.", 403, code="kyc_rejected",
            details={"kyc_status": state, "reason": safe_reason(
                (user or {}).get("kyc_reason"))})
    raise APIError(
        "Verify your identity before publishing rides.", 403, code="kyc_required",
        details={"kyc_status": state})


# --------------------------------------------------------------------- views
def kyc_summary(user):
    """The owner's own view of their identity status.

    Contains no document bytes and no storage locator -- only whether a document
    is on file and what it looks like.
    """
    user = user or {}
    state = kyc_state(user)
    return {
        "kyc_status": state,
        "doc_type": user.get("kyc_doc_type"),
        "doc_number_masked": user.get("kyc_doc_number_masked"),
        "has_document": bool((user.get("kyc_document") or {}).get("key")),
        "document": public_metadata(user.get("kyc_document")),
        "can_submit": state in (KYC_UNVERIFIED, KYC_REJECTED),
        "reason": safe_reason(user.get("kyc_reason")),
        "rejection_code": user.get("kyc_rejection_code"),
        "submitted_at": iso_utc(user.get("kyc_submitted_at")),
        "reviewed_at": iso_utc(user.get("kyc_reviewed_at")),
        "updated_at": iso_utc(user.get("kyc_updated_at")),
    }


def admin_kyc_view(user):
    """Reviewer view. Adds the reviewer identity and the raw note, because staff
    adjudicating a document are exactly the people who need it -- and the only
    route to it, since this is never returned by an owner-facing endpoint."""
    user = user or {}
    return {
        "user_id": str(user["_id"]),
        "name": user.get("name"),
        "email": user.get("email"),
        "role": user.get("role"),
        "kyc_status": kyc_state(user),
        "doc_type": user.get("kyc_doc_type"),
        "doc_number_masked": user.get("kyc_doc_number_masked"),
        "legal_name": user.get("kyc_legal_name"),
        "date_of_birth": user.get("kyc_date_of_birth"),
        "document": public_metadata(user.get("kyc_document")),
        "reviewer_id": str(user["kyc_reviewed_by"]) if user.get("kyc_reviewed_by") else None,
        "reviewed_at": iso_utc(user.get("kyc_reviewed_at")),
        "submitted_at": iso_utc(user.get("kyc_submitted_at")),
        "reason": user.get("kyc_reason"),
        "safe_reason": safe_reason(user.get("kyc_reason")),
        "rejection_code": user.get("kyc_rejection_code"),
    }
