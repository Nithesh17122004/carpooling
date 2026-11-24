"""Authenticated captain endpoint for API Setu MoRTH DL + RC verification."""
from flask import Blueprint, g, request

from .. import apisetu
from ..db import get_db, to_object_id, utcnow
from ..errors import APIError
from ..ratelimit import rate_limit
from ..security import require_auth

bp = Blueprint("apisetu", __name__, url_prefix="/api/vehicles")


@bp.post("/<vid>/government-verification")
@require_auth
@rate_limit("strict")
def verify_government_documents(vid):
    """Verify a captain's DL and RC against MoRTH through API Setu.

    Aadhaar UID and the signed consent artifact are request-only values; neither
    is persisted. Certificate bodies are reduced to match flags and a SHA-256
    digest, never stored in the user/vehicle record.
    """
    vehicle_id = to_object_id(vid, "vehicle")
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id, "user_id": g.user["_id"]})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")

    payload = request.get_json(silent=True) or {}
    consent_artifacts = payload.get("consent_artifacts") or {}
    dl_consent = consent_artifacts.get("dl")
    rc_consent = consent_artifacts.get("rc")
    uid = str(payload.get("uid") or "").strip()
    full_name = str(payload.get("full_name") or g.user.get("name") or "").strip()
    dob = str(payload.get("date_of_birth") or "").strip()
    chassis = str(payload.get("chasis_no") or "").strip()
    dl_number = str(vehicle.get("dl_number") or "").strip()
    registration = str(vehicle.get("vehicle_number") or "").strip()

    missing = [name for name, value in (
        ("uid", uid), ("full_name", full_name), ("date_of_birth", dob),
        ("chasis_no", chassis), ("dl_number", dl_number), ("vehicle_number", registration),
        ("consent_artifacts.dl", dl_consent),
        ("consent_artifacts.rc", rc_consent),
    ) if not value]
    if missing:
        raise APIError("Required verification details are missing.", 422,
                       code="government_verification_fields_missing",
                       details={"fields": missing})
    if not (vehicle.get("rc_document") or {}).get("key"):
        raise APIError("Upload the RC document before starting verification.", 422,
                       code="rc_document_required")

    results = {}
    errors = {}
    calls = (
        ("dl", "drvlc", {
            "dlno": dl_number, "UID": uid, "FullName": full_name, "DOB": dob,
        }),
        ("rc", "rvcer", {
            "reg_no": registration, "chasis_no": chassis,
            "UID": uid, "FullName": full_name,
        }),
    )
    for label, endpoint, params in calls:
        try:
            results[label] = apisetu.verify_certificate(
                endpoint, params, dl_consent if label == "dl" else rc_consent)
        except apisetu.APISetuError as exc:
            errors[label] = {"code": exc.code, "message": str(exc)}

    now = utcnow()
    update = {
        "apisetu_government_verification": {
            "provider": "apisetu_morth",
            "dl": results.get("dl"),
            "rc": results.get("rc"),
            "errors": errors,
            "updated_at": now,
            "requested_by": g.user["_id"],
        },
        "updated_at": now,
    }
    # A positive RC result is enough to approve the RC document because it is
    # matched against the registered vehicle number, chassis and owner. DL is
    # recorded independently: overall vehicle KYC still includes insurance and
    # is not auto-approved by a DL response alone.
    if (results.get("rc") or {}).get("decision") == "matched":
        update["rc_document.verification_status"] = "approved"
        update["rc_document.reviewed_at"] = now
        update["rc_document.reviewed_by"] = "apisetu_morth"
        update["rc_document.reason"] = None
        update["rc_document.apisetu_verification"] = results["rc"]
    db.vehicles.update_one({"_id": vehicle_id, "user_id": g.user["_id"]},
                           {"$set": update})

    # Record an audit event without logging Aadhaar, DOB, DL/RC numbers, consent
    # content, certificate bytes, or provider response bodies.
    from .. import audit
    audit.record(
        "identity.rc.review", domain=audit.IDENTITY, actor_id=g.user["_id"],
        actor_role="driver", target_type="vehicle", target_id=vehicle_id,
        meta={
            "provider": "apisetu_morth",
            "dl_decision": (results.get("dl") or {}).get("decision", "error"),
            "rc_decision": (results.get("rc") or {}).get("decision", "error"),
        },
        reason="API Setu government certificate verification",
    )

    return {
        "ok": True,
        "provider": "apisetu_morth",
        "dl": results.get("dl"),
        "rc": results.get("rc"),
        "errors": errors,
        "vehicle_kyc_note": (
            "DL result is recorded separately. Overall vehicle KYC still requires "
            "the existing insurance verification gate."
        ),
    }, (200 if results else 502)
