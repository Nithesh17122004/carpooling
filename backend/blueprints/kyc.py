"""Identity KYC, registration certificates, and driver payout onboarding.

Three related but separate review queues, one blueprint, because they share the
same shape: the owner submits a document (or opens a flow), a human decides, and
the decision gates something. What they must never share is a shortcut, so each
one is routed to its own state machine rather than a generic "verified" flag.

* `/api/kyc/*`            identity documents, for drivers and passengers alike
* `/api/vehicles/<id>/rc` registration certificates, for a specific vehicle
* `/api/onboarding/*`     payout account onboarding, for drivers

Two rules hold across all of them:

* **No endpoint lets a user set their own status to approved.** Every route here
  either moves a document *into* review or records a reviewer's decision. The
  user-facing `GET`s return `can_submit`/`can_confirm`-style booleans so the UI
  can render the right next action, but a client cannot act on the approval side.
* **Document bytes never leave through these responses.** Metadata comes from
  `documents.public_metadata`, which drops the storage key; the only way to fetch
  the bytes is `GET .../document`, which re-checks ownership on every read.
"""

from flask import Blueprint, Response, g, request

from .. import identity, onboarding, rc
from ..db import get_db, to_object_id
from ..documents import load_private_document, public_metadata
from ..errors import APIError
from ..ratelimit import rate_limit
from ..security import require_admin, require_auth

bp = Blueprint("kyc", __name__, url_prefix="/api/kyc")


def _upload_bytes(field="file"):
    """Read one uploaded file, or raise a typed error."""
    file = request.files.get(field) or request.files.get("document")
    if file is None or not file.filename:
        raise APIError("No document provided.", 422, code="no_file")
    data = file.read()
    if not data:
        raise APIError("No document provided.", 422, code="no_file")
    return data, file.filename


def _reject_banking_data():
    """Refuse any request carrying banking details or secrets.

    Banking data belongs at the provider. Refusing it here means a future
    frontend change cannot quietly start persisting an account number.
    """
    onboarding.assert_no_banking_data(request.form.to_dict())
    if request.is_json:
        onboarding.assert_no_banking_data(request.get_json(silent=True) or {})


# ================================================================= identity KYC
@bp.get("/me")
@require_auth
def my_kyc():
    """The caller's own identity status. Never any document bytes."""
    return {"ok": True, "kyc": identity.kyc_summary(g.user),
            "allowed_document_types": list(identity.allowed_doc_types())}


@bp.post("/identity")
@require_auth
@rate_limit("strict")
def submit_identity():
    """Upload an identity document and enter review.

    Accepts multipart (file) or JSON-with-base64 is deliberately NOT supported:
    a base64 field in a JSON body is trivially logged by proxies and is the most
    common way identity documents end up leaking into access logs.
    """
    _reject_banking_data()
    db = get_db()
    doc_type = (request.form.get("doc_type") or "").strip().lower()
    doc_number = (request.form.get("doc_number") or "").strip()
    full_name = (request.form.get("full_name") or "").strip()
    dob = (request.form.get("date_of_birth") or "").strip()
    data, filename = _upload_bytes()

    identity.submit_identity(
        g.user["_id"], doc_type=doc_type, doc_number=doc_number, data=data,
        declared_filename=filename, full_name=full_name, date_of_birth=dob)

    from .. import notifications

    notifications.notify(
        g.user["_id"], "Identity submitted",
        "We are reviewing your identity document. You will hear from us once it "
        "has been checked.")
    fresh = db.users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "kyc": identity.kyc_summary(fresh)}, 201


@bp.delete("/identity")
@require_auth
@rate_limit("strict")
def withdraw_identity():
    """Withdraw a submitted document and the stored copy of it."""
    identity.clear_identity_document(g.user["_id"])
    fresh = get_db().users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "kyc": identity.kyc_summary(fresh)}


@bp.get("/identity/document")
@require_auth
def download_identity_document():
    """Download one's own identity document.

    Ownership is re-derived from `g.user` and the AAD binding in
    `documents.load_private_document` means the bytes cannot be decrypted with
    another user's id, so this cannot be repurposed to read someone else's.
    """
    user = get_db().users.find_one({"_id": g.user["_id"]})
    doc = (user or {}).get("kyc_document") or {}
    key, doc_id = doc.get("key"), doc.get("doc_id")
    if not key:
        raise APIError("You have not uploaded an identity document.", 404,
                       code="not_found")
    data, mime = load_private_document(g.user["_id"], "identity", doc_id, key)
    return Response(data, mimetype=mime,
                    headers={"Content-Disposition": "inline",
                             "Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff"})


# =================================================================== RC uploads
@bp.post("/vehicles/<vid>/rc")
@require_auth
@rate_limit("strict")
def submit_rc(vid):
    """Upload a vehicle's registration certificate into the review queue."""
    _reject_banking_data()
    vehicle_id = to_object_id(vid, "vehicle")
    data, filename = _upload_bytes()
    rc.submit_rc(vehicle_id, g.user["_id"], data=data,
                 declared_filename=filename,
                 rc_number=(request.form.get("rc_number") or "").strip())
    fresh = get_db().vehicles.find_one({"_id": vehicle_id})
    return {"ok": True, "rc": rc.rc_summary(fresh)}, 201


@bp.get("/vehicles/<vid>/rc")
@require_auth
def get_rc(vid):
    vehicle_id = to_object_id(vid, "vehicle")
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")
    if vehicle.get("user_id") != g.user["_id"] and not _is_admin():
        raise APIError("You do not have permission to view this document.", 403,
                       code="forbidden")
    return {"ok": True, "rc": rc.rc_summary(vehicle)}


@bp.get("/vehicles/<vid>/rc/document")
@require_auth
def download_rc(vid):
    """Owner or admin only. Same ownership rule as every other private read."""
    vehicle_id = to_object_id(vid, "vehicle")
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")
    if vehicle.get("user_id") != g.user["_id"] and not _is_admin():
        raise APIError("You do not have permission to view this document.", 403,
                       code="forbidden")
    doc = vehicle.get("rc_document") or {}
    if not doc.get("key"):
        raise APIError("This vehicle has no RC uploaded.", 404, code="not_found")
    data, mime = load_private_document(vehicle["user_id"], "rc",
                                       doc["doc_id"], doc["key"])
    return Response(data, mimetype=mime,
                    headers={"Content-Disposition": "inline",
                             "Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff"})


# ======================================================== payout onboarding
@bp.get("/onboarding")
@require_auth
def my_onboarding():
    """The caller's payout onboarding status, for a driver."""
    if g.user.get("role") != "driver":
        raise APIError("Only drivers have a payout account.", 403, code="forbidden")
    return {"ok": True, "onboarding": onboarding.onboarding_summary(g.user)}


@bp.post("/onboarding/begin")
@require_auth
@rate_limit("strict")
def begin_onboarding():
    """Open the payout onboarding flow.

    Takes no banking details. The driver is redirected to the provider, and only
    the provider's opaque ids come back to us.
    """
    if g.user.get("role") != "driver":
        raise APIError("Only drivers have a payout account.", 403, code="forbidden")
    _reject_banking_data()
    last4 = request.form.get("account_last4") or request.form.get("last4")
    onboarding.begin_onboarding(g.user["_id"], account_last4=last4,
                                actor_id=g.user["_id"])
    fresh = get_db().users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "onboarding": onboarding.onboarding_summary(fresh)}, 201


@bp.post("/onboarding/submit")
@require_auth
@rate_limit("strict")
def submit_onboarding():
    """Record that the provider accepted the driver's account details.

    `contact_id` / `linked_account_id` are the provider's own opaque
    identifiers. No account number, IFSC or UPI id is accepted or stored.
    """
    if g.user.get("role") != "driver":
        raise APIError("Only drivers have a payout account.", 403, code="forbidden")
    _reject_banking_data()
    form = request.form.to_dict()
    onboarding.submit_onboarding(
        g.user["_id"],
        contact_id=form.get("contact_id"),
        linked_account_id=form.get("linked_account_id"),
        provider_status=form.get("provider_status"),
        actor_id=g.user["_id"])
    fresh = get_db().users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "onboarding": onboarding.onboarding_summary(fresh)}


@bp.get("/onboarding/status")
@require_auth
def onboarding_status():
    """A cheaper read of just the status, for polling after provider redirect."""
    user = get_db().users.find_one({"_id": g.user["_id"]})
    return {"ok": True,
            "status": onboarding.state_of(user),
            "settlement_allowed": onboarding.is_settled_ready(user),
            "reason": (user or {}).get("payout_onboarding_reason")}


# ================================================================= admin review
@bp.get("/admin/identity/queue")
@require_admin
def identity_queue():
    """Identity documents waiting on a human."""
    db = get_db()
    limit = min(int(request.args.get("limit", 50) or 50), 200)
    skip = max(int(request.args.get("skip", 0) or 0), 0)
    state = (request.args.get("status") or identity.KYC_SUBMITTED).strip()
    rows = db.users.find({"kyc_status": state}).sort("kyc_submitted_at", 1)
    return {"ok": True,
            "queue": [identity.admin_kyc_view(u) for u in rows.skip(skip).limit(limit)]}


@bp.get("/admin/rc/queue")
@require_admin
def rc_queue():
    """Registration certificates waiting on a human."""
    limit = min(int(request.args.get("limit", 50) or 50), 200)
    skip = max(int(request.args.get("skip", 0) or 0), 0)
    return {"ok": True, "queue": rc.review_queue(limit=limit, skip=skip)}


@bp.post("/admin/identity/<uid>/review")
@require_admin
@rate_limit("strict")
def review_identity(uid):
    """Approve or reject an identity. The only route to `verified`."""
    form = request.form.to_dict()
    decision = form.get("decision") or (request.get_json(silent=True) or {}).get("decision")
    reason = form.get("reason") or (request.get_json(silent=True) or {}).get("reason")
    code = (form.get("rejection_code")
            or (request.get_json(silent=True) or {}).get("rejection_code"))
    if not decision:
        raise APIError("decision is required.", 422, code="validation_error",
                       details={"fields": ["decision"]})

    from bson import ObjectId

    try:
        user_id = ObjectId(uid)
    except Exception:  # noqa: BLE001
        raise APIError("User not found.", 404, code="not_found")
    identity.review_identity(user_id, decision, reviewer_id=g.user["_id"],
                             reason=reason, rejection_code=code)
    fresh = get_db().users.find_one({"_id": user_id})
    return {"ok": True, "kyc": identity.admin_kyc_view(fresh)}


@bp.post("/admin/vehicles/<vid>/rc/review")
@require_admin
@rate_limit("strict")
def review_rc(vid):
    """Approve or reject an RC. The only route to `approved`."""
    payload = request.get_json(silent=True) or request.form.to_dict()
    decision = payload.get("decision")
    if not decision:
        raise APIError("decision is required.", 422, code="validation_error",
                       details={"fields": ["decision"]})
    vehicle_id = to_object_id(vid, "vehicle")
    rc.review_rc(vehicle_id, decision, reviewer_id=g.user["_id"],
                 reason=payload.get("reason"))
    fresh = get_db().vehicles.find_one({"_id": vehicle_id})
    return {"ok": True, "rc": rc.admin_rc_view(fresh)}


@bp.get("/admin/onboarding/<uid>")
@require_admin
def admin_onboarding(uid):
    """Full onboarding detail for one driver, for adjudication."""
    from bson import ObjectId

    try:
        user_id = ObjectId(uid)
    except Exception:  # noqa: BLE001
        raise APIError("User not found.", 404, code="not_found")
    user = get_db().users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")
    return {"ok": True, "onboarding": onboarding.admin_onboarding_view(user)}


@bp.post("/admin/onboarding/<uid>/review")
@require_admin
@rate_limit("strict")
def review_onboarding(uid):
    """Verify, reject, suspend or resume a driver's payout account."""
    from bson import ObjectId

    payload = request.get_json(silent=True) or request.form.to_dict()
    action = (payload.get("action") or "").strip().lower()
    reason = payload.get("reason")
    provider_status = payload.get("provider_status")

    try:
        user_id = ObjectId(uid)
    except Exception:  # noqa: BLE001
        raise APIError("User not found.", 404, code="not_found")

    if action in ("verify", "approve", "verified"):
        onboarding.verify_onboarding(user_id, reviewer_id=g.user["_id"],
                                     provider_status=provider_status,
                                     actor_role="admin")
    elif action in ("reject", "rejected"):
        if not (reason or "").strip():
            raise APIError("A reason is required when rejecting.", 422,
                           code="onboarding_reason_required")
        onboarding.reject_onboarding(user_id, reviewer_id=g.user["_id"],
                                     reason=reason, actor_role="admin")
    elif action == "suspend":
        if not (reason or "").strip():
            raise APIError("A reason is required when suspending.", 422,
                           code="onboarding_reason_required")
        onboarding.suspend_onboarding(user_id, actor_id=g.user["_id"],
                                      reason=reason, actor_role="admin")
    elif action == "resume":
        onboarding.resume_onboarding(user_id, actor_id=g.user["_id"],
                                     actor_role="admin")
    else:
        raise APIError("action must be verify, reject, suspend or resume.", 422,
                       code="validation_error", details={"fields": ["action"]})

    fresh = get_db().users.find_one({"_id": user_id})
    return {"ok": True, "onboarding": onboarding.admin_onboarding_view(fresh)}


def _is_admin():
    return (g.user or {}).get("role") == "admin"


# ============================================== trip completion / dispute (API)
# Mounted under /api/bookings by completion_api because the natural resource is
# a booking. Declared here to keep the two small surfaces together in review.
@bp.get("/vehicles/<vid>/documents")
@require_auth
def vehicle_documents(vid):
    """Safe projection of every private document on a vehicle, for its owner.

    Convenience for the settings screen: one call, no keys, no bytes.
    """
    vehicle_id = to_object_id(vid, "vehicle")
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id, "user_id": g.user["_id"]})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")
    return {
        "ok": True,
        "documents": {
            "rc": rc.rc_summary(vehicle),
            "dl": public_metadata(vehicle.get("dl_document")),
            "insurance": public_metadata(vehicle.get("insurance_document")),
        },
    }
