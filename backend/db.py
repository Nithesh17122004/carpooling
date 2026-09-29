"""MongoDB connection, indexes, and serialization helpers."""

import re
from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId

from .errors import APIError
from .timeutil import utc_now, iso_utc
from .serializers import public_user  # re-export for backward compatibility

_db = None
_client = None
_active_uri = None


def _redact(uri: str) -> str:
    """Strip any embedded credentials so URIs are safe to log."""
    if "@" not in uri or "//" not in uri:
        return uri
    scheme, _, rest = uri.partition("//")
    _, _, host = rest.rpartition("@")
    return f"{scheme}//***@{host}"


def init_db(app):
    """Connect to the first reachable candidate in MONGO_URI_FALLBACKS."""
    global _db, _client, _active_uri
    from pymongo import MongoClient

    candidates = app.config.get("MONGO_URI_FALLBACKS") or [app.config["MONGO_URI"]]
    timeout = app.config["MONGO_SERVER_SELECTION_TIMEOUT_MS"]
    failures = []

    if _client is not None:
        try:
            _client.admin.command("ping")
            return _db
        except Exception:  # noqa: BLE001 - stale client, fall through to reconnect
            app.logger.info("Existing MongoDB client is stale; reconnecting.")

    for uri in candidates:
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=timeout)
            client.admin.command("ping")  # raises if unreachable
            _client, _db, _active_uri = client, client[app.config["MONGO_DB_NAME"]], uri
            create_indexes()
            app.logger.info("MongoDB connected: %s via %s", app.config["MONGO_DB_NAME"], _redact(uri))
            return _db
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            app.logger.warning("MongoDB candidate unreachable, trying next: %s (%s)", _redact(uri), exc)
            failures.append(f"{_redact(uri)}: {type(exc).__name__}")

    _db = None
    app.logger.error(
        "No MongoDB candidate reachable. Tried -> %s. API will report database=false.",
        " | ".join(failures) or "none configured",
    )
    return None


def active_uri() -> str | None:
    return _active_uri


def get_db():
    if _db is None:
        raise APIError("Database is unavailable.", 503, code="db_unavailable")
    return _db


def _unique_string_index(coll, field):
    """Unique index that applies only when the field holds a non-empty string.

    Legacy code stored `None` in these fields and used a `sparse` unique index;
    sparse still rejects duplicate explicit-null values (a `null` value IS an
    indexed value), which made e.g. a second booking on a ride fail with an
    E11000. This upgrades/recreates such indexes as partial-on-string.
    """
    name = f"{field}_1"
    try:
        info = coll.index_information()
    except Exception:  # noqa: BLE001 - collection may not exist yet
        info = {}
    idx = info.get(name) or {}
    key_spec = idx.get("key")
    if isinstance(key_spec, dict):
        key_spec = list(key_spec.items())
    keys = {(str(f).split(".")[0], k) for f, k in (key_spec or [])}
    if keys == {(field, 1)} and idx.get("unique") and "partialFilterExpression" in idx:
        return
    if idx:
        coll.drop_index(name)
    coll.create_index([(field, 1)], unique=True,
                      partialFilterExpression={field: {"$type": "string"}})


def _payment_booking_unique(coll):
    """At most ONE payment document per booking, enforced by the database.

    This index is what makes `create_order` safe. Order creation must claim the
    booking BEFORE calling the gateway, and that claim is only atomic if the
    database rejects the second writer. With a plain (non-unique) index two
    concurrent requests both "succeed", producing two payment rows for one
    booking and two live orders at the gateway.

    Legacy databases created before this constraint may already hold duplicates
    (the old code swallowed the insert error), so they are collapsed first --
    keeping the most advanced document, which is the one with real money
    movement behind it.
    """
    name = "booking_id_1"
    try:
        info = coll.index_information()
    except Exception:  # noqa: BLE001
        info = {}
    existing = info.get(name)
    if existing and existing.get("unique"):
        return

    # Keep the most advanced row per booking: success > any other status, then
    # the newest. Superseded rows are removed so the unique index can be built.
    pipeline = [
        {"$sort": {"status": -1, "created_at": -1}},
        {"$group": {"_id": "$booking_id", "keep": {"$first": "$_id"},
                    "drop": {"$push": "$_id"}}},
        {"$project": {"drop": 1, "_id": 0}},
    ]
    try:
        for group in coll.aggregate(pipeline, allowDiskUse=True):
            drop = [d for d in group.get("drop", []) if d != group["keep"]]
            if drop:
                coll.delete_many({"_id": {"$in": drop}})
    except Exception:  # noqa: BLE001 - no duplicates is the common case
        pass

    if existing:
        coll.drop_index(name)
    try:
        coll.create_index([("booking_id", 1)], unique=True)
    except Exception:  # noqa: BLE001 - concurrent index build; next start retries
        pass


_ACTIVE_BOOKING_STATES = ("pending_payment", "payment_failed", "confirmed")


def _booking_active_unique(coll):
    """A rider can hold at most ONE active (seat-holding) booking per ride.

    The index is partial on ride_id+rider_id: it only covers active states, so
    a rider may rebook after a cancellation/refund/completion but duplicate
    ACTIVE bookings (double-click, concurrent POSTs) are rejected atomically.
    This replaces the legacy permanent-unique (ride_id, rider_id) index which
    locked a rider out forever after a single cancel.
    """
    name = "ride_id_1_rider_id_1"
    try:
        info = coll.index_information()
    except Exception:  # noqa: BLE001
        info = {}
    idx = info.get(name) or {}
    key_spec = idx.get("key")
    if isinstance(key_spec, dict):
        key_spec = list(key_spec.items())
    keys = {(str(f).split(".")[0], k) for f, k in (key_spec or [])}
    expected_partial = {"status": {"$in": list(_ACTIVE_BOOKING_STATES)}}
    if keys == {("ride_id", 1), ("rider_id", 1)} and idx.get("unique") \
            and idx.get("partialFilterExpression") == expected_partial:
        return
    if idx:
        coll.drop_index(name)
    coll.create_index(
        [("ride_id", 1), ("rider_id", 1)], unique=True,
        partialFilterExpression={"status": {"$in": list(_ACTIVE_BOOKING_STATES)}})


def create_indexes():
    users = _db.users
    users.create_index("email", unique=True)
    users.create_index([("google_id", 1)], unique=True, sparse=True)
    users.create_index([("role", 1), ("created_at", -1)])

    vehicles = _db.vehicles
    vehicles.create_index([("user_id", 1), ("created_at", -1)])
    vehicles.create_index("vehicle_number", unique=True)
    vehicles.create_index([("user_id", 1), ("verification_status", 1)])

    rides = _db.rides
    rides.create_index([("owner_id", 1), ("departure_at", -1)])
    rides.create_index([("status", 1), ("departure_at", 1)])
    rides.create_index([("origin.label", 1), ("destination.label", 1), ("departure_date", 1)])
    rides.create_index([("vehicle_id", 1), ("status", 1), ("departure_at", 1)])
    try:
        # GeoJSON Point fields written by create_ride (origin_location /
        # destination_location). Legacy documents without them are excluded from
        # geo searches until migrated.
        rides.create_index([("origin_location", "2dsphere")])
        rides.create_index([("destination_location", "2dsphere")])
    except Exception:  # noqa: BLE001 - legacy db records may predate the fields
        pass

    bookings = _db.bookings
    bookings.create_index([("rider_id", 1), ("created_at", -1)])
    _booking_active_unique(bookings)
    bookings.create_index([("status", 1), ("ride_id", 1)])
    _unique_string_index(bookings, "idempotency_key")

    payments = _db.payments
    _unique_string_index(payments, "provider_reference")
    _unique_string_index(payments, "idempotency_key")
    _payment_booking_unique(payments)

    refunds = _db.refunds
    refunds.create_index([("payment_id", 1)])
    _unique_string_index(refunds, "provider_reference")
    _unique_string_index(refunds, "idempotency_key")
    try:
        # At most one live/processed refund per payment -> concurrent refund
        # claims are resolved atomically by the unique index (E11000).
        refunds.create_index(
            [("payment_id", 1)], unique=True,
            partialFilterExpression={"status": {"$in": ["processing", "processed"]}})
    except Exception:  # noqa: BLE001 - best-effort partial unique index
        pass

    webhook_events = _db.webhook_events
    try:
        webhook_events.create_index([("provider", 1), ("event_id", 1)], unique=True)
    except Exception:  # noqa: BLE001 - best-effort unique index
        pass
    webhook_events.create_index([("provider", 1), ("first_received_at", -1)])

    ledger_entries = _db.ledger_entries
    ledger_entries.create_index([("account_id", 1), ("created_at", 1)])
    ledger_entries.create_index([("booking_id", 1)])
    ledger_entries.create_index("entry_id", unique=True)

    payouts = _db.payouts
    payouts.create_index([("user_id", 1), ("created_at", -1)])
    _unique_string_index(payouts, "reference")

    refresh_tokens = _db.refresh_tokens
    refresh_tokens.create_index("jti", unique=True)
    refresh_tokens.create_index([("user_id", 1), ("revoked", 1)])
    refresh_tokens.create_index([("expires_at", 1)], expireAfterSeconds=0)

    notifications = _db.notifications
    notifications.create_index([("user_id", 1), ("created_at", -1)])
    notifications.create_index([("user_id", 1), ("read", 1)])

    notification_preferences = _db.notification_preferences
    notification_preferences.create_index([("user_id", 1)], unique=True)

    ratings = _db.ratings
    ratings.create_index("booking_id")
    ratings.create_index([("rated_user_id", 1), ("created_at", -1)])
    try:
        ratings.create_index(
            [("booking_id", 1), ("reviewer_id", 1), ("rated_user_id", 1)],
            unique=True,
            partialFilterExpression={"booking_id": {"$type": "objectId"}},
        )
    except Exception:  # noqa: BLE001 - best-effort partial index
        pass

    locations = _db.locations
    locations.create_index([("ride_id", 1), ("timestamp", 1)])
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            ttl_hours = current_app.config.get("LOCATION_HISTORY_TTL_HOURS", 24) or 0
        else:
            ttl_hours = 24
        if ttl_hours > 0:
            locations.create_index([("timestamp", 1)],
                                   expireAfterSeconds=int(ttl_hours * 3600))
    except Exception:  # noqa: BLE001 - index is best-effort
        pass

    audit_logs = _db.audit_logs
    audit_logs.create_index([("actor_id", 1), ("created_at", -1)])

    blocks = _db.blocks
    try:
        blocks.create_index([("blocker_id", 1), ("blocked_id", 1)], unique=True)
    except Exception:  # noqa: BLE001 - best-effort unique index
        pass

    reports = _db.reports
    reports.create_index([("target_user_id", 1), ("created_at", -1)])
    reports.create_index([("status", 1), ("created_at", -1)])
    audit_logs.create_index([("action", 1), ("created_at", -1)])


def to_object_id(raw, label="id"):
    try:
        return ObjectId(str(raw))
    except (InvalidId, TypeError):
        raise APIError(f"Invalid {label}.", 400, code="invalid_id")


def utcnow():
    """Back-compat alias. Canonical now = naive-UTC instant (see timeutil)."""
    return utc_now()


def clean(value):
    """Recursively convert ObjectId -> str and datetime -> ISO for JSON."""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return str(value)


def public_ride(ride):
    ride = dict(ride)
    return clean(ride)