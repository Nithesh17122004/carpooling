"""Ride publishing, search, and management endpoints.

Hardening (vs the legacy endpoint set):
  - `departure_at` is computed SERVER-SIDE from wall-clock `departure_date` +
    `departure_time` + ride `timezone` (default Asia/Kolkata). Naive-UTC.
  - `distance_km` is computed SERVER-SIDE (OSRM with haversine fallback) and
    the client-supplied value is ignored.
  - Overlapping rides on the same vehicle are rejected within a configurable
    buffer window.
  - State transitions are validated by states.py; cancellation is refused once
    departure has passed (relying on the cancellation-cutoff policy).
  - Neither `earnings` nor `seats_booked` balances are exposed to non-owners.
"""

import re
from datetime import datetime, timedelta

from flask import Blueprint, current_app, g, request

from ..db import get_db, to_object_id, utcnow
from ..errors import APIError
from ..ratelimit import rate_limit
from ..security import optional_auth, require_auth
from ..states import (
    BOOKING_AWAITING_COMPLETION,
    BOOKING_CANCELLED,
    BOOKING_COMPLETED,
    BOOKING_CONFIRMED,
    BOOKING_NO_SHOW,
    RIDE_BOOKABLE,
    RIDE_BOARDING,
    RIDE_CANCELLED,
    RIDE_COMPLETED,
    RIDE_DRIVER_EN_ROUTE,
    RIDE_EXPIRED,
    RIDE_IN_PROGRESS,
    RIDE_OCCUPIES_VEHICLE,
    RIDE_PUBLISHED,
    can_transition_ride,
    ride_transition_error,
)
from ..timeutil import combine_local, from_utc, iso_utc, is_past
from .bookings import _release_seats, clean_booking
from ..validators import (
    VEHICLE_TYPES,
    as_float,
    as_int,
    as_str,
    body,
    haversine_km,
    parse_point,
    require_fields,
    valid_date_str,
    valid_time_str,
    valid_timezone,
)

bp = Blueprint("rides", __name__, url_prefix="/api/rides")

SORTS = {
    "early": [("departure_at", 1)],
    "cheap": [("fare_per_seat", 1), ("departure_at", 1)],
    "nearest": [("departure_at", 1)],
    "score": [("departure_at", 1)],  # relevance ranking handled post-query
}

# Legacy docs store status "active"; canonical state is "published". Queries
# must match both until a data migration rewrites existing rows.
_BOOKABLE_DB = set(RIDE_BOOKABLE) | {"active"}
_OCCUPIES_DB = set(RIDE_OCCUPIES_VEHICLE) | {"active"}
_UPCOMING_DB = {"published", "active", "full"}


def _now():
    return utcnow()


def _expire_past():
    now = _now()
    get_db().rides.update_many(
        {"status": {"$in": ["active", "published", "full"]}, "departure_at": {"$lt": now}},
        {"$set": {"status": RIDE_COMPLETED, "updated_at": now}},
    )


def server_distance_km(origin, destination):
    """Authoritative server-side distance: OSRM first, haversine fallback."""
    try:
        from .geo import route_distance_minutes

        dist, _dur = route_distance_minutes(origin, destination)
        if dist:
            return round(dist, 1)
    except Exception:  # noqa: BLE001 - any provider issue -> fallback
        pass
    return haversine_km(origin, destination)


def embedded_owner(owner):
    return {
        "id": str(owner["_id"]),
        "name": owner.get("name"),
        "photo_url": owner.get("photo_url") or "",
        "rating": owner.get("rating", 0) or 0,
        "total_rides": owner.get("total_rides", 0) or 0,
    }


def clean_ride_public(ride, owner_view=False, include_plate=False):
    ride = dict(ride)
    vehicle = dict(ride.get("vehicle") or {})
    if not include_plate:
        # registration plate is private: only the driver and confirmed riders see it
        vehicle.pop("number", None)
    out = {
        "id": str(ride["_id"]),
        "owner_id": str(ride["owner_id"]),
        "vehicle_id": str(ride["vehicle_id"]),
        "vehicle": vehicle,
        "origin": ride.get("origin"),
        "destination": ride.get("destination"),
        "departure_date": ride.get("departure_date"),
        "departure_time": ride.get("departure_time"),
        "timezone": ride.get("timezone", "Asia/Kolkata"),
        "departure_at": iso_utc(ride.get("departure_at")),
        "seats_total": ride.get("seats_total", 0),
        "seats_available": ride.get("seats_available", 0),
        "fare_per_seat": ride.get("fare_per_seat", 0),
        "distance_km": ride.get("distance_km"),
        "notes": ride.get("notes") or "",
        "status": ride.get("status"),
        "created_at": iso_utc(ride.get("created_at")),
    }
    if owner_view:
        out["earnings"] = ride.get("earnings", 0)
    return out


def _attach_driver(ride, owner_view=False, include_plate=False):
    out = clean_ride_public(ride, owner_view=owner_view, include_plate=include_plate)
    owner = get_db().users.find_one({"_id": ride["owner_id"]})
    out["owner"] = embedded_owner(owner) if owner else None
    return out


def _overlap_window(departure_at, duration_minutes):
    """(start, end) UTC window including the buffer preceding the ride."""
    from datetime import timedelta

    buffer_min = int(current_app.config.get("RIDE_OVERLAP_BUFFER_MINUTES", 30) or 0)
    buffer = timedelta(minutes=buffer_min)
    duration = timedelta(minutes=int(duration_minutes or 0))
    return departure_at - buffer, departure_at + duration + buffer


def _check_overlap(db, vehicle_id, departure_at, duration_minutes, exclude_ride_id=None):
    start, end = _overlap_window(departure_at, duration_minutes)
    query = {
        "vehicle_id": vehicle_id,
        "status": {"$in": list(_OCCUPIES_DB)},
        "departure_at": {"$gte": start, "$lt": end},
    }
    if exclude_ride_id:
        query["_id"] = {"$ne": exclude_ride_id}
    clash = db.rides.find_one(query, {"_id": 1, "origin": 1, "destination": 1,
                                      "departure_at": 1})
    if clash:
        raise APIError(
            "This vehicle is already used for another ride that overlaps "
            "this time window (including the buffer).",
            409, code="overlapping_ride",
            details={"clash_id": str(clash["_id"]),
                     "clash_departure_at": iso_utc(clash.get("departure_at"))},
        )


def _ride_duration_estimate(distance_km):
    from flask import current_app, has_app_context

    if has_app_context():
        minutes = current_app.config.get("RIDE_DEFAULT_DURATION_MINUTES", 20)
    else:
        from ..config import Config

        minutes = Config.RIDE_DEFAULT_DURATION_MINUTES
    duration = round(float(distance_km or 20) / 35 * 60 + 10)
    return duration or minutes


def _compute_departure_at(ddate, dtime, tzname):
    """Wall-clock (date, time, tz) -> canonical naive-UTC instant; must be future."""
    departure_at = combine_local(ddate, datetime.strptime(dtime, "%H:%M").time(), tzname)
    if departure_at <= _now():
        raise APIError("Departure time cannot be in the past.", 422, code="past_departure",
                       details={"fields": ["departure_time"]})
    return departure_at


def _match_score(ride, origin_pt=None, origin_radius_km=None,
                 dest_pt=None, dest_radius_km=None, time_from=None,
                 max_fare=None, now=None):
    """0..100 relevance for a search result. Signals are normalized to 0..1 and
    weighted only by the signals actually supplied by the caller, so a labeled
    search (no coords) still produces a useful score."""
    now = now or _now()
    origin, dest = ride.get("origin") or {}, ride.get("destination") or {}
    comps = []

    if origin_pt is not None and origin_radius_km:
        km = haversine_km(origin_pt, origin)
        comps.append((max(0.0, 1 - (km / origin_radius_km)), 0.45))

    if dest_pt is not None and dest_radius_km:
        km = haversine_km(dest_pt, dest)
        comps.append((max(0.0, 1 - (km / dest_radius_km)), 0.30))

    if time_from:
        fmt = "%H:%M"
        tmin = datetime.strptime(time_from, fmt).time()
        rmin = datetime.strptime(ride.get("departure_time") or "00:00", fmt).time()
        delta = abs((datetime.combine(datetime(2000, 1, 1), tmin) -
                     datetime.combine(datetime(2000, 1, 1), rmin)).total_seconds())
        comps.append((max(0.0, 1 - (delta / 21600.0)), 0.15))

    if max_fare:
        fare = ride.get("fare_per_seat") or 0
        comps.append((1.0 if fare <= max_fare
                      else max(0.0, 1 - ((fare - max_fare) / max_fare)), 0.10))

    if not comps:
        return None
    total = sum(w for _, w in comps)
    score = round(sum(s * w for s, w in comps) / total * 100)
    return max(0, min(100, score))


# ------------------------------------------------------------------- create
def _ride_payload(user_id, vehicle, origin, destination, ddate, dtime, tzname,
                  departure_at, duration_minutes, seats_total, fare, notes,
                  distance_km, **extra):
    """Server-canonical ride document shared by single publish and recurring
    commute generation. Extra fields (e.g. `recurring_key`) are appended."""
    payload = {
        "owner_id": user_id,
        "vehicle_id": vehicle["_id"],
        "vehicle": {
            "type": vehicle.get("vehicle_type"),
            "model": vehicle.get("vehicle_model") or "",
            "number": vehicle.get("vehicle_number"),
            "color": vehicle.get("color") or "",
            "seat_count": vehicle.get("seat_count", 1),
        },
        "origin": origin,
        "origin_location": {"type": "Point",
                            "coordinates": [origin["lng"], origin["lat"]]},
        "destination": destination,
        "destination_location": {"type": "Point",
                                 "coordinates": [destination["lng"], destination["lat"]]},
        "departure_date": ddate.isoformat(),
        "departure_time": dtime,
        "timezone": tzname,
        "departure_at": departure_at,
        "duration_minutes": duration_minutes,
        "seats_total": seats_total,
        "seats_available": seats_total,
        "fare_per_seat": fare,
        "distance_km": distance_km,
        "notes": notes,
        "status": RIDE_PUBLISHED,  # stored canonical; mirrors legacy "active" for the UI
        "earnings": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    payload.update(extra)
    return payload


@bp.post("")
@require_auth
@rate_limit("strict")
def create_ride():
    db = get_db()
    data = body()
    require_fields(data, "vehicle_id", "origin", "destination",
                   "departure_date", "departure_time", "seats_total", "fare_per_seat")

    vehicle_id = to_object_id(data.get("vehicle_id"), "vehicle")
    vehicle = db.vehicles.find_one({"_id": vehicle_id, "user_id": g.user["_id"]})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")

    # A driver may only take paying passengers once THREE independent things
    # are true: the driver is who they say they are, the vehicle is verified, and
    # the vehicle is registered. Checked here, before any ride document is
    # written, so an unverified driver cannot build up a schedule and publish it
    # later. Each gate raises its own error, so the driver is told which of the
    # three to fix rather than being given a generic "not allowed".
    from .. import rc as rc_mod
    from ..identity import assert_can_publish as assert_driver_verified
    from ..kyc import assert_can_publish

    assert_driver_verified(g.user)
    assert_can_publish(vehicle)
    rc_mod.assert_rc_approved(vehicle)

    origin = parse_point(data.get("origin"), required=True)
    destination = parse_point(data.get("destination"), required=True)
    ddate = valid_date_str(data.get("departure_date"), required=True)
    dtime = valid_time_str(data.get("departure_time"), required=True)
    tzname = valid_timezone(data.get("timezone"), default=None) or "Asia/Kolkata"

    seats_total = as_int(data.get("seats_total"), "seats_total", minimum=1,
                         maximum=vehicle.get("seat_count", 12), required=True)
    fare = as_int(data.get("fare_per_seat"), "fare_per_seat", minimum=1, maximum=100000,
                  required=True)
    notes = as_str(data.get("notes"), "notes", max_len=300) or ""

    # distance is server truth -- client value is deliberately ignored
    distance_km = server_distance_km(origin, destination)
    departure_at = _compute_departure_at(ddate, dtime, tzname)
    duration_min = _ride_duration_estimate(distance_km)
    _check_overlap(db, vehicle_id, departure_at, duration_min)

    ride = _ride_payload(g.user["_id"], vehicle, origin, destination, ddate, dtime,
                         tzname, departure_at, duration_min, seats_total, fare,
                         notes, distance_km)
    result = db.rides.insert_one(ride)
    ride["_id"] = result.inserted_id
    return {"ok": True, "ride": _attach_driver(ride, owner_view=True)}, 201


# ------------------------------------------------------------------- recurring
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
RECURRING_MAX_WEEKS = 12
RECURRING_MAX_OCCURRENCES = 104


@bp.post("/recurring")
@require_auth
@rate_limit("strict")
def create_recurring():
    """Create a weekly commute series. Each matching weekday within the
    horizon becomes an independent published ride (server-computed UTC
    departure, same validate-and-check as single publish). Conflicts and past
    occurrences are skipped per-date with a reason; the batch never fails
    wholesale. `recurring_key` makes the series idempotent so a retry cannot
    double-create occurrences."""
    db = get_db()
    data = body()
    require_fields(data, "vehicle_id", "origin", "destination",
                   "departure_time", "days", "seats_total", "fare_per_seat")

    vehicle_id = to_object_id(data.get("vehicle_id"), "vehicle")
    vehicle = db.vehicles.find_one({"_id": vehicle_id, "user_id": g.user["_id"]})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")

    origin = parse_point(data.get("origin"), required=True)
    destination = parse_point(data.get("destination"), required=True)
    dtime = valid_time_str(data.get("departure_time"), required=True)
    tzname = valid_timezone(data.get("timezone"), default=None) or "Asia/Kolkata"

    raw_days = data.get("days")
    if not isinstance(raw_days, list) or not raw_days:
        raise APIError("At least one weekday is required (e.g. ['mon','wed']).",
                       422, code="invalid_days", details={"fields": ["days"]})
    chosen = set()
    for d in raw_days:
        if not isinstance(d, str) or d.lower() not in WEEKDAYS:
            raise APIError("Invalid weekday.", 422, code="invalid_days",
                           details={"fields": ["days"], "value": d})
        chosen.add(WEEKDAYS[d.lower()])

    weeks = as_int(data.get("weeks"), "weeks", minimum=1,
                   maximum=RECURRING_MAX_WEEKS)
    end_date = None
    if data.get("end_date") and not weeks:
        end_date = valid_date_str(data.get("end_date"), required=True)
    start_date = None
    if data.get("start_date"):
        start_date = valid_date_str(data.get("start_date"), required=True)

    seats_total = as_int(data.get("seats_total"), "seats_total", minimum=1,
                         maximum=vehicle.get("seat_count", 12), required=True)
    fare = as_int(data.get("fare_per_seat"), "fare_per_seat", minimum=1, maximum=100000,
                  required=True)
    notes = as_str(data.get("notes"), "notes", max_len=300) or ""
    key = as_str(data.get("recurring_key"), "recurring_key", max_len=64) or None

    distance_km = server_distance_km(origin, destination)
    duration_min = _ride_duration_estimate(distance_km)
    now = _now()
    start_local = start_date or from_utc(now, tzname).date()
    if end_date:
        horizon = (end_date - start_local).days + 1
    else:
        horizon = weeks * 7
    horizon = max(horizon, 1)

    created = []
    skipped = []
    count = 0
    for offset in range(horizon):
        cand = start_local + timedelta(days=offset)
        if cand.weekday() not in chosen:
            continue
        if count >= RECURRING_MAX_OCCURRENCES:
            break
        departure_at = combine_local(cand, datetime.strptime(dtime, "%H:%M").time(),
                                     tzname)
        if departure_at <= now:
            skipped.append({"date": cand.isoformat(), "reason": "past"})
            continue
        if key:
            existing = db.rides.find_one({"recurring_key": key,
                                          "departure_at": departure_at}, {"_id": 1})
            if existing:
                skipped.append({"date": cand.isoformat(), "reason": "duplicate"})
                continue
        try:
            _check_overlap(db, vehicle_id, departure_at, duration_min)
        except APIError as err:
            skipped.append({"date": cand.isoformat(), "reason": "conflict",
                            "details": err.details})
            continue
        extra = {"recurring_key": key} if key else {}
        payload = _ride_payload(g.user["_id"], vehicle, origin, destination, cand,
                                dtime, tzname, departure_at, duration_min, seats_total,
                                fare, notes, distance_km, **extra)
        ride_id = db.rides.insert_one(payload).inserted_id
        count += 1
        created.append({"ride_id": str(ride_id), "departure_date": cand.isoformat(),
                        "departure_time": dtime,
                        "departure_at": iso_utc(departure_at)})

    return {"ok": True, "recurring_key": key, "created": created,
            "skipped": skipped, "count": count}, 201


# ------------------------------------------------------------------- search
@bp.get("/search")
@optional_auth
@rate_limit("default")
def search_rides():
    _expire_past()
    db = get_db()
    now = _now()
    query = {"status": {"$in": list(_BOOKABLE_DB)}, "departure_at": {"$gte": now}}

    origin = as_str(request.args.get("origin"), "origin", max_len=160)
    destination = as_str(request.args.get("destination"), "destination", max_len=160)
    if origin:
        query["origin.label"] = {"$regex": re.escape(origin), "$options": "i"}
    if destination:
        query["destination.label"] = {"$regex": re.escape(destination), "$options": "i"}

    ddate = valid_date_str(request.args.get("date"))
    if ddate:
        query["departure_date"] = ddate.isoformat()
    time_from = valid_time_str(request.args.get("time_from"))
    if time_from:
        query["departure_time"] = {"$gte": time_from}
    vtype = as_str(request.args.get("type"), "type", max_len=20)
    if vtype and vtype in VEHICLE_TYPES:
        query["vehicle.type"] = vtype
    max_fare = as_int(request.args.get("max_fare"), "max_fare", minimum=1)
    if max_fare:
        query["fare_per_seat"] = {"$lte": max_fare}
    seats = as_int(request.args.get("seats"), "seats", minimum=1)
    if seats:
        query["seats_available"] = {"$gte": seats}

    sort_key = request.args.get("sort", "early")
    sort = SORTS.get(sort_key, SORTS["early"])
    limit = min(as_int(request.args.get("limit"), "limit", minimum=1) or 20, 50)
    page = max(as_int(request.args.get("page"), "page", minimum=1) or 1, 1)

    EARTH_RADIUS_KM = 6371.0088
    # Origin radius: matched server-side BEFORE counting/pagination, so
    # `total`/`pages` reflect the geofenced set (no post-pagination filtering).
    lat = as_float(request.args.get("lat"), "lat", minimum=-90, maximum=90)
    lng = as_float(request.args.get("lng"), "lng", minimum=-180, maximum=180)
    radius_km = as_int(request.args.get("radius_km"), "radius_km", minimum=1)
    geo_origin = None
    if lat is not None and lng is not None and radius_km:
        geo_origin = ({"lat": lat, "lng": lng}, radius_km)

    # Destination radius (optional): e.g. "rides within 10 km of the airport".
    dlat = as_float(request.args.get("dest_lat"), "dest_lat", minimum=-90, maximum=90)
    dlng = as_float(request.args.get("dest_lng"), "dest_lng", minimum=-180, maximum=180)
    dradius_km = as_int(request.args.get("dest_radius_km"), "dest_radius_km", minimum=1)
    dest_filter = None
    if dlat is not None and dlng is not None and dradius_km:
        dest_filter = {"destination_location": {
            "$geoWithin": {"$centerSphere": [[dlng, dlat], dradius_km / EARTH_RADIUS_KM]}}}

    # Geo predicates are combined with an explicit $and (MongoDB permits only
    # one geospatial predicate per top-level query).
    filters = [query]
    if geo_origin is not None:
        point, radius = geo_origin
        filters.append({"origin_location": {
            "$geoWithin": {"$centerSphere": [[point["lng"], point["lat"]],
                                             radius / EARTH_RADIUS_KM]}}})
    if dest_filter is not None:
        filters.append(dest_filter)
    if len(filters) > 1:
        query = {"$and": filters}

    total = db.rides.count_documents(query)
    rows = []
    if sort_key == "nearest" and geo_origin is not None:
        # True nearest-first pagination via $geoNear (needs the 2dsphere index).
        # Falls back to origin-radius + distance sort if the index is absent.
        point, radius = geo_origin
        try:
            near_query = [f for f in filters if "origin_location" not in f]
            near_q = near_query[0] if len(near_query) == 1 else {"$and": near_query}
            pipeline = [{"$geoNear": {
                "near": {"type": "Point", "coordinates": [point["lng"], point["lat"]]},
                "distanceField": "_dist_m",
                "maxDistance": radius * 1000,
                "spherical": True,
                "query": near_q,
            }}]
            if dest_filter is not None:
                pipeline.append({"$match": dest_filter})
            pipeline += [{"$skip": (page - 1) * limit}, {"$limit": limit}]
            rows = list(db.rides.aggregate(pipeline))
        except Exception:  # noqa: BLE001 - index not present yet -> approximate sort
            rows = list(db.rides.find(query).sort(SORTS["early"])
                        .skip((page - 1) * limit).limit(limit))
            rows = sorted(rows, key=lambda r: haversine_km(point, r.get("origin") or {}))
    else:
        base = db.rides.find(query)
        if sort_key == "score":
            # relevance ordering needs the full matched set before slicing
            rows = list(base)
        else:
            rows = list(base.sort(sort)
                        .skip((page - 1) * limit).limit(limit))

    owner_ids = {r["owner_id"] for r in rows}
    owners = {u["_id"]: u for u in db.users.find({"_id": {"$in": list(owner_ids)}})}

    score_ctx = {}
    if geo_origin is not None:
        score_ctx["origin_pt"], score_ctx["origin_radius_km"] = geo_origin
    if dlat is not None and dlng is not None and dradius_km:
        score_ctx["dest_pt"] = {"lat": dlat, "lng": dlng}
        score_ctx["dest_radius_km"] = dradius_km
    if time_from:
        score_ctx["time_from"] = time_from
    if max_fare:
        score_ctx["max_fare"] = max_fare

    data = []
    for ride in rows:
        item = clean_ride_public(ride)
        owner = owners.get(ride["owner_id"])
        item["owner"] = embedded_owner(owner) if owner else None
        if score_ctx:
            item["match_score"] = _match_score(ride, **score_ctx)
        data.append(item)

    if sort_key == "score":
        data.sort(key=lambda x: x.get("match_score") if x.get("match_score") is not None
                  else -1, reverse=True)
        data = data[(page - 1) * limit:page * limit]

    return {
        "ok": True,
        "data": data,
        "page": page,
        "pages": max(((total + limit - 1) // limit), 1),
        "total": total,
    }


# ---------------------------------------------------------------- my rides
@bp.get("/mine")
@require_auth
def my_rides():
    _expire_past()
    db = get_db()
    scope = request.args.get("scope")
    now = _now()
    query = {"owner_id": g.user["_id"]}
    if scope == "upcoming":
        query["departure_at"] = {"$gte": now}
        query["status"] = {"$in": ["published", "active", "full"]}
    elif scope == "past":
        query["$or"] = [{"departure_at": {"$lt": now}},
                        {"status": {"$in": ["completed", "cancelled", "expired"]}}]
    rows = list(db.rides.find(query).sort("departure_at", -1).limit(100))
    return {"ok": True, "data": [_attach_driver(r, owner_view=True) for r in rows]}


# -------------------------------------------------------------- locations
@bp.get("/meta/locations")
@optional_auth
@rate_limit("default")
def popular_locations():
    db = get_db()
    labels = set()
    for ride in db.rides.find({}, {"_id": 0, "origin.label": 1, "destination.label": 1}).limit(500):
        for key in ("origin.label", "destination.label"):
            label = ride
            for part in key.split("."):
                if isinstance(label, dict):
                    label = label.get(part)
            if label:
                labels.add(label)
    return {"ok": True, "data": sorted(labels)}


# ------------------------------------------------------------------ detail
@bp.get("/<rid>")
@optional_auth
@rate_limit("default")
def ride_detail(rid):
    _expire_past()
    ride = get_db().rides.find_one({"_id": to_object_id(rid, "ride")})
    if not ride:
        raise APIError("Ride not found.", 404, code="not_found")

    is_owner = bool(g.user and str(ride["owner_id"]) == str(g.user["_id"]))
    show_plate = is_owner
    if not show_plate and g.user:
        # confirmed passengers need the plate to find the car on the day
        confirmed = get_db().bookings.find_one(
            {"ride_id": ride["_id"], "rider_id": g.user["_id"], "status": "confirmed"})
        show_plate = bool(confirmed)
    item = _attach_driver(ride, owner_view=is_owner, include_plate=show_plate)

    if is_owner:
        bookings = list(get_db().bookings.find({
            "ride_id": ride["_id"],
            "status": {"$in": ["confirmed", "pending_payment"]}}).sort("created_at", 1))
        rider_ids = {b["rider_id"] for b in bookings}
        riders = {u["_id"]: u for u in
                  get_db().users.find({"_id": {"$in": list(rider_ids)}})}
        item["passengers"] = [
            {"id": str(b["_id"]), "seats": b["seats"], "amount": b["amount"],
             "status": b["status"],
             "rider": embedded_owner(riders.get(b["rider_id"])) if riders.get(b["rider_id"]) else None}
            for b in bookings
        ]
    return {"ok": True, "ride": item}


# ------------------------------------------------------------------ update
@bp.patch("/<rid>")
@require_auth
@rate_limit("default")
def update_ride(rid):
    db = get_db()
    ride_id = to_object_id(rid, "ride")
    ride = db.rides.find_one({"_id": ride_id, "owner_id": g.user["_id"]})
    if not ride:
        raise APIError("Ride not found.", 404, code="not_found")
    if not can_transition_ride(ride["status"], ride["status"]):
        raise APIError("Ride is closed for edits.", 409, code="ride_not_active")
    if is_past(ride.get("departure_at"), _now()):
        raise APIError("Ride has already departed.", 409, code="ride_departed")

    data = body()
    fields = {"updated_at": _now()}
    if "fare_per_seat" in data:
        fields["fare_per_seat"] = as_int(data.get("fare_per_seat"), "fare_per_seat",
                                         minimum=1, maximum=100000)
    if "notes" in data:
        fields["notes"] = as_str(data.get("notes"), "notes", max_len=300) or ""
    if "seats_total" in data:
        new_total = as_int(data.get("seats_total"), "seats_total", minimum=1,
                           maximum=ride.get("vehicle", {}).get("seat_count", 12))
        booked = ride["seats_total"] - ride["seats_available"]
        if new_total < booked:
            raise APIError("Cannot reduce below the already-booked seats.", 422,
                           code="seats_booked", details={"fields": ["seats_total"]})
        diff = new_total - ride["seats_total"]
        fields["seats_total"] = new_total
        fields["seats_available"] = ride["seats_available"] + diff
    if "timezone" in data:
        tzname = valid_timezone(data.get("timezone"))
        departure_at = combine_local(
            ride.get("departure_date"),
            datetime.strptime(ride.get("departure_time"), "%H:%M").time(),
            tzname)
        if departure_at <= _now():
            raise APIError("Departure time cannot be in the past.", 422, code="past_departure")
        _check_overlap(db, ride["vehicle_id"], departure_at,
                       ride.get("duration_minutes", 60), exclude_ride_id=ride_id)
        fields["timezone"] = tzname
        fields["departure_at"] = departure_at

    db.rides.update_one({"_id": ride_id}, {"$set": fields})
    updated = db.rides.find_one({"_id": ride_id})
    return {"ok": True, "ride": _attach_driver(updated, owner_view=True)}


# ------------------------------------------------------------- trip lifecycle
# Driver-asserted trip phases. The server maps the coarse request to the exact
# next canonical ride state; the client can never set a status directly.
_LIFECYCLE_TARGETS = {
    "start": RIDE_DRIVER_EN_ROUTE,
    "en_route": RIDE_DRIVER_EN_ROUTE,
    "boarding": RIDE_BOARDING,
    "depart": RIDE_IN_PROGRESS,
    "in_progress": RIDE_IN_PROGRESS,
    "complete": RIDE_COMPLETED,
}


@bp.post("/<rid>/status")
@require_auth
@rate_limit("strict")
def update_ride_status(rid):
    """Advance a ride through its trip lifecycle.

    Only the driver may move a ride, and only to the single next legal state --
    `can_transition_ride` is the authority, so an out-of-order or skipped step
    (e.g. publishing straight to completed) is refused rather than trusted.

    Completing the trip is the event that converts money from "collected" to
    "earned": every confirmed booking is closed out and its driver payable is
    released for settlement. It is idempotent, and the booking transition is
    conditional so two concurrent completes cannot double-close.
    """
    from .. import notifications

    db = get_db()
    ride_id = to_object_id(rid, "ride")
    ride = db.rides.find_one({"_id": ride_id})
    if not ride:
        raise APIError("Ride not found.", 404, code="not_found")
    if ride.get("owner_id") != g.user["_id"]:
        raise APIError("Only the driver can update this ride.", 403, code="forbidden")

    data = body()
    phase = as_str(data.get("status") or data.get("phase"), "status", max_len=20)
    target = _LIFECYCLE_TARGETS.get((phase or "").lower())
    if not target:
        raise APIError("Unknown trip status.", 422, code="validation_error",
                       details={"allowed": sorted(set(_LIFECYCLE_TARGETS))})

    # A completed ride is terminal: repeating it is a no-op, not an error. The
    # response mirrors the first call's shape exactly, reporting the same
    # awaiting/payable split, so a client that retries on a flaky network sees
    # identical data rather than a differently-shaped "already done".
    if ride["status"] == RIDE_COMPLETED and target == RIDE_COMPLETED:
        from .. import completion as completion_mod

        awaiting = [str(b["_id"]) for b in db.bookings.find(
            {"ride_id": ride_id, "status": BOOKING_AWAITING_COMPLETION})]
        return {"ok": True, "ride": _attach_driver(db.rides.find_one({"_id": ride_id}),
                                                   owner_view=True),
                "completed_bookings": len(awaiting), "booking_ids": awaiting,
                "awaiting_confirmation": awaiting,
                "payable_now": [
                    str(b["_id"]) for b in db.bookings.find(
                        {"ride_id": ride_id, "status": BOOKING_COMPLETED})
                ],
                "completion_window_minutes": completion_mod.confirm_window_minutes(),
                "idempotent": True}

    if not can_transition_ride(ride["status"], target):
        raise APIError(ride_transition_error(ride["status"]), 409, code="invalid_transition",
                       details={"from": ride["status"], "to": target})

    # Starting or boarding before departure time is refused; a late driver can
    # still be marked en route (that is exactly the real-world case for it).
    now = _now()
    if target in (RIDE_IN_PROGRESS,) and is_past(ride.get("departure_at"), now) is False:
        raise APIError("This ride has not departed yet.", 409, code="ride_not_departed")

    fields = {"status": target, "updated_at": now}
    if target == RIDE_COMPLETED:
        fields["completed_at"] = now

    updated = db.rides.find_one_and_update(
        {"_id": ride_id, "status": ride["status"]},
        {"$set": fields})
    if updated is None:
        # a concurrent request already moved the ride; report where it landed
        raise APIError(ride_transition_error(ride["status"]), 409, code="invalid_transition")

    closed = []
    if target == RIDE_COMPLETED:
        # Opening the confirmation window, NOT closing the booking. The fare was
        # collected, but the trip is not finished until the passenger says so
        # (or the window lapses) -- see completion.py. A no-show already closed
        # by the driver keeps its terminal state, and a concurrent refund wins.
        from .. import completion

        closed = completion.driver_completed(ride_id, g.user["_id"], now=now)
        for bid in closed:
            booking = db.bookings.find_one({"_id": to_object_id(bid, "booking")})
            if not booking:
                continue
            deadline = booking.get("completion_deadline")
            when = iso_utc(deadline) if deadline else "shortly"
            notifications.notify(
                booking.get("rider_id"), "Confirm your trip",
                "Your driver marked the trip finished. Please confirm it to release "
                f"their payout, or it happens automatically {when}.")
            notifications.notify(
                booking.get("owner_id"), "Trip finished",
                f"Ride finished. {booking.get('seats', 1)} seat(s) will be payable "
                "once your passenger confirms the trip.")

    fresh = db.rides.find_one({"_id": ride_id})
    from .. import completion as completion_mod

    return {"ok": True, "ride": _attach_driver(fresh, owner_view=True),
            "completed_bookings": len(closed), "booking_ids": closed,
            # Named to preserve the existing response shape: these bookings are
            # the ones whose completion the driver's tap advanced. They are NOT
            # settled yet -- `awaiting_confirmation` says so explicitly so a
            # client cannot read this field as "money has moved".
            "awaiting_confirmation": closed,
            "payable_now": [
                str(b["_id"]) for b in db.bookings.find(
                    {"ride_id": ride_id, "status": BOOKING_COMPLETED})
            ],
            "completion_window_minutes": completion_mod.confirm_window_minutes()}


@bp.post("/<rid>/bookings/<bid>/no-show")
@require_auth
@rate_limit("strict")
def mark_no_show(rid, bid):
    """Driver marks a confirmed rider as a no-show for this trip.

    The seat is released and the collected fare is refunded in full, because the
    service was not provided. A no-show is terminal, so it can never be undone
    into a completed trip (or double-refunded).

    Ordering matters: the booking is only allowed to become a terminal no-show
    once the refund has actually succeeded. If the gateway rejects the refund we
    roll the booking and the seat back, so the driver can simply retry -- the
    alternative (refund first, then close) would let a booking be marked
    no-show with the rider's money still held, and no endpoint can recover it
    because the idempotent branch above returns early.
    """
    from ..payments import refund_payment

    db = get_db()
    ride_id = to_object_id(rid, "ride")
    ride = db.rides.find_one({"_id": ride_id})
    if not ride:
        raise APIError("Ride not found.", 404, code="not_found")
    if ride.get("owner_id") != g.user["_id"]:
        raise APIError("Only the driver can mark a no-show.", 403, code="forbidden")

    booking_id = to_object_id(bid, "booking")
    booking = db.bookings.find_one({"_id": booking_id, "ride_id": ride_id})
    if not booking:
        raise APIError("Booking not found on this ride.", 404, code="not_found")
    if booking["status"] == BOOKING_NO_SHOW:
        return {"ok": True, "booking": clean_booking(booking), "idempotent": True}

    claimed = db.bookings.find_one_and_update(
        {"_id": booking_id, "status": BOOKING_CONFIRMED},
        {"$set": {"status": BOOKING_NO_SHOW, "no_show_at": _now(), "updated_at": _now()}})
    if claimed is None:
        raise APIError("Only a confirmed booking can be marked as a no-show.", 409,
                       code="state_conflict")

    _release_seats(db, ride_id, booking["seats"], booking["amount"])
    payment = db.payments.find_one({"booking_id": booking_id, "status": "success"})
    refund = None
    try:
        if payment:
            refund = refund_payment(payment, "Rider did not show up for the ride")
    except Exception:  # noqa: BLE001 - the refund is the part that can fail
        _unrelease_seats(db, ride, booking)
        db.bookings.update_one(
            {"_id": booking_id, "status": BOOKING_NO_SHOW},
            {"$set": {"status": BOOKING_CONFIRMED, "updated_at": _now()},
             "$unset": {"no_show_at": ""}})
        raise

    fresh = db.bookings.find_one({"_id": booking_id})
    out = clean_booking(fresh)
    if refund:
        out["refund_amount"] = refund.get("amount")
    return {"ok": True, "booking": out}


def _unrelease_seats(db, ride, booking):
    """Exact inverse of `_release_seats`, used to roll a no-show back when its
    refund could not be completed. Guarded so seats can never go negative."""
    db.rides.update_one(
        {"_id": ride["_id"], "seats_available": {"$gte": booking["seats"]}},
        [{"$set": {
            "seats_available": {"$subtract": ["$seats_available", booking["seats"]]},
            "earnings": {"$add": ["$earnings", booking["amount"]]},
            "updated_at": _now(),
        }}],
    )


# ------------------------------------------------------------------ cancel
@bp.delete("/<rid>")
@require_auth
@rate_limit("strict")
def cancel_ride(rid):
    """Cancel a ride: only while it is still in the future. Acknowledged
    bookings are cancelled and their successful payments refunded."""
    from ..payments import refund_payment

    db = get_db()
    ride_id = to_object_id(rid, "ride")
    ride = db.rides.find_one({"_id": ride_id, "owner_id": g.user["_id"]})
    if not ride:
        raise APIError("Ride not found.", 404, code="not_found")
    if ride["status"] in (RIDE_CANCELLED, RIDE_COMPLETED, RIDE_EXPIRED):
        raise APIError("Ride is already closed.", 409, code="ride_closed")
    if is_past(ride.get("departure_at"), _now()):
        raise APIError("Cannot cancel a ride that has already departed.", 409,
                       code="ride_departed")
    if not can_transition_ride(ride["status"], RIDE_CANCELLED):
        raise APIError("Ride cannot be cancelled in its current state.", 409,
                       code="ride_closed")

    now = _now()
    db.rides.update_one(
        {"_id": ride_id},
        {"$set": {"status": RIDE_CANCELLED, "updated_at": now,
                  "seats_available": ride["seats_total"]}},
    )

    cancelled_bookings = list(db.bookings.find({"ride_id": ride_id}))
    for b in cancelled_bookings:
        if b["status"] == "pending_payment":
            db.bookings.update_one({"_id": b["_id"]},
                                   {"$set": {"status": "cancelled", "cancelled_at": now,
                                             "updated_at": now}})
            continue
        db.bookings.update_one(
            {"_id": b["_id"]},
            {"$set": {"status": "refund_pending", "cancelled_at": now, "updated_at": now}},
        )
        payment = db.payments.find_one({"booking_id": b["_id"], "status": "success"})
        if payment:
            refund_payment(payment, "Ride cancelled by the driver",
                           amount=b.get("amount"))
            db.bookings.update_one({"_id": b["_id"]},
                                   {"$set": {"status": "refunded", "refunded": True,
                                             "cancelled_at": now, "updated_at": now}})

    return {"ok": True, "message": "Ride cancelled and bookings refunded."}