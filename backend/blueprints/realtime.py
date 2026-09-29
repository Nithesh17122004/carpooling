"""Realtime driver locations: share + stream.

POST /api/rides/<rid>/location   driver pushes a lat/lng ping
GET  /api/rides/<rid>/location   live SSE stream (driver + confirmed riders)

Privacy & lifecycle rules:
- Only the ride owner (driver) may POST a ping.
- Only the driver and riders with a CONFIRMED booking may subscribe.
- Tracking is only meaningful while the ride is still in a live state
  (published/full/driver_en_route/boarding/in_progress). Once a ride is
  cancelled/completed/expired the stream and the ping endpoint refuse, so
  no rider can keep tracking a finished or re-used vehicle.
- GPS updates are fan-out via Redis pub/sub (rm:live:<ride_id>); MongoDB is
  only written as SAMPLED history (configurable interval), never per-ping.
"""

import json
import time as _time
from time import sleep

from flask import Blueprint, Response, g, request, current_app, stream_with_context

from ..db import get_db, to_object_id, utcnow
from ..errors import APIError
from ..ratelimit import rate_limit
from ..security import require_auth
from ..timeutil import iso_utc

bp = Blueprint("realtime", __name__, url_prefix="/api/rides")

_TRACKABLE = ("published", "full", "driver_en_route", "boarding", "in_progress", "active")
_SAMPLE_SECONDS = 10


def _publish(ride_id, payload):
    client = getattr(current_app, "extensions", {}).get("rm_redis")
    if client is None:
        return
    try:
        client.publish(f"rm:live:{ride_id}", json.dumps(payload, default=str))
    except Exception:  # noqa: BLE001 - best effort
        pass


def _trackable(db, ride_id):
    ride = db.rides.find_one({"_id": ride_id}, {"status": 1, "departure_at": 1})
    return ride is not None and ride.get("status") in _TRACKABLE


def _sample_history(db, ride_id, user_id, lat, lng, now):
    """Persist a sampled history point (never per-ping)."""
    interval = _SAMPLE_SECONDS
    last = db.locations.find_one(
        {"ride_id": ride_id}, sort=[("timestamp", -1)], projection={"timestamp": 1}
    )
    if last and (now - last["timestamp"]).total_seconds() < interval:
        return False
    db.locations.insert_one(
        {
            "ride_id": ride_id,
            "user_id": user_id,
            "lat": lat,
            "lng": lng,
            "timestamp": now,
        }
    )
    return True


@bp.post("/<rid>/location")
@require_auth
@rate_limit("strict")
def share_location(rid):
    from ..validators import as_float

    db = get_db()
    ride_id = to_object_id(rid, "ride")
    ride = db.rides.find_one({"_id": ride_id})
    if not ride:
        raise APIError("Ride not found.", 404, code="not_found")
    if str(ride["owner_id"]) != str(g.user["_id"]):
        raise APIError("Only the driver can share the live location.", 403, code="forbidden")
    if ride["status"] not in _TRACKABLE:
        raise APIError("This ride is not tracking live location.", 409, code="trip_not_active")

    data = request.get_json(silent=True) or {}
    lat = as_float(data.get("lat"), "lat", minimum=-90, maximum=90, required=True)
    lng = as_float(data.get("lng"), "lng", minimum=-180, maximum=180, required=True)

    now = utcnow()
    _sample_history(db, ride_id, g.user["_id"], lat, lng, now)
    _publish(ride_id, {"ride_id": str(ride_id), "lat": lat, "lng": lng,
                       "timestamp": iso_utc(now)})
    return {"ok": True, "saved": True, "timestamp": iso_utc(now)}


@bp.get("/<rid>/location")
@require_auth
@rate_limit("default")
def stream_location(rid):
    db = get_db()
    ride_id = to_object_id(rid, "ride")
    ride = db.rides.find_one({"_id": ride_id})
    if not ride:
        raise APIError("Ride not found.", 404, code="not_found")
    if not _trackable(db, ride_id):
        raise APIError("Tracking for this ride has ended.", 409, code="trip_not_active")
    is_driver = str(ride["owner_id"]) == str(g.user["_id"])
    if not is_driver:
        confirmed = db.bookings.find_one(
            {"ride_id": ride_id, "rider_id": g.user["_id"],
             "status": "confirmed"})
        if not confirmed:
            raise APIError("Only confirmed passengers can watch the live location.",
                           403, code="forbidden")

    # recent position snapshot so late joiners get immediate context
    last = db.locations.find_one({"ride_id": ride_id}, sort=[("timestamp", -1)])
    ride_key = str(ride_id)

    def generator():
        client = getattr(current_app, "extensions", {}).get("rm_redis")
        pubsub = None
        if client is not None:
            try:
                pubsub = client.pubsub()
                pubsub.subscribe(f"rm:live:{ride_key}")
            except Exception:  # noqa: BLE001
                pubsub = None
        try:
            if last:
                yield f"data: {json.dumps({'lat': last['lat'], 'lng': last['lng'], 'timestamp': iso_utc(last['timestamp'])})}\n\n"
            yield ":connected\n\n"
            if pubsub is not None:
                last_status_check = 0
                while True:
                    msg = pubsub.get_message(timeout=15)
                    now = int(_time.time())
                    if now - last_status_check > 15:
                        last_status_check = now
                        if not _trackable(db, ride_id):
                            yield "event: end\ndata: {\"reason\":\"trip_ended\"}\n\n"
                            return
                    if msg and msg.get("type") == "message":
                        yield f"data: {msg['data']}\n\n"
                    else:
                        yield ":heartbeat\n\n"
            else:
                while True:
                    if not _trackable(db, ride_id):
                        yield "event: end\ndata: {\"reason\":\"trip_ended\"}\n\n"
                        return
                    rec = db.locations.find_one({"ride_id": ride_id}, sort=[("timestamp", -1)])
                    if rec:
                        yield f"data: {json.dumps({'lat': rec['lat'], 'lng': rec['lng'], 'timestamp': iso_utc(rec['timestamp'])})}\n\n"
                    yield ":heartbeat\n\n"
                    sleep(5)
        finally:
            if pubsub is not None:
                try:
                    pubsub.unsubscribe()
                except Exception:  # noqa: BLE001
                    pass

    return Response(stream_with_context(generator()), mimetype="text/event-stream", headers={
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })