"""Notifications API: feed, prefs, and a live SSE stream (Redis-backed)."""

import json
import time

from flask import Blueprint, Response, g, request, current_app, stream_with_context

from ..db import get_db, to_object_id, utcnow
from ..errors import APIError
from .. import notifications as notify_mod
from ..ratelimit import rate_limit
from ..security import require_auth
from ..timeutil import iso_utc
from ..validators import as_bool, body

bp = Blueprint("notify", __name__, url_prefix="/api/notifications")

DEFAULT_PREFS = {
    "booking_confirmed": True,
    "booking_cancelled": True,
    "new_booking": True,
    "ride_cancelled": True,
    "chat_messages": True,
    "promotions": False,
}


def _clean(row):
    row = dict(row)
    return {
        "id": str(row.get("_id", "")),
        "title": row.get("title"),
        "body": row.get("body"),
        "notification_type": row.get("notification_type"),
        "ref_type": row.get("ref_type"),
        "ref_id": row.get("ref_id"),
        "data": row.get("data", {}),
        "read": bool(row.get("read", False)),
        "created_at": iso_utc(row.get("created_at")),
    }


@bp.get("")
@require_auth
@rate_limit("default")
def list_notifications():
    limit = min(int(request.args.get("limit") or 20), 100)
    rows = notify_mod.list_for_user(g.user["_id"], limit=limit)
    return {"ok": True, "data": [_clean(r) for r in rows]}


@bp.get("/unread-count")
@require_auth
@rate_limit("default")
def unread():
    return {"ok": True, "count": notify_mod.unread_count(g.user["_id"])}


@bp.patch("/<nid>/read")
@require_auth
@rate_limit("default")
def mark_read(nid):
    notify_mod.mark_read(g.user["_id"], to_object_id(nid, "notification"))
    return {"ok": True}


@bp.patch("/read-all")
@require_auth
@rate_limit("default")
def read_all():
    notify_mod.mark_all_read(g.user["_id"])
    return {"ok": True}


@bp.get("/prefs")
@require_auth
@rate_limit("default")
def get_prefs():
    prefs = get_db().notification_preferences.find_one({"user_id": g.user["_id"]})
    merged = dict(DEFAULT_PREFS)
    if prefs and isinstance(prefs.get("prefs"), dict):
        merged.update(prefs["prefs"])
    return {"ok": True, "prefs": merged}


@bp.put("/prefs")
@require_auth
@rate_limit("default")
def put_prefs():
    data = body()
    prefs = {}
    for key, default in DEFAULT_PREFS.items():
        if key in data:
            prefs[key] = as_bool(data.get(key), default)
    if not prefs:
        return {"ok": True, "prefs": dict(DEFAULT_PREFS)}
    get_db().notification_preferences.update_one(
        {"user_id": g.user["_id"]},
        {"$set": {"prefs": prefs, "updated_at": utcnow()}},
        upsert=True,
    )
    return {"ok": True, "prefs": prefs}


@bp.get("/stream")
@require_auth
@rate_limit("strict")
def stream():
    """Server-Sent Events stream. Backed by Redis pub/sub when configured;
    otherwise a keep-alive heartbeat (long-poll clients should also poll the
    standard feed endpoint)."""

    user_id = str(g.user["_id"])

    def generator():
        client = getattr(current_app, "extensions", {}).get("rm_redis")
        pubsub = None
        if client is not None:
            try:
                pubsub = client.pubsub()
                pubsub.subscribe(f"rm:notify:{user_id}")
            except Exception:  # noqa: BLE001
                pubsub = None
        try:
            yield ":connected\n\n"
            if pubsub is not None:
                while True:
                    msg = pubsub.get_message(timeout=15)
                    if msg and msg.get("type") == "message":
                        yield f"data: {msg['data']}\n\n"
                    else:
                        yield ":heartbeat\n\n"
            else:
                while True:
                    time.sleep(15)
                    yield ":heartbeat\n\n"
        finally:
            if pubsub is not None:
                try:
                    pubsub.unsubscribe()
                except Exception:  # noqa: BLE001
                    pass

    return Response(
        stream_with_context(generator()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )