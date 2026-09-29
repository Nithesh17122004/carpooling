"""Uploads for private vehicle documents (DL / insurance).

Documents are stored with random keys under docs/. They are NEVER exposed as
public URLs and are never included in ride/search/vehicle payloads. Only the
vehicle owner can download them via /api/vehicles/<id>/document/<kind>.

Upload hardening (Part 15):
  - files are identified by MAGIC BYTES (never by client MIME/extension);
  - oversized uploads are rejected with a typed 413;
  - a declared filename whose extension is unknown/unsafe (`file.jpg.exe`,
    `evil.txt`) or mismatches the detected type is rejected outright;
  - stored keys are server-generated random paths (no client input -> no
    path traversal).
"""

from flask import Blueprint, Response, g, request

from ..db import get_db, to_object_id, utcnow
from ..errors import APIError
from ..ratelimit import rate_limit
from ..security import random_token, require_auth
from .. import storage

bp = Blueprint("uploads", __name__, url_prefix="/api/uploads")

KINDS = {"dl", "insurance"}
CONFIRMATION = {"dl": "driving licence", "insurance": "insurance"}
_ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "pdf"}


def _field_names(kind):
    return {
        "dl": ("dl_document", "licence_verified"),
        "insurance": ("insurance_document", "insurance_verified"),
    }[kind]


def _declared_extension(filename):
    _, _, ext = (filename or "").rpartition(".")
    return (ext or "").lower()


def _read_upload():
    file = request.files.get("file") or request.files.get("document")
    if not file or not file.filename:
        raise APIError("No file provided.", 422, code="no_file")
    data = file.read()
    if not data:
        raise APIError("No file provided.", 422, code="no_file")
    limit = 0
    try:
        from flask import current_app

        limit = int(current_app.config.get("MAX_CONTENT_LENGTH", 0) or 0)
    except Exception:  # pragma: no cover - defensive
        limit = 0
    if limit and len(data) > limit:
        raise APIError("File is too large. Limit is %d MB." % (limit // (1024 * 1024)),
                       413, code="file_too_large")
    return data, (file.filename or "document")


@bp.post("/vehicle-doc")
@require_auth
@rate_limit("strict")
def vehicle_document():
    db = get_db()
    vehicle_id = to_object_id(request.form.get("vehicle_id") or "", "vehicle")
    kind = (request.form.get("doc") or "dl").strip().lower()
    if kind not in KINDS:
        raise APIError("doc must be 'dl' or 'insurance'.", 422, code="validation_error")

    vehicle = db.vehicles.find_one({"_id": vehicle_id, "user_id": g.user["_id"]})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")

    data, original_name = _read_upload()
    declared = _declared_extension(original_name)
    declared_norm = "jpg" if declared in ("jpg", "jpeg") else declared
    if declared and declared_norm not in _ALLOWED_EXT:
        raise APIError("Documents must be PDF or an image (PNG, JPG, WEBP).",
                       422, code="invalid_file")
    ext, content_type = storage.detect_extension(data, original_name)
    if content_type not in ("application/pdf", "image/png", "image/jpeg", "image/webp"):
        raise APIError("Documents must be PDF or an image (PNG, JPG, WEBP).", 422, code="invalid_file")
    if declared and declared_norm != ext and not (declared in ("jpg", "jpeg") and ext in ("jpg", "jpeg")):
        raise APIError("File extension does not match its contents.", 422, code="invalid_file")

    key = storage.document_key(g.user["_id"], kind, ext)
    storage.save_to_key(key, data, content_type)

    doc_field, verified_field = _field_names(kind)
    old = vehicle.get(doc_field) or {}
    res = db.vehicles.update_one(
        {"_id": vehicle_id, "user_id": g.user["_id"]},
        {"$set": {
            doc_field: {
                "key": key,
                "content_type": content_type,
                "size": len(data),
                "original_name": (original_name or "document")[:120],
                "status": "uploaded",
                "uploaded_at": utcnow(),
            },
            verified_field: False,  # a human/verification step must flip this on
            "updated_at": utcnow(),
        }},
    )
    if old.get("key") and old.get("key") != key:
        storage.delete_key(old["key"])
    if not res.modified_count:
        raise APIError("Vehicle not found.", 404, code="not_found")

    # A new document re-opens verification. Without this a driver could get a
    # vehicle approved once and then swap in different papers, which is the
    # obvious way to defeat the control.
    from ..kyc import reset_on_document_change

    reset_on_document_change(vehicle_id, g.user["_id"], doc_field)
    return {"ok": True, "status": "uploaded", "kind": kind,
            "verification_status": "unverified"}


@bp.get("/vehicle-doc/<vid>/<kind>")
@require_auth
@rate_limit("default")
def download_document(vid, kind):
    """Authorized download for the vehicle OWNER only."""
    db = get_db()
    vehicle_id = to_object_id(vid, "vehicle")
    kind = (kind or "").lower()
    if kind not in KINDS:
        raise APIError("Invalid document kind.", 422, code="validation_error")
    vehicle = db.vehicles.find_one(
        {"_id": vehicle_id}, {"user_id": 1, "dl_document": 1, "insurance_document": 1})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")
    if str(vehicle["user_id"]) != str(g.user["_id"]):
        raise APIError("Vehicle not found.", 404, code="not_found")

    doc = vehicle.get(f"{kind}_document") or {}
    if not doc.get("key"):
        raise APIError("Document not uploaded.", 404, code="not_found")
    data, content_type, name = storage.read_key(doc["key"])
    return Response(data, mimetype=content_type, headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "Cache-Control": "private, no-store",
    })