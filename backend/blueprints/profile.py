"""Profile endpoints: avatar upload/remove and dashboard stats.

Stats financials come from the ledger (immutable money records), not from
mutable ride.earnings counters.
"""

import io

from PIL import Image
from flask import Blueprint, current_app, g, request

from ..db import get_db, utcnow
from ..errors import APIError
from .. import storage
from ..ledger import commission_for_payment, driver_payable
from ..payouts import reserved_payable, settled_payable
from ..ratelimit import rate_limit
from ..security import require_auth
from ..serializers import private_user
from ..timeutil import iso_utc

bp = Blueprint("profile", __name__, url_prefix="/api/profile")

ALLOWED_AVATAR = {"png", "jpg", "jpeg", "webp"}
_COMPLETED = ("completed", "in_progress", "boarding", "driver_en_route")


@bp.post("/avatar")
@require_auth
@rate_limit("default")
def upload_avatar():
    db = get_db()
    file = request.files.get("avatar") or request.files.get("file")
    if not file or not file.filename:
        raise APIError("No image provided.", 422, code="no_file")

    data = file.read()
    if not data:
        raise APIError("No image provided.", 422, code="no_file")
    limit = int(getattr(current_app.config, "MAX_CONTENT_LENGTH", 0) or 0)
    if limit and len(data) > limit:
        raise APIError("Image is too large.", 413, code="file_too_large")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - PIL rejects non-images
        raise APIError("Upload a valid image (PNG, JPG or WebP).", 422, code="invalid_image")

    fmt = (img.format or "JPEG").lower()
    ext = {"jpeg": "jpg"}.get(fmt, fmt)
    if ext not in ALLOWED_AVATAR:
        raise APIError("Avatar must be PNG, JPG or WebP.", 422, code="invalid_image")

    img = img.convert("RGB")
    img.thumbnail((512, 512))
    buf = io.BytesIO()
    img.save(buf, "JPEG" if ext == "jpg" else ext.upper(), quality=88)

    key = storage.avatar_key(g.user["_id"], ext)
    storage.save_to_key(key, buf.getvalue(), "image/jpeg" if ext == "jpg" else f"image/{ext}")

    old_key = (g.user.get("photo_url") or "").replace("/uploads/", "")
    db.users.update_one({"_id": g.user["_id"]},
                        {"$set": {"photo_url": storage.public_avatar_url(key),
                                  "updated_at": utcnow()}})
    if old_key and old_key != key and storage.is_public_avatar(old_key):
        storage.delete_key(old_key)
    user = db.users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "user": private_user(user)}


@bp.delete("/avatar")
@require_auth
@rate_limit("default")
def remove_avatar():
    db = get_db()
    old_key = (g.user.get("photo_url") or "").replace("/uploads/", "")
    if old_key and storage.is_public_avatar(old_key):
        storage.delete_key(old_key)
    db.users.update_one({"_id": g.user["_id"]},
                        {"$set": {"photo_url": "", "updated_at": utcnow()}})
    user = db.users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "user": private_user(user)}


@bp.get("/earnings")
@require_auth
@rate_limit("default")
def earnings():
    """Driver earnings statement, derived entirely from the immutable ledger.

    Every figure is a sum over ledger rows, so the statement can always be
    re-derived from the books and can never drift from what was actually
    collected. The three totals answer three different questions:

        earned     - gross payables from completed trips
        settled    - money the provider confirmed it has sent
        outstanding- earned and not yet settled (this is what a payout uses)

    `line_items` is the per-ride breakdown, including the commission that was
    frozen at payment time, so a driver can see exactly what was deducted.
    """
    db = get_db()
    uid = g.user["_id"]

    def _sum(entry_type):
        rows = db.ledger_entries.find({"account_id": str(uid),
                                       "account_type": "driver",
                                       "entry_type": entry_type})
        return round(sum(float(r.get("amount", 0.0) or 0.0) for r in rows), 2)

    payable = _sum("DRIVER_PAYABLE")
    reversal = _sum("DRIVER_PAYABLE_REVERSAL")   # negative
    payout_debits = _sum("DRIVER_PAYOUT")        # negative
    void_reversals = _sum("DRIVER_PAYOUT_REVERSAL")  # positive
    earned = round(payable + reversal, 2)
    settled = settled_payable(uid)
    reserved = reserved_payable(uid)
    # Outstanding is what is still free to be paid out. It subtracts every live
    # reservation, not just settled ones, so a payout that is pending, in
    # flight, or failed-but-not-yet-voided does not show up as money the driver
    # can be paid twice.
    outstanding = round(earned - reserved, 2)

    # Commission is debited to the PLATFORM account, so the driver's share of it
    # is read from the split frozen on each payment rather than from a ledger
    # query scoped to this driver.
    gross_collected = 0.0
    platform_fees = 0.0
    for p in db.payments.find({"driver_id": uid, "status": {"$in": ["success", "refunded"]}}):
        split = commission_for_payment(p)
        gross_collected += split["gross"]
        platform_fees += split["platform_fee"]

    line_items = []
    seen = set()
    for row in db.ledger_entries.find({"account_id": str(uid), "account_type": "driver",
                                       "entry_type": "DRIVER_PAYABLE"}).sort("created_at", -1):
        bid = row.get("booking_id")
        if not bid or bid in seen or not _is_oid(bid):
            continue
        seen.add(bid)
        from bson import ObjectId

        booking = db.bookings.find_one({"_id": ObjectId(bid)})
        payment = db.payments.find_one({"booking_id": booking["_id"]}) if booking else None
        split = commission_for_payment(payment) if payment else None
        ride = db.rides.find_one({"_id": booking["ride_id"]}) if booking else None
        line_items.append({
            "booking_id": bid,
            "ride_id": str(ride["_id"]) if ride else None,
            "route": (f"{ride.get('origin', {}).get('label')} → "
                      f"{ride.get('destination', {}).get('label')}") if ride else None,
            "completed_at": (iso_utc(booking.get("completed_at"))
                             if booking and booking.get("completed_at") else None),
            "booking_status": booking.get("status") if booking else None,
            "gross": split["gross"] if split else round(float(row.get("amount", 0.0)), 2),
            "platform_fee": split["platform_fee"] if split else None,
            "net": round(float(row.get("amount", 0.0)), 2),
            "commission_rate_percent": split["commission_rate_percent"] if split else None,
        })

    return {
        "ok": True,
        "earnings": {
            "currency": "INR",
            "gross_collected": round(gross_collected, 2),
            "platform_fees": round(platform_fees, 2),
            "earned": earned,
            "reversed": round(abs(reversal), 2),
            "payouts_reserved": round(abs(payout_debits) - void_reversals, 2),
            "settled": settled,
            "outstanding": max(outstanding, 0.0),
            "line_items": line_items[:100],
            "line_items_total": len(line_items),
        },
    }


def _is_oid(value):
    from bson import ObjectId

    try:
        ObjectId(str(value))
        return True
    except Exception:  # noqa: BLE001
        return False


@bp.get("/stats")
@require_auth
@rate_limit("default")
def stats():
    db = get_db()
    now = utcnow()
    uid = g.user["_id"]

    rides_given = db.rides.count_documents(
        {"owner_id": uid, "status": {"$in": list(_COMPLETED)}})
    given_completed = db.rides.count_documents(
        {"owner_id": uid, "status": "completed"})

    confirmed_ids = [
        b["ride_id"] for b in db.bookings.find(
            {"rider_id": uid, "status": "confirmed"}, {"ride_id": 1})
    ]
    rides_taken = len(confirmed_ids)

    taken_completed = 0
    co2_kg = 0.0
    for b in db.bookings.find({"rider_id": uid, "status": "confirmed"},
                              {"ride_id": 1, "seats": 1}):
        ride = db.rides.find_one({"_id": b["ride_id"]}, {"departure_at": 1, "distance_km": 1})
        if ride and ride.get("departure_at", now) < now:
            taken_completed += 1
            km = ride.get("distance_km") or 20
            co2_kg += b["seats"] * km * 0.115

    return {
        "ok": True,
        "stats": {
            "rides_given": rides_given,
            "given_completed": given_completed,
            "rides_taken": rides_taken,
            "taken_completed": taken_completed,
            "total_earnings": round(driver_payable(uid), 2),
            "co2_saved_kg": round(co2_kg, 1),
            "rating": g.user.get("rating", 0) or 0,
        },
    }