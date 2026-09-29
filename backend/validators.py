"""Request parsing and field validation helpers."""

import re
from datetime import date, datetime

from flask import request

from .errors import APIError

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[1-9]\d{9,14}$")
PLATE_RE = re.compile(r"^[A-Z]{2}[ -]?(?:[0-9]{2}|[A-Z]{2})[ -]?[A-Z]{1,2}[ -]?[0-9]{4}$")
DL_RE = re.compile(r"^[A-Z]{2}\d{13}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

GENDERS = ("male", "female", "other")
VEHICLE_TYPES = ("2-wheeler", "4-wheeler", "auto", "bus")


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError("Expected a JSON object body.", 400, code="body_required")
    return data


def require_fields(data, *names):
    missing = [n for n in names if data.get(n) in (None, "")]
    if missing:
        raise APIError(
            "Missing required fields.",
            422,
            code="missing_fields",
            details={"fields": missing},
        )


def as_str(value, field, max_len=200, min_len=0, required=False):
    if value is None:
        if required:
            raise APIError(f"{field} is required.", 422, code="validation_error",
                           details={"fields": [field]})
        return None
    value = str(value).strip()
    if required and not value:
        raise APIError(f"{field} is required.", 422, code="validation_error",
                       details={"fields": [field]})
    if len(value) > max_len:
        raise APIError(f"{field} is too long.", 422, code="validation_error",
                       details={"fields": [field]})
    if len(value) < min_len:
        raise APIError(f"{field} is too short.", 422, code="validation_error",
                       details={"fields": [field]})
    return value


def as_int(value, field, minimum=None, maximum=None, required=False):
    if value in (None, ""):
        if required:
            raise APIError(f"{field} is required.", 422, code="validation_error",
                           details={"fields": [field]})
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise APIError(f"{field} must be a whole number.", 422, code="validation_error",
                       details={"fields": [field]})
    if minimum is not None and value < minimum:
        raise APIError(f"{field} must be at least {minimum}.", 422, code="validation_error",
                       details={"fields": [field]})
    if maximum is not None and value > maximum:
        raise APIError(f"{field} must be at most {maximum}.", 422, code="validation_error",
                       details={"fields": [field]})
    return value


def as_float(value, field, minimum=None, maximum=None, required=False):
    if value in (None, ""):
        if required:
            raise APIError(f"{field} is required.", 422, code="validation_error",
                           details={"fields": [field]})
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise APIError(f"{field} must be a number.", 422, code="validation_error",
                       details={"fields": [field]})
    if minimum is not None and value < minimum:
        raise APIError(f"{field} must be at least {minimum}.", 422, code="validation_error",
                       details={"fields": [field]})
    if maximum is not None and value > maximum:
        raise APIError(f"{field} must be at most {maximum}.", 422, code="validation_error",
                       details={"fields": [field]})
    return value


def as_bool(value, default=False):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def valid_email(value, required=False):
    value = as_str(value, "email", max_len=120)
    if not value:
        if required:
            raise APIError("Email is required.", 422, code="validation_error",
                           details={"fields": ["email"]})
        return None
    if not EMAIL_RE.match(value):
        raise APIError("Enter a valid email address.", 422, code="validation_error",
                       details={"fields": ["email"]})
    return value.lower()


def valid_phone(value, required=False):
    value = as_str(value, "phone", max_len=15)
    if not value:
        if required:
            raise APIError("Phone number is required.", 422, code="validation_error",
                           details={"fields": ["phone"]})
        return None
    if not PHONE_RE.match(value):
        raise APIError("Enter a valid phone number.", 422, code="validation_error",
                       details={"fields": ["phone"]})
    return value


def valid_date_str(value, required=False, field="date"):
    value = as_str(value, field, max_len=10)
    if not value:
        if required:
            raise APIError(f"{field} is required.", 422, code="validation_error",
                           details={"fields": [field]})
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise APIError(f"{field} must be a valid date (YYYY-MM-DD).", 422,
                       code="validation_error", details={"fields": [field]})


def valid_time_str(value, required=False, field="time"):
    value = as_str(value, field, max_len=5)
    if not value:
        if required:
            raise APIError(f"{field} is required.", 422, code="validation_error",
                           details={"fields": [field]})
        return None
    if not TIME_RE.match(value):
        raise APIError(f"{field} must be a valid 24-hour time (HH:MM).", 422,
                       code="validation_error", details={"fields": [field]})
    return value


def valid_timezone(value, default="Asia/Kolkata"):
    """Validate an IANA timezone name; falls back to the configured default."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    value = (value or "").strip()
    if not value:
        return default
    try:
        ZoneInfo(value)
        return value
    except (ZoneInfoNotFoundError, TypeError):
        raise APIError(f"Invalid timezone '{value}'.", 422, code="validation_error",
                       details={"fields": ["timezone"]})


def parse_point(value, required=False):
    """Accept {label, address?, lat, lng}."""

    if not isinstance(value, dict):
        if required:
            raise APIError("Location is required.", 422, code="validation_error",
                           details={"fields": ["origin"]})
        return None
    label = as_str(value.get("label"), "label", max_len=160, required=True)
    address = as_str(value.get("address"), "address", max_len=300) or label
    lat = as_float(value.get("lat"), "lat", minimum=-90, maximum=90, required=True)
    lng = as_float(value.get("lng"), "lng", minimum=-180, maximum=180, required=True)
    return {"label": label, "address": address, "lat": lat, "lng": lng}


def haversine_km(a, b):
    """Straight-line distance between two {lat,lng} points in km."""

    import math

    r = 6371.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp = math.radians(b["lat"] - a["lat"])
    dl = math.radians(b["lng"] - a["lng"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(r * 2 * math.asin(math.sqrt(h)), 1)