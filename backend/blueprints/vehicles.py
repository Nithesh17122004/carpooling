"""Vehicle management endpoints."""

from flask import Blueprint, g
from pymongo.errors import DuplicateKeyError

from ..db import get_db, to_object_id, utcnow
from ..errors import APIError
from ..ratelimit import rate_limit
from ..security import require_auth
from ..validators import (
    VEHICLE_TYPES,
    DL_RE,
    PLATE_RE,
    as_int,
    as_str,
    body,
    require_fields,
    parse_point,
)

bp = Blueprint("vehicles", __name__, url_prefix="/api/vehicles")

MAX_SEATS = {"2-wheeler": 1, "4-wheeler": 7, "auto": 8, "bus": 24}


def _normalize(data, partial=False):
    vehicle_type = None
    if data.get("vehicle_type"):
        vehicle_type = as_str(data.get("vehicle_type"), "vehicle_type", max_len=20)
        if vehicle_type not in VEHICLE_TYPES:
            raise APIError("Invalid vehicle type.", 422, code="validation_error",
                           details={"fields": ["vehicle_type"]})

    vehicle_number = None
    if data.get("vehicle_number"):
        vehicle_number = as_str(data.get("vehicle_number"), "vehicle_number", max_len=20)
        vehicle_number = vehicle_number.upper().replace(" ", "")
        if not PLATE_RE.match(vehicle_number):
            raise APIError("Enter a valid vehicle registration number.", 422,
                           code="validation_error", details={"fields": ["vehicle_number"]})

    fields = {}
    if vehicle_type:
        fields["vehicle_type"] = vehicle_type
    if vehicle_number:
        fields["vehicle_number"] = vehicle_number
    if "vehicle_model" in data:
        fields["vehicle_model"] = as_str(data.get("vehicle_model"), "vehicle_model", max_len=80)
    if "dl_number" in data:
        dl = as_str(data.get("dl_number"), "dl_number", max_len=20)
        if dl and not DL_RE.match(dl.upper().replace("-", "")) and not DL_RE.match(dl):
            raise APIError("Enter a valid driving licence number.", 422, code="validation_error",
                           details={"fields": ["dl_number"]})
        fields["dl_number"] = dl or None
    if "insurance_number" in data:
        ins = as_str(data.get("insurance_number"), "insurance_number", max_len=50)
        fields["insurance_number"] = ins or None
    if "color" in data:
        fields["color"] = as_str(data.get("color"), "color", max_len=30)
    if "notes" in data:
        fields["notes"] = as_str(data.get("notes"), "notes", max_len=300) or ""
    if "home_location" in data and data.get("home_location"):
        fields["home_location"] = parse_point(data.get("home_location"))

    if "seat_count" in data:
        seat_count = as_int(data.get("seat_count"), "seat_count", minimum=1, maximum=24)
        if vehicle_type and seat_count > MAX_SEATS.get(vehicle_type, 24):
            raise APIError(
                f"Seat count exceeds the {vehicle_type} limit.",
                422,
                code="seat_limit",
                details={"fields": ["seat_count"]},
            )
        fields["seat_count"] = seat_count
    elif vehicle_type and not partial:
        fields["seat_count"] = 1 if vehicle_type == "2-wheeler" else 4
    return fields


@bp.get("")
@require_auth
@rate_limit("default")
def list_vehicles():
    vehicles = list(
        get_db().vehicles.find({"user_id": g.user["_id"]}).sort("created_at", -1)
    )
    return {"ok": True, "data": [clean_vehicle(v) for v in vehicles]}


def clean_vehicle(v):
    from ..kyc import kyc_summary

    v = dict(v)
    dl_doc = v.get("dl_document") or {}
    ins_doc = v.get("insurance_document") or {}
    out = {
        "id": str(v["_id"]),
        "vehicle_type": v.get("vehicle_type"),
        "vehicle_number": v.get("vehicle_number"),
        "vehicle_model": v.get("vehicle_model") or "",
        "dl_number": v.get("dl_number"),
        "insurance_number": v.get("insurance_number"),
        "seat_count": v.get("seat_count", 1),
        "color": v.get("color") or "",
        "notes": v.get("notes") or "",
        "home_location": v.get("home_location"),
        "dl_document_status": dl_doc.get("status", "not_uploaded"),
        "dl_document_size": dl_doc.get("size"),
        "insurance_document_status": ins_doc.get("status", "not_uploaded"),
        "insurance_document_size": ins_doc.get("size"),
        "dl_document_url": f"/api/uploads/vehicle-doc/{v['_id']}/dl" if dl_doc.get("key") else "",
        "insurance_document_url": f"/api/uploads/vehicle-doc/{v['_id']}/insurance" if ins_doc.get("key") else "",
        "created_at": v.get("created_at").isoformat() if v.get("created_at") else None,
        "updated_at": v.get("updated_at").isoformat() if v.get("updated_at") else None,
    }
    # Verification is driver-facing: without it the UI cannot explain why a
    # vehicle is refused for publishing, and the owner must never be able to set
    # it themselves.
    out["kyc"] = kyc_summary(v)
    out["verification_status"] = out["kyc"]["verification_status"]
    return out


@bp.get("/<vid>/kyc")
@require_auth
@rate_limit("default")
def kyc_status(vid):
    """What is still required before this vehicle can be verified."""
    from ..kyc import kyc_summary

    db = get_db()
    vehicle = db.vehicles.find_one({"_id": to_object_id(vid, "vehicle"),
                                    "user_id": g.user["_id"]})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")
    return {"ok": True, "kyc": kyc_summary(vehicle)}


@bp.post("/<vid>/submit-verification")
@require_auth
@rate_limit("strict")
def submit_verification(vid):
    """Submit an owned vehicle for review.

    Both the licence and the insurance must be on file first, so the reviewer is
    never handed an incomplete packet.
    """
    from ..kyc import submit_for_verification

    vehicle = submit_for_verification(to_object_id(vid, "vehicle"), g.user["_id"])
    return {"ok": True, "vehicle": clean_vehicle(vehicle)}


@bp.post("")
@require_auth
@rate_limit("strict")
def create_vehicle():
    data = body()
    require_fields(data, "vehicle_type", "vehicle_number")
    fields = _normalize(data)
    fields["user_id"] = g.user["_id"]
    fields["seats_booked_total"] = 0
    # A new vehicle starts UNVERIFIED and there is deliberately no client-settable
    # field for it: verification is a human decision, not something a driver can
    # assert about themselves.
    fields["verification_status"] = "unverified"
    fields["created_at"] = utcnow()
    fields["updated_at"] = utcnow()
    try:
        result = get_db().vehicles.insert_one(fields)
    except DuplicateKeyError:
        raise APIError("A vehicle with this number already exists.", 409, code="duplicate_vehicle")
    vehicle = get_db().vehicles.find_one({"_id": result.inserted_id})
    return {"ok": True, "vehicle": clean_vehicle(vehicle)}, 201


@bp.get("/<vid>")
@require_auth
@rate_limit("default")
def get_vehicle(vid):
    vehicle = get_db().vehicles.find_one(
        {"_id": to_object_id(vid, "vehicle")}, {"user_id": 1, "vehicle_number": 1})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")
    if str(vehicle["user_id"]) != str(g.user["_id"]):
        raise APIError("Vehicle not found.", 404, code="not_found")
    vehicle = get_db().vehicles.find_one({"_id": vehicle["_id"]})
    return {"ok": True, "vehicle": clean_vehicle(vehicle)}


@bp.patch("/<vid>")
@require_auth
@rate_limit("default")
def update_vehicle(vid):
    vehicle_id = to_object_id(vid, "vehicle")
    existing = get_db().vehicles.find_one(
        {"_id": vehicle_id, "user_id": g.user["_id"]})
    if not existing:
        raise APIError("Vehicle not found.", 404, code="not_found")

    data = body()
    fields = _normalize(data, partial=True)

    active_rides = get_db().rides.count_documents(
        {"vehicle_id": vehicle_id, "status": "active"})
    if active_rides and (fields.get("vehicle_type") or "seat_count" in fields):
        raise APIError(
            "Cannot change type or seats while this vehicle has active rides.",
            409,
            code="vehicle_in_use",
        )
    if fields.get("vehicle_number") and fields["vehicle_number"] != existing.get("vehicle_number"):
        if get_db().vehicles.count_documents({
            "vehicle_number": fields["vehicle_number"], "_id": {"$ne": vehicle_id}}):
            raise APIError(
                "A vehicle with this number already exists.", 409, code="duplicate_vehicle")

    fields["updated_at"] = utcnow()
    get_db().vehicles.update_one({"_id": vehicle_id}, {"$set": fields})
    vehicle = get_db().vehicles.find_one({"_id": vehicle_id})
    return {"ok": True, "vehicle": clean_vehicle(vehicle)}


@bp.delete("/<vid>")
@require_auth
@rate_limit("strict")
def delete_vehicle(vid):
    vehicle_id = to_object_id(vid, "vehicle")
    existing = get_db().vehicles.find_one({"_id": vehicle_id, "user_id": g.user["_id"]})
    if not existing:
        raise APIError("Vehicle not found.", 404, code="not_found")
    if get_db().rides.count_documents({"vehicle_id": vehicle_id, "status": "active"}):
        raise APIError(
            "Delete or finish this vehicle's active rides first.",
            409,
            code="vehicle_in_use",
        )
    get_db().vehicles.delete_one({"_id": vehicle_id})
    return {"ok": True, "message": "Vehicle deleted."}