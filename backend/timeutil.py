"""Timezone helpers.

Convention:
- BSON datetime has no timezone; PyMongo returns datetimes as naive-UTC.
- All timestamps are stored as UTC instants (naive, but UCT).
- `utc_now()` returns the canonical naive-UTC "now" used everywhere.
- `to_utc(value, tzname)` converts a local wall-clock (date, time or datetime)
  in `tzname` into a naive-UTC instant.
- `from_utc(value, tzname)` renders a stored UTC instant as local wall-clock.

BUSINESS LOGIC MUST ONLY USE naive-UTC values from these helpers so that
`naive <= naive` comparisons can never raise the classic
"can't compare offset-naive and offset-aware datetimes" error.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utc_now():
    """Canonical now: naive datetime representing the current UTC instant."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _zone(tzname):
    if not tzname:
        return timezone.utc
    try:
        return ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, TypeError):
        return timezone.utc


def to_utc(value, tzname="UTC"):
    """Interpret a date/datetime as wall-clock local time and return naive UTC."""
    zone = _zone(tzname)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    # date -> midnight local
    if hasattr(value, "hour") is False and hasattr(value, "time"):
        value = datetime.combine(value, datetime.min.time())
        return value.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    raise TypeError("to_utc expects a date or datetime")


def from_utc(value, tzname="UTC"):
    """Render a naive-UTC instant as wall-clock local time in `tzname`."""
    zone = _zone(tzname)
    if value is None:
        return None
    value = _as_naive_utc(value)
    return value.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)


def _as_naive_utc(value):
    """Normalize aware/naive datetimes and ISO strings to naive UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    return None


def iso_utc(value):
    """Serialize a naive-UTC instant with an explicit Z suffix."""
    value = _as_naive_utc(value)
    return value.isoformat() + "Z" if value else None


def is_past(value, now=None):
    """True when the UTC instant `value` is strictly before `now`."""
    value = _as_naive_utc(value)
    now = _as_naive_utc(now) or utc_now()
    if value is None:
        return False
    return value <= now


def combine_local(date_obj, time_obj, tzname="UTC"):
    """datetime.combine + local->UTC for a (date, time, tz) wall-clock pair."""
    return to_utc(datetime.combine(date_obj, time_obj), tzname)


def add_seconds(value, seconds):
    value = _as_naive_utc(value)
    return value + timedelta(seconds=seconds) if value else None