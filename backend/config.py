"""Environment-driven configuration. Loads backend/.env if present.

SECURITY: secrets must only ever come from the environment / secret manager.
This module never hardcodes credentials, and errors fast in production when
required secrets are missing or still set to placeholder values.

Profiles:  Config (development default) / StagingConfig / ProductionConfig.
Production boots FAIL FAST on insecure or incomplete configuration and NEVER
enables Flask debug mode.
"""

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:  # pragma: no cover - dotenv optional
    pass

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _env(key, default=None):
    value = os.environ.get(key)
    return default if value is None or value == "" else value


def _env_bool(key, default=False):
    return str(_env(key, "1" if default else "0")).lower() in ("1", "true", "yes", "on")


def _env_int(key, default):
    try:
        return int(_env(key, str(default)) or str(default))
    except (TypeError, ValueError):
        return default


def _env_float(key, default):
    try:
        return float(_env(key, str(default)) or str(default))
    except (TypeError, ValueError):
        return default


class Config:
    """Development profile. Never use in production."""

    SECRET_KEY = _env("SECRET_KEY", "dev-secret-change-me")
    ENV = _env("FLASK_ENV", "development")
    # Flask 2.3+ uses `DEBUG`; the legacy `FLASK_DEBUG=1` is still honoured.
    DEBUG = _env_bool("DEBUG", _env("FLASK_DEBUG", "0") == "1")
    HOST = _env("HOST", "0.0.0.0")
    PORT = _env_int("PORT", 5000)
    PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "").rstrip("/")

    # JWT -- short-lived access tokens (rotation handled by refresh flow)
    JWT_SECRET = _env("JWT_SECRET", SECRET_KEY)
    JWT_ALGORITHM = "HS256"
    JWT_ACCESS_TTL_MINUTES = _env_int("JWT_ACCESS_TTL_MINUTES", 15)
    JWT_REFRESH_TTL_DAYS = _env_int("JWT_REFRESH_TTL_DAYS", 7)
    JWT_ISSUER = _env("JWT_ISSUER", "ridemate-api")

    # Cookie (refresh token) settings
    COOKIE_NAME = _env("REFRESH_COOKIE_NAME", "") or _env("COOKIE_NAME", "rm_refresh")
    # secure cookies are mandatory in production; dev over plain HTTP uses lax
    COOKIE_SECURE = _env_bool("REFRESH_COOKIE_SECURE", ENV == "production")
    COOKIE_DOMAIN = _env("REFRESH_COOKIE_DOMAIN", "") or None
    COOKIE_PATH = _env("REFRESH_COOKIE_PATH", "/api/auth")
    COOKIE_SAMESITE = _env("REFRESH_COOKIE_SAMESITE", "lax")

    # HTTPS proxy trust: >=1 to enable werkzeug ProxyFix (X-Forwarded-For etc).
    # With 0 (default) the client IP is the direct socket peer and spoofable
    # X-Forwarded-For headers are IGNORED -- so they cannot bypass rate limits.
    TRUSTED_PROXY_COUNT = _env_int("TRUSTED_PROXY_COUNT", 0)

    # MongoDB. MONGO_URI may itself be a comma-separated candidate list; an
    # explicit MONGO_URI_FALLBACKS adds further candidates tried in order.
    MONGO_URI = _env("MONGO_URI", None) or _env("MONGODB_URI", "mongodb://localhost:27017")
    MONGO_URI_FALLBACKS = [
        u.strip()
        for u in _env("MONGO_URI_FALLBACKS", "").split(",")
        if u.strip()
    ] + [u.strip() for u in MONGO_URI.split(",") if u.strip()]
    MONGO_DB_NAME = _env("MONGO_DB_NAME", "ridemate")
    MONGO_SERVER_SELECTION_TIMEOUT_MS = _env_int("MONGO_SERVER_SELECTION_TIMEOUT_MS", 3000)

    # CORS -- never a wildcard origin (enforced for production below)
    FRONTEND_ORIGINS = [
        o.strip()
        for o in _env(
            "FRONTEND_ORIGINS",
            "http://localhost:5000,http://localhost:5500,http://localhost:8000",
        ).split(",")
        if o.strip()
    ]
    ALLOW_CREDENTIALS = _env_bool("CORS_ALLOW_CREDENTIALS", True)

    # Uploads
    MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 8)
    MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024
    STORAGE_BACKEND = _env("STORAGE_BACKEND", "local")  # local | s3
    STORAGE_ACCESS_KEY = _env("STORAGE_ACCESS_KEY", "") or _env("AWS_ACCESS_KEY_ID", "")
    STORAGE_SECRET_KEY = _env("STORAGE_SECRET_KEY", "") or _env("AWS_SECRET_ACCESS_KEY", "")
    STORAGE_BUCKET = _env("STORAGE_BUCKET", "") or _env("AWS_BUCKET", "ridemate")
    STORAGE_REGION = _env("STORAGE_REGION", "") or _env("AWS_REGION", "")
    STORAGE_ENDPOINT = _env("STORAGE_ENDPOINT", "")  # S3-compatible custom endpoint

    # External providers
    MAPS_API_KEY = _env("MAPS_API_KEY", "") or _env("GOOGLE_MAPS_API_KEY", "")
    RAZORPAY_KEY_ID = _env("PAYMENT_KEY_ID", "") or _env("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET = _env("PAYMENT_KEY_SECRET", "") or _env("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET = _env("RAZORPAY_WEBHOOK_SECRET", "")
    PAYMENT_PROVIDER = _env("PAYMENT_PROVIDER", "demo")  # demo | razorpay
    PAYMENT_WEBHOOK_PATH = _env("PAYMENT_WEBHOOK_PATH", "/api/payments/webhook")
    # Payout provider: `manual` (staff settle, confirmed out of band) or
    # `razorpayx` (RazorpayX linked account). Either way only a provider
    # confirmation may mark a payout PAID.
    PAYOUT_PROVIDER = _env("PAYOUT_PROVIDER", "manual")  # manual | razorpayx
    RAZORPAY_X_ACCOUNT_NUMBER = _env("RAZORPAY_X_ACCOUNT_NUMBER", "")
    MAX_PAYOUT_AMOUNT = _env_float("MAX_PAYOUT_AMOUNT", 1000000.0)

    # Driver KYC gate. When enabled (the default) a vehicle must be verified
    # before its owner can publish a ride. Set to 0 only for local demo data.
    KYC_ENFORCE_PUBLISH = _env_bool("KYC_ENFORCE_PUBLISH", True)
    OSM_USER_AGENT = _env("OSM_USER_AGENT", "ridemate/1.0")
    OUTBOUND_TIMEOUT_SECONDS = _env_int("OUTBOUND_TIMEOUT_SECONDS", 6)

    # Google OAuth / OIDC
    GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_DISCOVERY_URL = _env(
        "GOOGLE_DISCOVERY_URL", "https://accounts.google.com/.well-known/openid-configuration"
    )
    GOOGLE_VERIFY_AUDIENCE = _env_bool("GOOGLE_VERIFY_AUDIENCE", True)

    # Redis (rate limiting + realtime pub/sub + live locations)
    REDIS_URL = _env("REDIS_URL", "")
    REDIS_NOTIFY_CHANNEL = _env("NOTIFY_REDIS_CHANNEL", "ridemate:events")

    # Rate limiting (per minute)
    RATE_LIMIT_DEFAULT = _env_int("RATE_LIMIT_DEFAULT", 120)
    RATE_LIMIT_AUTH = _env_int("RATE_LIMIT_AUTH", 12)
    RATE_LIMIT_STRICT = _env_int("RATE_LIMIT_STRICT", 30)

    # Platform economics (server-side only, never client-supplied).
    # Commission is exactly 30% of the gross booking amount. MIN_PLATFORM_FEE
    # must remain 0: any non-zero floor makes a low-fare booking deviate from
    # the 30% invariant and breaks gross == fee + net reconciliation.
    # The rate AND the computed rupee amount are frozen onto the payment
    # document at order creation, so later config changes can never rewrite an
    # already-settled transaction.
    PLATFORM_FEE_PERCENT = _env_int("PLATFORM_FEE_PERCENT", 30)
    MIN_PLATFORM_FEE = _env_int("MIN_PLATFORM_FEE", 0)

    # Payout reservation lock (seconds before a concurrent creation can retry)
    PAYOUT_LOCK_TTL_SECONDS = _env_int("PAYOUT_LOCK_TTL_SECONDS", 60)

    # Booking / rides
    RIDE_OVERLAP_BUFFER_MINUTES = _env_int("RIDE_OVERLAP_BUFFER_MINUTES", 30)
    RIDE_DEFAULT_DURATION_MINUTES = _env_int("RIDE_DEFAULT_DURATION_MINUTES", 60)
    RIDE_TIMEZONE = _env("RIDE_TIMEZONE", "Asia/Kolkata")
    SEATS_MAX_PER_BOOKING = _env_int("SEATS_MAX_PER_BOOKING", 12)
    CANCELLATION_CUTOFF_MINUTES = _env_int("CANCELLATION_CUTOFF_MINUTES", 60)
    LOCATION_HISTORY_TTL_HOURS = _env_int("LOCATION_HISTORY_TTL_HOURS", 7 * 24)
    PAYMENT_TTL_MINUTES = _env_int("PAYMENT_TTL_MINUTES", 15)

    # Verification
    REQUIRE_EMAIL_VERIFICATION = _env_bool("REQUIRE_EMAIL_VERIFICATION", False)
    EMAIL_VERIFY_TTL_HOURS = _env_int("EMAIL_VERIFY_TTL_HOURS", 24)

    # Observability
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")
    REQUEST_ID_HEADER = _env("REQUEST_ID_HEADER", "X-Request-Id")

    NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
    NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
    OSRM_ROUTE = _env("OSRM_ROUTE", "https://router.project-osrm.org/route/v1/driving")


class StagingConfig(Config):
    """Pre-production profile. Same secret hygiene as production."""

    ENV = "staging"
    DEBUG = False
    COOKIE_SECURE = _env_bool("REFRESH_COOKIE_SECURE", True)


class ProductionConfig(Config):
    """Production profile.

    - Debug mode can never be enabled.
    - Boot F A I L S (RuntimeError) on placeholder/empty secrets, the demo
      payment provider, insecure cookies/origins, or a demo Google config.
    """

    ENV = "production"
    DEBUG = False
    COOKIE_SECURE = True
    COOKIE_SAMESITE = "lax"


def validate_runtime(app_config) -> None:
    """Fail fast when the active config is insecure or incomplete.

    `app_config` is a Flask config (dict-like). Called from create_app() and
    intended to run for every profile; only production enforces the gate.
    This keeps debug-booting a production profile impossible and surfaces
    misconfiguration at boot instead of first request.
    """
    env = app_config.get("ENV", "development")
    if env not in ("production", "staging"):
        return

    # Debug mode is never acceptable outside development.
    if app_config.get("DEBUG"):
        raise RuntimeError(
            "FATAL: Flask DEBUG must be disabled for %s (got DEBUG=%r)." % (env, app_config["DEBUG"])
        )

    placeholders = ("change-me", "dev-secret", "changeme", "test-only-secret")
    problems = []

    def _placeholder(value):
        return (
            value is None
            or str(value).strip() == ""
            or any(p in str(value).lower() for p in placeholders)
        )

    if _placeholder(app_config.get("JWT_SECRET")):
        problems.append("JWT_SECRET")
    if _placeholder(app_config.get("SECRET_KEY")) and _placeholder(app_config.get("APP_SECRET")):
        problems.append("SECRET_KEY/APP_SECRET")
    if app_config.get("PAYMENT_PROVIDER") == "demo":
        problems.append("PAYMENT_PROVIDER=demo is not allowed (set PAYMENT_PROVIDER=razorpay)")
    if app_config.get("PAYMENT_PROVIDER") == "razorpay":
        if _placeholder(app_config.get("RAZORPAY_KEY_ID")):
            problems.append("RAZORPAY_KEY_ID")
        if _placeholder(app_config.get("RAZORPAY_KEY_SECRET")):
            problems.append("RAZORPAY_KEY_SECRET")
        if _placeholder(app_config.get("RAZORPAY_WEBHOOK_SECRET")):
            problems.append("RAZORPAY_WEBHOOK_SECRET")

    # MongoDB must never point at localhost/shared defaults in production.
    try:
        mongo_uris = app_config.get("MONGO_URI_FALLBACKS") or [app_config["MONGO_URI"]]
    except KeyError:
        mongo_uris = []
    if any(
        "localhost" in str(uri).lower() or "127.0.0.1" in str(uri) or "0.0.0.0" in str(uri)
        for uri in mongo_uris
    ):
        problems.append("MONGO_URI must not use localhost")
    if _placeholder(app_config.get("MONGO_DB_NAME")):
        problems.append("MONGO_DB_NAME")

    # Redis is mandatory in production (rate limiting + realtime fan-out).
    if _placeholder(app_config.get("REDIS_URL")):
        problems.append("REDIS_URL is required in production")

    if not app_config.get("COOKIE_SECURE"):
        problems.append("REFRESH_COOKIE_SECURE must be true")
    if app_config.get("GOOGLE_CLIENT_ID") and _placeholder(app_config.get("GOOGLE_CLIENT_ID")):
        problems.append("GOOGLE_CLIENT_ID")

    origins = [str(o) for o in (app_config.get("FRONTEND_ORIGINS") or [])]
    if any(o == "*" for o in origins):
        problems.append("FRONTEND_ORIGINS must not contain '*'")
    if any(o.startswith("http://") and not o.startswith("http://localhost") for o in origins):
        problems.append("FRONTEND_ORIGINS must use https outside localhost")

    if problems:
        raise RuntimeError("FATAL: Insecure/unconfigured %s settings: %s" % (env, "; ".join(problems)))


# Backwards-compatible module import-time gate driven by FLASK_ENV.
def _legacy_import_gate() -> None:
    try:
        validate_runtime(Config.__dict__)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if Config.ENV == "production":
    _legacy_import_gate()