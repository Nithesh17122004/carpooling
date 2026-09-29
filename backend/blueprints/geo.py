"""Geocoding (Nominatim/OSM) and routing (OSRM) proxied through the server."""

import requests
from flask import Blueprint, current_app, request

from ..errors import APIError
from ..ratelimit import rate_limit
from ..security import require_auth

bp = Blueprint("geo", __name__, url_prefix="/api/geo")


def _headers():
    return {
        "User-Agent": current_app.config["OSM_USER_AGENT"],
        "Accept-Language": "en",
    }


def _timeout():
    return current_app.config["OUTBOUND_TIMEOUT_SECONDS"]


def route_distance_minutes(a, b):
    """Server-side (distance_km, duration_min) for two {lat,lng} points.

    Uses the configured OSRM service. Raises on any failure so callers
    can fall back to the haversine straight-line estimate.
    """
    service_url = current_app.config["OSRM_ROUTE"]
    resp = requests.get(
        f"{service_url}/{b['lng']},{b['lat']};{a['lng']},{a['lat']}",
        params={"overview": "false", "alternatives": "false"},
        headers=_headers(),
        timeout=_timeout(),
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("routes"):
        raise ValueError("no route")
    r = body["routes"][0]
    return round(r["distance"] / 1000, 1), round(r["duration"] / 60)


def _map_item(item):
    display = item.get("display_name") or item.get("name") or ""
    short = ", ".join(display.split(",")[:2]).strip()
    return {"label": short, "address": display, "lat": float(item["lat"]),
            "lng": float(item["lon"])}


@bp.get("/search")
@rate_limit("default")
def search():
    q = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit") or 6), 10)
    if not q:
        return {"ok": True, "data": []}
    try:
        resp = requests.get(
            current_app.config["NOMINATIM_SEARCH"],
            params={"q": q, "format": "json", "limit": limit, "addressdetails": 0},
            headers=_headers(),
            timeout=_timeout(),
        )
        resp.raise_for_status()
        return {"ok": True, "data": [_map_item(it) for it in resp.json()]}
    except Exception:  # noqa: BLE001 - geocoding is best-effort
        return {"ok": True, "data": []}


@bp.get("/reverse")
@rate_limit("default")
def reverse():
    lat = request.args.get("lat")
    lng = request.args.get("lng")
    if lat is None or lng is None:
        raise APIError("lat and lng are required.", 422, code="validation_error")
    try:
        resp = requests.get(
            current_app.config["NOMINATIM_REVERSE"],
            params={"lat": lat, "lon": lng, "format": "jsonv2"},
            headers=_headers(),
            timeout=_timeout(),
        )
        resp.raise_for_status()
        item = resp.json()
        return {"ok": True, "place": {"label": item.get("name") or item.get("display_name", ""),
                                      "address": item.get("display_name", ""),
                                      "lat": float(item.get("lat", lat)),
                                      "lng": float(item.get("lon", lng))}}
    except Exception:  # noqa: BLE001
        return {"ok": True, "place": None}


@bp.get("/route")
@rate_limit("default")
def route():
    """points=lat,lng,lat,lng (two waypoints) -> distance, duration, geometry."""

    points = (request.args.get("points") or "").strip().split(",")
    coords = []
    try:
        pts = [float(p) for p in points]
        coords = [(pts[i], pts[i + 1]) for i in range(0, len(pts), 2)]
    except (ValueError, IndexError):
        pass
    if len(coords) < 2:
        raise APIError("Provide at least two lat,lng points.", 422, code="validation_error")

    a, b = coords[0], coords[1]
    service_url = current_app.config["OSRM_ROUTE"]
    try:
        resp = requests.get(
            f"{service_url}/{b[1]},{b[0]};{a[1]},{a[0]}",
            params={"overview": "full", "geometries": "geojson"},
            headers=_headers(),
            timeout=_timeout(),
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("routes"):
            raise ValueError("no route")
        r = body["routes"][0]
        geom = r.get("geometry", {})
        raw_coords = geom.get("coordinates", []) if isinstance(geom, dict) else []
        route = [[lat, lon] for lon, lat in raw_coords]
        return {
            "ok": True,
            "distance_km": round(r["distance"] / 1000, 1),
            "duration_min": round(r["duration"] / 60),
            "route": route,
            "source": "osrm",
        }
    except Exception as exc:  # noqa: BLE001 - fallback to straight line
        current_app.logger.info("Routing fallback: %s", exc)
        distance_km = round(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 * 111.0, 1)
        return {
            "ok": True,
            "distance_km": distance_km,
            "duration_min": round(distance_km / 35 * 60),
            "route": [a, b],
            "source": "straight",
        }


@bp.get("/providers")
@require_auth
@rate_limit("default")
def providers():
    cfg = current_app.config
    return {
        "ok": True,
        "geocoding": "google" if cfg.get("GOOGLE_MAPS_API_KEY") else "osm",
        "routing": "osrm",
        "maps": "leaflet-osm",
        "payments": "razorpay" if cfg.get("RAZORPAY_KEY_ID") else "demo",
    }
