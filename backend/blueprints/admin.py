"""Admin endpoints: RBAC-gated operations and audit trails.

Only users whose role is 'admin' or 'support' (set server-side) may access.
Role values are never read from the client.
"""

from flask import Blueprint, current_app, g, request

from ..db import get_db, to_object_id, utcnow
from ..errors import APIError
from ..ratelimit import rate_limit
from ..security import require_admin
from ..serializers import admin_user
from ..timeutil import iso_utc
from ..validators import as_str, body, require_fields

bp = Blueprint("admin", __name__, url_prefix="/api/admin")


def _audit(action, actor_id, target_type=None, target_id=None, meta=None):
    get_db().audit_logs.insert_one({
        "action": action,
        "actor_id": actor_id,
        "target_type": target_type,
        "target_id": str(target_id) if target_id else None,
        "meta": meta or {},
        "created_at": utcnow(),
    })


def _clean_payout(p, now=None):
    return {
        "id": str(p["_id"]),
        "user_id": str(p.get("user_id")),
        "amount": p.get("amount", 0),
        "currency": p.get("currency", "INR"),
        "status": p.get("status"),
        "provider": p.get("provider"),
        "reference": p.get("reference"),
        "provider_reference": p.get("provider_reference"),
        "confirmed_by": p.get("confirmed_by"),
        "failure_reason": p.get("failure_reason"),
        "void_reason": p.get("void_reason"),
        "booking_ids": p.get("booking_ids") or [],
        "created_at": iso_utc(p.get("created_at")),
        "processing_at": iso_utc(p.get("processing_at")),
        "paid_at": iso_utc(p.get("paid_at")),
        "failed_at": iso_utc(p.get("failed_at")),
        "voided_at": iso_utc(p.get("voided_at")),
    }


@bp.get("/overview")
@require_admin
@rate_limit("default")
def overview():
    db = get_db()
    now = utcnow()
    return {
        "ok": True,
        "overview": {
            "users": db.users.count_documents({}),
            "drivers": db.users.count_documents({"role": "driver"}),
            "vehicles": db.vehicles.count_documents({}),
            "vehicles_pending_verify": db.vehicles.count_documents({"verification_status": {"$ne": "verified"}}),
            "rides": db.rides.count_documents({}),
            "rides_active": db.rides.count_documents({"status": {"$in": ["active", "published", "full"]}}),
            "bookings": db.bookings.count_documents({}),
            "bookings_confirmed": db.bookings.count_documents({"status": "confirmed"}),
            "payments": db.payments.count_documents({}),
            "payments_success": db.payments.count_documents({"status": "success"}),
            "payments_value": round(sum(p.get("amount", 0) for p in
                                       db.payments.find({"status": "success"}, {"amount": 1})), 2),
            "refunds": db.refunds.count_documents({}),
            "refunds_pending": db.refunds.count_documents({"status": "pending"}),
            "refunds_processed": db.refunds.count_documents({"status": "processed"}),
            "refunds_failed": db.refunds.count_documents({"status": "failed"}),
            "bookings_refunded": db.bookings.count_documents({"status": "refunded"}),
            "pending_payouts": db.payouts.count_documents({"status": "pending"}),
            "payouts_paid": db.payouts.count_documents({"status": "paid"}),
            "open_risk_flags": db.notifications.count_documents({"notification_type": "admin_flag", "read": False}),
            "safety_reports": db.reports.count_documents({}),
            "sos_incidents": db.audit_logs.count_documents({"action": "safety.sos"}),
            "user_blocks": db.blocks.count_documents({}),
            "reviews": db.ratings.count_documents({}),
            "avg_rating": round(sum(r.get("rating", 0) or 0 for r in db.ratings.find({}, {"rating": 1})) /
                               max(db.ratings.count_documents({}), 1), 2),
        },
    }


@bp.get("/analytics")
@require_admin
@rate_limit("default")
def analytics():
    """Lightweight product analytics from existing data: activity cohorts,
    ride pipeline, seat fill rate, and payment/payout totals."""
    db = get_db()
    now = utcnow()
    from datetime import timedelta

    def active_since(hours):
        cutoff = now - timedelta(hours=hours)
        return db.users.count_documents({"last_login_at": {"$gte": cutoff}})

    since30 = now - timedelta(days=30)
    seats_agg = list(db.rides.aggregate([
        {"$match": {"created_at": {"$gte": since30}}},
        {"$group": {"_id": None,
                    "booked": {"$sum": {"$subtract": ["$seats_total", "$seats_available"]}},
                    "total": {"$sum": "$seats_total"}}},
    ]))
    agg = seats_agg[0] if seats_agg else {}
    total_seats = agg.get("total", 0) or 0
    fill_rate = round((agg.get("booked", 0) or 0) / total_seats, 3) if total_seats else 0.0

    gmv = next(db.payments.aggregate([
        {"$match": {"status": "success"}},
        {"$group": {"_id": None, "v": {"$sum": "$amount"}}},
    ]), {}).get("v", 0) or 0
    paid_out = next(db.payouts.aggregate([
        {"$match": {"status": {"$in": ["paid", "processing"]}}},
        {"$group": {"_id": None, "v": {"$sum": "$amount"}}},
    ]), {}).get("v", 0) or 0

    return {"ok": True, "analytics": {
        "active": {"dau": active_since(24), "wau": active_since(24 * 7), "mau": active_since(24 * 30)},
        "rides": {
            "total": db.rides.count_documents({}),
            "created_30d": db.rides.count_documents({"created_at": {"$gte": since30}}),
            "completed": db.rides.count_documents({"status": "completed"}),
            "active_now": db.rides.count_documents({"status": {"$in": ["active", "published", "full"]}}),
            "bookings_confirmed": db.bookings.count_documents({"status": "confirmed"}),
            "fill_rate": fill_rate,
        },
        "payments": {
            "success_count": db.payments.count_documents({"status": "success"}),
            "gmv": round(gmv, 2),
        },
        "payouts": {"paid_total": round(paid_out, 2)},
    }}


@bp.get("/users")
@require_admin
@rate_limit("default")
def users():
    db = get_db()
    role = as_str(request.args.get("role"), "role", max_len=20)
    page = max(int(request.args.get("page") or 1), 1)
    limit = min(int(request.args.get("limit") or 25), 100)
    query = {"role": role} if role and role in ("user", "driver", "admin", "support") else {}
    total = db.users.count_documents(query)
    rows = list(db.users.find(query).sort("created_at", -1)
                .skip((page - 1) * limit).limit(limit))
    return {"ok": True, "data": [admin_user(u) for u in rows], "page": page,
            "pages": max((total + limit - 1) // limit, 1), "total": total}


@bp.patch("/users/<uid>/role")
@require_admin
@rate_limit("default")
def set_role(uid):
    db = get_db()
    target = db.users.find_one({"_id": to_object_id(uid, "user")})
    if not target:
        raise APIError("User not found.", 404, code="not_found")
    data = body()
    role = as_str(data.get("role"), "role", max_len=20, required=True)
    if role not in ("user", "driver", "admin", "support"):
        raise APIError("Invalid role.", 422, code="validation_error")
    if role in ("admin", "support") and g.user.get("role") not in ("admin",):
        raise APIError("Only an admin can grant staff roles.", 403, code="forbidden")
    db.users.update_one({"_id": target["_id"]}, {"$set": {"role": role, "updated_at": utcnow()}})
    _audit("user.role_change", g.user["_id"], "user", target["_id"],
           {"from": target.get("role"), "to": role})
    fresh = db.users.find_one({"_id": target["_id"]})
    return {"ok": True, "user": admin_user(fresh)}


@bp.post("/users/<uid>/verify")
@require_admin
@rate_limit("default")
def verify_user(uid):
    """Mark a driver's licence/insurance as verified after manual review."""
    db = get_db()
    data = body()
    kind = as_str(data.get("kind"), "kind", max_len=20, required=True)
    if kind not in ("licence", "insurance"):
        raise APIError("kind must be 'licence' or 'insurance'.", 422, code="validation_error")
    approved = str(data.get("approved")).strip().lower() in ("true", "1", "yes")
    field = "licence_verified" if kind == "licence" else "insurance_verified"
    db.users.update_one({"_id": to_object_id(uid, "user")}, {"$set": {field: approved, "updated_at": utcnow()}})
    me = db.users.find_one({"_id": to_object_id(uid, "user")})
    if approved and me.get("licence_verified") and me.get("insurance_verified"):
        db.users.update_one({"_id": me["_id"]}, {"$set": {"driver_verified": True, "updated_at": utcnow()}})
    _audit(f"user.{kind}_verify", g.user["_id"], "user", me["_id"], {"approved": approved})
    fresh = db.users.find_one({"_id": me["_id"]})
    return {"ok": True, "user": admin_user(fresh)}


@bp.get("/reconcile")
@require_admin
@rate_limit("default")
def reconcile():
    """Financial reconciliation (read-only): cross-checks the immutable ledger
    against payments / refunds / payouts and surfaces open anomalies.

    Total DRIVER_PAYABLE posted will only equal success-payment payables after
    refund reversals, so every comparison below is a consensus check across
    independent records, and `issues` are grouped by kind. A balanced result
    means the books are internally consistent.
    """
    from ..ledger import commission_for_payment

    db = get_db()
    issues = []
    info = {}

    def _ledger_sum(entry_type):
        rows = db.ledger_entries.find({"entry_type": entry_type})
        return round(sum(float(r.get("amount", 0.0)) for r in rows), 2)

    def _float_sum(cursor, key):
        return round(sum(float(r.get(key, 0.0) or 0.0) for r in cursor), 2)

    success_payments = list(db.payments.find({"status": "success"}))
    captured_payments = list(db.payments.find({"status": {"$in": ["success", "refunded"]}}))
    gross_captured = round(sum(float(p.get("amount", 0.0) or 0.0) for p in captured_payments), 2)
    ledger_gmv = _ledger_sum("PASSENGER_PAYMENT")          # posted at capture
    if abs(gross_captured - ledger_gmv) > 0.01:
        issues.append({
            "kind": "gmv_mismatch", "severity": "high",
            "detail": f"captured payments ({gross_captured}) vs PASSENGER_PAYMENT ledger ({ledger_gmv})"})

    ledger_refund = abs(_ledger_sum("PASSENGER_REFUND"))   # reversals posted at refund
    retained_gmv = round(ledger_gmv - ledger_refund, 2)
    payments_retained = round(sum(float(p.get("amount", 0.0) or 0.0) for p in success_payments), 2)
    if abs(retained_gmv - payments_retained) > 0.01:
        issues.append({
            "kind": "retained_mismatch", "severity": "high",
            "detail": f"retained ledger ({retained_gmv}) vs outstanding success payments ({payments_retained})"})

    # Commission must equal the split frozen on each payment at order creation.
    # Comparing against stored values (not current config) is what makes this a
    # real audit: a later commission change must NOT register as drift. Retained
    # commission is net of reversals, so a fully refunded payment contributes
    # zero -- exactly like the GMV check above.
    recorded_fee = round(_ledger_sum("PLATFORM_FEE") + _ledger_sum("PLATFORM_FEE_REVERSAL"), 2)
    recorded_fee = abs(recorded_fee)
    expected_fee = round(sum(commission_for_payment(p)["platform_fee"] for p in success_payments), 2)
    if abs(recorded_fee - expected_fee) > 0.01:
        issues.append({
            "kind": "commission_mismatch", "severity": "high",
            "detail": f"retained commission ledger ({recorded_fee}) vs frozen commission on "
                      f"outstanding payments ({expected_fee})"})
    unfrozen = [str(p.get("_id")) for p in success_payments if not commission_for_payment(p)["frozen"]]
    if unfrozen:
        info["legacy_unfrozen_payments"] = {"count": len(unfrozen), "sample": unfrozen[:20]}

    processed_refunds = list(db.refunds.find({"status": "processed"}))
    refund_total = round(sum(float(r.get("amount", 0.0) or 0.0) for r in processed_refunds), 2)
    if abs(refund_total - ledger_refund) > 0.01:
        issues.append({
            "kind": "refund_ledger_mismatch", "severity": "high",
            "detail": f"processed refunds ({refund_total}) vs PASSENGER_REFUND ledger ({ledger_refund})"})

    payouts = list(db.payouts.find({}))
    payout_total = round(sum(float(p.get("amount", 0.0) or 0.0) for p in payouts), 2)
    ledger_payout = abs(_ledger_sum("DRIVER_PAYOUT"))
    if abs(payout_total - ledger_payout) > 0.01:
        issues.append({
            "kind": "payout_ledger_mismatch", "severity": "high",
            "detail": f"payouts ({payout_total}) vs DRIVER_PAYOUT ledger ({ledger_payout})"})

    for p in captured_payments:
        bid = p.get("booking_id")
        if bid is None:
            continue
        if not db.ledger_entries.find_one({"payment_id": str(p.get("_id")), "entry_type": "PASSENGER_PAYMENT"}):
            issues.append({"kind": "success_payment_unledgered", "severity": "high",
                           "booking_id": str(bid), "payment_id": str(p.get("_id"))})
        # Per-payment identity: gross == commission + driver net, and the
        # ledgered amounts must equal the split frozen at order creation.
        split = commission_for_payment(p)
        if abs(split["gross"] - round(split["platform_fee"] + split["driver_net"], 2)) > 0.01:
            issues.append({"kind": "split_unbalanced", "severity": "high",
                           "payment_id": str(p.get("_id")),
                           "detail": f"gross {split['gross']} != fee {split['platform_fee']} "
                                     f"+ net {split['driver_net']}"})
        booking = db.bookings.find_one({"_id": bid})
        if booking and booking.get("status") != "confirmed" and not db.refunds.find_one(
                {"payment_id": p.get("_id"), "status": "processed"}):
            issues.append({"kind": "captured_money_unsettled_booking", "severity": "medium",
                           "booking_id": str(bid)})

    open_pending = db.bookings.count_documents({"status": {"$in": ["pending_payment", "payment_failed"]}})
    info["open_pending_bookings"] = open_pending
    info["payouts_pending"] = db.payouts.count_documents({"status": "pending"})
    info["refunds_failed"] = db.refunds.count_documents({"status": "failed"})
    info["gmv"] = retained_gmv
    info["refunds_processed"] = len(processed_refunds)
    info["payouts_total"] = len(payouts)

    return {"ok": True, "status": "balanced" if not issues else "attention",
            "issues": issues, "info": info}


@bp.get("/audit")
@require_admin
@rate_limit("default")
def audit():
    db = get_db()
    action = as_str(request.args.get("action"), "action", max_len=60)
    page = max(int(request.args.get("page") or 1), 1)
    limit = min(int(request.args.get("limit") or 50), 200)
    query = {"action": action} if action else {}
    total = db.audit_logs.count_documents(query)
    rows = list(db.audit_logs.find(query).sort("created_at", -1)
                .skip((page - 1) * limit).limit(limit))
    out = []
    for r in rows:
        out.append({**r, "id": str(r.pop("_id")), "created_at": iso_utc(r.get("created_at"))})
    return {"ok": True, "data": out, "page": page, "total": total}


@bp.get("/payments")
@require_admin
@rate_limit("default")
def payments():
    from bson import ObjectId

    db = get_db()
    page = max(int(request.args.get("page") or 1), 1)
    limit = min(int(request.args.get("limit") or 25), 100)
    total = db.payments.count_documents({})
    rows = list(db.payments.find({}).sort("created_at", -1)
                .skip((page - 1) * limit).limit(limit))
    data = []
    for p in rows:
        data.append({**p, "id": str(p.pop("_id")),
                     "created_at": iso_utc(p.get("created_at")),
                     "updated_at": iso_utc(p.get("updated_at")),
                     "paid_at": iso_utc(p.get("paid_at"))})
    return {"ok": True, "data": data, "page": page, "total": total}


@bp.post("/payouts")
@require_admin
@rate_limit("strict")
def create_payout():
    """Create a payout from a driver's outstanding payable.

    This RESERVES the balance (the driver account is debited and the payout sits
    in `pending`); it does not move money. Settlement happens when the payout is
    submitted to the provider and confirmed back.
    """
    from ..payouts import new_payout, outstanding_payable
    from datetime import timedelta

    db = get_db()
    data = body()
    user_id = to_object_id((data.get("user_id") or ""), "user")
    user = db.users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")

    # Atomic per-driver reservation: two concurrent payout creations for the
    # same driver cannot both consume the same balance. The user row is the
    # mutex; a stale lock expires after PAYOUT_LOCK_TTL_SECONDS.
    now = utcnow()
    lock_seconds = int(current_app.config.get("PAYOUT_LOCK_TTL_SECONDS", 60) or 60)
    claimed = db.users.find_one_and_update(
        {"_id": user_id, "$or": [
            {"payout_lock_until": {"$exists": False}},
            {"payout_lock_until": {"$lte": now}},
        ]},
        {"$set": {"payout_lock_until": now + timedelta(seconds=lock_seconds),
                  "updated_at": now}})
    if claimed is None:
        raise APIError("A payout for this driver is already being created. Try again shortly.",
                       429, code="payout_in_progress")
    try:
        # Balance is re-read AFTER the reservation is held. The amount is
        # optional: omitted means "settle everything outstanding". The
        # outstanding figure subtracts every payout that still holds a debit
        # (pending, processing, paid and failed alike), so money already
        # committed to an attempt cannot be spent a second time.
        requested = data.get("amount")
        payable = outstanding_payable(user_id)
        if requested is not None:
            requested = round(float(requested), 2)
            if requested <= 0:
                raise APIError("Payout amount must be greater than zero.", 422,
                               code="invalid_payout_amount")
            if requested > payable:
                raise APIError(
                    f"Amount exceeds the outstanding payable ({payable}).", 422,
                    code="amount_exceeds_payable")
            amount = requested
        else:
            amount = payable

        if amount <= 0:
            raise APIError("This driver has no outstanding payable.", 422, code="no_balance")

        payout = new_payout(user_id, amount, booking_ids=_unsettled_bookings(db, user_id))
        _audit("payout.create", g.user["_id"], "user", user_id,
               {"amount": payout["amount"], "payout_id": str(payout["_id"])})
        return {"ok": True, "payout": _clean_payout(payout)}
    finally:
        db.users.update_one({"_id": user_id}, {"$unset": {"payout_lock_until": ""}})


def _unsettled_bookings(db, driver_id):
    """Completed bookings whose payable is not inside a still-live payout.

    Voided payouts are excluded: voiding posts a reversing ledger entry, so the
    driver is owed that money again and the trip must reappear here to be
    covered by a replacement payout.
    """
    rows = db.bookings.find({
        "owner_id": driver_id,
        "status": "completed",
    }, {"_id": 1})
    ids = [str(r["_id"]) for r in rows]
    if not ids:
        return []
    settled = set()
    for p in db.payouts.find({"user_id": driver_id, "booking_ids": {"$exists": True, "$ne": []},
                              "status": {"$ne": "voided"}}):
        settled.update(p.get("booking_ids") or [])
    return [b for b in ids if b not in settled]


@bp.post("/payouts/<pid>/submit")
@require_admin
@rate_limit("strict")
def submit_payout(pid):
    """Send a pending payout to the configured provider (pending -> processing).

    Idempotent, so it is safe to retry after a timeout. This does NOT mark the
    payout paid -- only the provider's confirmation does that.
    """
    from ..payouts import submit_payout as _submit

    db = get_db()
    payout_id = to_object_id(pid, "payout")
    payout = _submit(payout_id)
    _audit("payout.submit", g.user["_id"], "payout", payout_id,
           {"status": payout.get("status")})
    return {"ok": True, "payout": _clean_payout(payout)}


@bp.post("/vehicles/<vid>/verify")
@require_admin
@rate_limit("strict")
def review_vehicle(vid):
    """Approve or reject a vehicle's KYC packet.

    The only way a vehicle becomes `verified`. Reviewing a packet that is still
    missing documents is refused upstream (see kyc.review_vehicle), and a
    rejection without a reason is refused too -- a driver who is told nothing
    cannot fix anything.
    """
    from ..kyc import review_vehicle as _review

    db = get_db()
    vehicle_id = to_object_id(vid, "vehicle")
    data = body()
    reason = as_str(data.get("reason"), "reason", max_len=300) or None
    vehicle = _review(vehicle_id, data.get("decision"), str(g.user["_id"]), reason)
    _audit("vehicle.verify", g.user["_id"], "vehicle", vehicle_id,
           {"to": vehicle.get("verification_status"), "reason": reason})
    return {"ok": True,
            "vehicle": {"id": str(vehicle["_id"]),
                        "verification_status": vehicle.get("verification_status"),
                        "kyc_reason": vehicle.get("kyc_reason")}}


@bp.post("/payouts/<pid>/confirm")
@require_admin
@rate_limit("strict")
def confirm_payout(pid):
    """Record that an out-of-band (manual) transfer actually reached the driver.

    This exists ONLY for the `manual` provider, where there is no gateway to
    confirm for us. It is deliberately narrow and heavily audited, because it is
    the one place where a human's word is what moves a payout to `paid`:

    * only a `manual`-provider payout in `processing` may be confirmed;
    * a bank reference (UTR) is REQUIRED, so the payout is reconcilable against
      the bank statement afterwards;
    * the confirmation is idempotent on that reference, so a double submit
      cannot mark the same transfer twice;
    * every attempt, accepted or refused, is written to the audit log.

    A `razorpayx` payout is refused outright -- its settlement is established by
    the signed provider webhook, and letting an admin assert it would defeat
    the point of that control.
    """
    from ..payouts import confirm_payout as _confirm

    db = get_db()
    payout_id = to_object_id(pid, "payout")
    payout = db.payouts.find_one({"_id": payout_id})
    if not payout:
        raise APIError("Payout not found.", 404, code="not_found")

    data = body()
    reference = (as_str(data.get("provider_reference") or data.get("reference"),
                        "provider_reference", max_len=64) or "").strip()
    if not reference:
        raise APIError(
            "A bank reference (UTR) is required to confirm a manual payout.",
            422, code="payout_reference_required")

    if payout.get("provider", "manual") != "manual":
        _audit("payout.confirm_refused", g.user["_id"], "payout", payout_id,
               {"provider": payout.get("provider"), "reason": "non_manual_provider"})
        raise APIError(
            "This payout is settled by the payment provider and cannot be "
            "confirmed manually.", 409, code="payout_not_manual")

    # Reusing a reference that already paid out a DIFFERENT payout would make the
    # bank statement ambiguous, so it is refused outright.
    clash = db.payouts.find_one({"provider": "manual", "status": "paid",
                                 "provider_reference": reference,
                                 "_id": {"$ne": payout_id}})
    if clash:
        _audit("payout.confirm_refused", g.user["_id"], "payout", payout_id,
               {"reason": "duplicate_reference", "reference": reference})
        raise APIError(
            "That bank reference is already recorded against another payout.",
            409, code="payout_reference_in_use")

    settled = _confirm(payout_id, reference, source="manual")
    _audit("payout.confirm", g.user["_id"], "payout", payout_id,
           {"provider_reference": reference, "amount": payout.get("amount"),
            "status": settled.get("status")})
    return {"ok": True, "payout": _clean_payout(db.payouts.find_one({"_id": payout_id}))}


@bp.post("/payouts/<pid>/retry")
@require_admin
@rate_limit("strict")
def retry_payout(pid):
    """Re-open a failed payout so it can be submitted again (failed -> pending)."""
    from ..payouts import retry_payout as _retry

    db = get_db()
    payout_id = to_object_id(pid, "payout")
    payout = _retry(payout_id)
    _audit("payout.retry", g.user["_id"], "payout", payout_id, {})
    return {"ok": True, "payout": _clean_payout(payout)}


@bp.post("/payouts/<pid>/void")
@require_admin
@rate_limit("strict")
def void_payout_route(pid):
    """Cancel a FAILED payout and return the reserved money to the driver.

    This is the only way reserved funds become payable again, and it is
    deliberately narrow: only a `failed` payout can be voided, the reversal is
    posted as a separate immutable ledger entry, and a `paid` payout can never
    be voided because the money has already left the platform.
    """
    from ..payouts import void_payout as _void

    db = get_db()
    payout_id = to_object_id(pid, "payout")
    data = body()
    reason = as_str(data.get("reason"), "reason", max_len=200) or "Voided by admin"
    payout = _void(payout_id, reason)
    _audit("payout.void", g.user["_id"], "payout", payout_id, {"reason": reason})
    return {"ok": True, "payout": _clean_payout(db.payouts.find_one({"_id": payout_id}))}


@bp.patch("/payouts/<pid>")
@require_admin
@rate_limit("default")
def advance_payout(pid):
    """Move a payout along its lifecycle.

    `paid` is deliberately NOT settable here. A payout becomes paid only when
    the provider confirms the transfer (see /api/payments/payout-webhook), so no
    human action can mark money as sent. Admins can record a failure, re-open a
    failed payout, or void a failed payout to release the reservation
    (POST /api/admin/payouts/{id}/void).
    """
    from ..payouts import fail_payout

    db = get_db()
    payout_id = to_object_id(pid, "payout")
    payout = db.payouts.find_one({"_id": payout_id})
    if not payout:
        raise APIError("Payout not found.", 404, code="not_found")
    data = body()
    status = as_str(data.get("status"), "status", max_len=20) or ""

    if status == "paid":
        raise APIError(
            "A payout is marked paid only by provider confirmation, not by an "
            "admin action. Submit it and wait for the provider callback.",
            409, code="payout_confirmation_required")
    if status == "failed":
        reason = as_str(data.get("reason"), "reason", max_len=200) or "Marked failed by admin"
        payout = fail_payout(payout_id, reason, source="admin")
    elif status == "processing":
        raise APIError("Use POST /api/admin/payouts/{id}/submit to send this payout.",
                       409, code="payout_submit_required")
    elif status == "pending":
        from ..payouts import retry_payout as _retry

        payout = _retry(payout_id)
    else:
        raise APIError("Invalid payout status.", 422, code="validation_error",
                       details={"allowed": ["failed", "pending"]})

    _audit("payout.update", g.user["_id"], "payout", payout_id, {"to": payout.get("status")})
    fresh = db.payouts.find_one({"_id": payout_id})
    return {"ok": True, "payout": _clean_payout(fresh)}