"""Notifications: persisted per-user feed + optional Redis pub/sub.

Without Redis the feed is stored in MongoDB (works single-instance). With
REDIS_URL configured the same message is published on channel `rm:notify:<uid>`
so a realtime SSE endpoint can stream it.
"""

import json

from .db import get_db, utcnow


def notify(user_id, title, body, ref_type=None, ref_id=None, data=None):
    db = get_db()
    doc = {
        "user_id": user_id,
        "title": (title or "")[:120],
        "body": (body or "")[:500],
        "ref_type": ref_type or None,
        "ref_id": str(ref_id) if ref_id else None,
        "data": data or {},
        "read": False,
        "notification_type": ref_type or "info",
        "created_at": utcnow(),
    }
    db.notifications.insert_one(doc)
    _publish(user_id, doc)
    return doc


def _publish(user_id, doc):
    from flask import current_app

    client = getattr(current_app, "extensions", {}).get("rm_redis")
    if client is None:
        return
    try:
        payload = dict(doc)
        payload["id"] = str(doc.pop("_id", ""))
        client.publish(f"rm:notify:{user_id}", json.dumps(payload, default=str))
    except Exception:  # noqa: BLE001 - pub/sub is best-effort
        pass


def list_for_user(user_id, limit=50):
    rows = list(get_db().notifications.find({"user_id": user_id})
                .sort("created_at", -1).limit(limit))
    return rows


def mark_read(user_id, notification_id):
    from ..db import to_object_id

    get_db().notifications.update_one(
        {"_id": to_object_id(notification_id, "notification"), "user_id": user_id},
        {"$set": {"read": True}})


def mark_all_read(user_id):
    get_db().notifications.update_many({"user_id": user_id, "read": False},
                                       {"$set": {"read": True}})


def unread_count(user_id):
    return get_db().notifications.count_documents({"user_id": user_id, "read": False})