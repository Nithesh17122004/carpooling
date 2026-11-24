"""Admin-attested evidence for the free DigiLocker document workflow.

This module does NOT validate DigiLocker QR payloads or PDF signatures itself.
It records an authorised reviewer's explicit attestation after out-of-band
validation with the official DigiLocker app or a trusted PDF signature tool.
"""
from .errors import APIError
from .db import utcnow


def build_review_evidence(payload, reviewer_id, *, require_vehicle_match=False):
    if not isinstance(payload, dict):
        raise APIError("Verification evidence is required before approval.", 422,
                       code="verification_evidence_required")
    method = str(payload.get("integrity_method") or "").strip().lower()
    if method not in ("digilocker_qr", "pdf_digital_signature"):
        raise APIError("Choose QR validation or PDF digital-signature validation.",
                       422, code="verification_method_required")
    required = ("issued_document_confirmed", "integrity_verified",
                "holder_match", "validity_checked")
    if require_vehicle_match:
        required += ("vehicle_match",)
    missing = [field for field in required if payload.get(field) is not True]
    if missing:
        raise APIError("Complete every verification check before approval.", 422,
                       code="verification_checks_incomplete",
                       details={"missing": list(missing)})
    return {
        "provider": "digilocker",
        "workflow": "manual_free_no_api",
        "integrity_method": method,
        "issued_document_confirmed": True,
        "integrity_verified": True,
        "holder_match": True,
        "validity_checked": True,
        "vehicle_match": bool(payload.get("vehicle_match")) if require_vehicle_match else None,
        "checked_by": reviewer_id,
        "checked_at": utcnow(),
        "automated": False,
    }
