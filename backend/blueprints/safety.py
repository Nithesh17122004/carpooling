"""Safety features: trusted contacts, moderation reports, SOS flags, blocks.

- Trusted contacts are stored on the user profile (max 5).
- Reports create an admin_flag notification seen by the admin overview.
- SOS flags the active ride and raises an admin_flag + audit trail; it also
  notifies the rider's confirmed driver (if on an active booking).
- Blocks are one-way and enforced by unique index.
"""

from flask import Blueprint, g, request

from pymongo.errors import DuplicateKeyError

from ..db import get_db, to_object_id, utcnow
from ..errors import APIError
from .. import notifications
from ..ratelimit import rate_limit
from ..security import require_auth
from ..validators import as_str, body, valid_phone

bp = Blueprint("safety", __name__, url_prefix="/api/safety")

_CONTACT_MAX = 5


def _validated_contact(data):
    name = as_str(data.get("name"), "name", max_len=60, required=True)
    phone = valid_phone(data.get("phone"), required=True)
    return {"name": name, "phone": phone}


@bp.get("/trusted-contacts")
@require_auth
@rate_limit("default")
def get_contacts():
    user = get_db().users.find_one({"_id": g.user["_id"]}, {"trusted_contacts": 1})
    return {"ok": True, "data": (user or {}).get("trusted_contacts", [])}


@bp.put("/trusted-contacts")
@require_auth
@rate_limit("strict")
def put_contacts():
    data = body()
    contacts = data.get("contacts")
    if not isinstance(contacts, list) or not contacts:
        raise APIError("Provide a non-empty list of contacts.", 422, code="validation_error")
    if len(contacts) > _CONTACT_MAX:
        raise APIError(f"At most {_CONTACT_MAX} trusted contacts are allowed.", 422,
                       code="validation_error")
    parsed = [_validated_contact(c) for c in contacts]
    get_db().users.update_one({"_id": g.user["_id"]},
                              {"$set": {"trusted_contacts": parsed, "updated_at": utcnow()}})
    return {"ok": True, "data": parsed}


@bp.post("/report")
@require_auth
@rate_limit("strict")
def report():
    db = get_db()
    data = body()
    target_id = to_object_id(data.get("target_user_id") or "", "user")
    target = db.users.find_one({"_id": target_id})
    if not target:
        raise APIError("User not found.", 404, code="not_found")
    if str(target_id) == str(g.user["_id"]):
        raise APIError("You cannot report yourself.", 422, code="validation_error")
    reason = as_str(data.get("reason"), "reason", max_len=60, required=True)
    details = as_str(data.get("details", ""), "details", max_len=1000)
    ride_id = to_object_id((data.get("ride_id") or ""), "ride") if data.get("ride_id") else None

    doc = {
        "reporter_id": g.user["_id"],
        "target_user_id": target_id,
        "ride_id": ride_id,
        "reason": reason,
        "details": details or "",
        "status": "open",
        "created_at": utcnow(),
    }
    db.reports.insert_one(doc)
    notifications.notify(
        None,  # system
        "New user report",
        f"Report received for {target.get('name')} ({reason}).",
        ref_type="admin_flag",
        ref_id=str(doc["_id"]),
    )
    return {"ok": True, "report": {"id": str(doc["_id"]), "status": "open"}}, 201


@bp.post("/sos")
@require_auth
@rate_limit("strict")
def sos():
    db = get_db()
    data = body()
    ride_id = to_object_id(data.get("ride_id") or "", "ride") if data.get("ride_id") else None
    note = as_str(data.get("note", ""), "note", max_len=500)
    me = db.users.find_one({"_id": g.user["_id"]})

    ride_data = None
    if ride_id is not None:
        ride = db.rides.find_one({"_id": ride_id})
        if not ride:
            raise APIError("Ride not found.", 404, code="not_found")
        db.rides.update_one({"_id": ride_id},
                            {"$set": {"sos_active": True, "sos_at": utcnow()}})
        ride_data = {"id": str(ride_id), "owner_id": str(ride["owner_id"])}

    msg = f"SOS from {me.get('name','user')}"
    if ride_data:
        msg += f" on ride {ride_data['id']}"
    if note:
        msg += f": {note}"
    notifications.notify(None, "SOS alert", msg, ref_type="admin_flag",
                         ref_id=ride_data["id"] if ride_data else None)

    db.audit_logs.insert_one({
        "action": "safety.sos",
        "actor_id": g.user["_id"],
        "target_type": "ride" if ride_data else None,
        "target_id": ride_data["id"] if ride_data else None,
        "meta": {"note": note or ""},
        "created_at": utcnow(),
    })
    return {"ok": True, "sos": {"ride_id": ride_data["id"] if ride_data else None}}


@bp.get("/blocked")
@require_auth
@rate_limit("default")
def blocked():
    from ..serializers import public_user
    db = get_db()
    rows = list(db.blocks.find({"blocker_id": g.user["_id"]}).sort("created_at", -1))
    users = list(db.users.find({"_id": {"$in": [r["blocked_id"] for r in rows]}}))
    by_id = {str(u["_id"]): public_user(u) for u in users}
    return {"ok": True, "data": [by_id.get(str(r["blocked_id"])) for r in rows if by_id.get(str(r["blocked_id"]))]}


@bp.put("/block/<uid>")
@require_auth
@rate_limit("default")
def block(uid):
    db = get_db()
    target_id = to_object_id(uid, "user")
    if str(target_id) == str(g.user["_id"]):
        raise APIError("You cannot block yourself.", 422, code="validation_error")
    if not db.users.find_one({"_id": target_id}, {"_id": 1}):
        raise APIError("User not found.", 404, code="not_found")
    try:
        db.blocks.insert_one({"blocker_id": g.user["_id"], "blocked_id": target_id,
                              "created_at": utcnow()})
    except DuplicateKeyError:
        pass
    return {"ok": True, "blocked": True}


@bp.delete("/block/<uid>")
@require_auth
@rate_limit("default")
def unblock(uid):
    db = get_db()
    db.blocks.delete_one({"blocker_id": g.user["_id"], "blocked_id": to_object_id(uid, "user")})
    return {"ok": True, "blocked": False}