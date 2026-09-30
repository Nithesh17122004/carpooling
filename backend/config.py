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
    # Read from the environment like every other provider setting. This was
    # previously a hardcoded literal, which silently ignored PAYMENT_PROVIDER
    # from backend/.env and forced `razorpay` on every deployment -- including
    # local/test runs that had no keys, so order creation failed closed with
    # `payments_unconfigured`. `razorpay` stays the default because it is the
    # only provider allowed in production.
    PAYMENT_PROVIDER = _env("PAYMENT_PROVIDER", "razorpay")  # razorpay | demo
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

    # ------------------------------------------------ driver payout onboarding
    # The payout onboarding state machine lives in onboarding.py. Only the
    # provider's own opaque account identifier is ever persisted: no account
    # number, IFSC, UPI id, card or token is stored by this service.
    PAYOUT_ONBOARDING_REQUIRED = _env_bool("PAYOUT_ONBOARDING_REQUIRED", True)
    # When the provider marks onboarding `submitted` rather than instantly
    # `verified`, a human reviewer must approve it before settlement is allowed.
    PAYOUT_ONBOARDING_MANUAL_REVIEW = _env_bool("PAYOUT_ONBOARDING_MANUAL_REVIEW", True)
    # RazorpayX linked-account creation is normally done in the Razorpay
    # dashboard; when this is on, the service may create a Contact + Linked
    # Account through the API.
    RAZORPAYX_CONTACT_ID = _env("RAZORPAYX_CONTACT_ID", "")

    # ------------------------------------------------------ settlement worker
    # The worker runs as a SEPARATE Render background worker process, never
    # inside the web dyno (an in-web infinite loop starves request handling and
    # is killed on every deploy). `python -m backend.worker` is the entry point.
    SETTLEMENT_ENABLED = _env_bool("SETTLEMENT_ENABLED", True)
    SETTLEMENT_POLL_SECONDS = _env_int("SETTLEMENT_POLL_SECONDS", 20)
    SETTLEMENT_BATCH_SIZE = _env_int("SETTLEMENT_BATCH_SIZE", 25)
    # How long a worker may hold a claimed job before another worker may steal
    # it. A crashed worker's jobs therefore recover instead of stalling.
    SETTLEMENT_CLAIM_TTL_SECONDS = _env_int("SETTLEMENT_CLAIM_TTL_SECONDS", 300)
    SETTLEMENT_MAX_ATTEMPTS = _env_int("SETTLEMENT_MAX_ATTEMPTS", 5)
    SETTLEMENT_RETRY_BASE_SECONDS = _env_int("SETTLEMENT_RETRY_BASE_SECONDS", 30)
    # A completed ride is not paid out until the passenger has confirmed (or the
    # auto-confirm window has closed) AND this grace period has elapsed.
    SETTLEMENT_MIN_AGE_SECONDS = _env_int("SETTLEMENT_MIN_AGE_SECONDS", 0)
    # Dry run exercises the whole state machine with a provider stub that never
    # moves money. Hard-refused in production.
    PAYOUT_DRY_RUN = _env_bool("PAYOUT_DRY_RUN", False)

    # ------------------------------------------------ passenger trip completion
    # The driver says the trip finished; the passenger must confirm. Until they
    # do (or the timeout lapses) the booking's earnings are NOT payout-eligible.
    COMPLETION_CONFIRM_TIMEOUT_MINUTES = _env_int("COMPLETION_CONFIRM_TIMEOUT_MINUTES", 24 * 60)
    # Require a verified passenger identity before a booking may be created.
    PASSENGER_KYC_ENFORCE_BOOKING = _env_bool("PASSENGER_KYC_ENFORCE_BOOKING", False)

    # --------------------------------------------------------------- documents
    # Aadhaar / identity / RC documents are encrypted at rest with
    # DOCUMENT_ENCRYPTION_KEY (32 bytes, urlsafe-base64 or hex). When it is
    # absent, encryption is disabled and only available in non-production, so a
    # production deployment cannot quietly store identity papers in plaintext.
    DOCUMENT_ENCRYPTION_KEY = _env("DOCUMENT_ENCRYPTION_KEY", "")
    DOCUMENT_MAX_SIZE_MB = _env_int("DOCUMENT_MAX_SIZE_MB", 8)
    # Only these identity document types are accepted for KYC.
    KYC_IDENTITY_DOC_TYPES = ("aadhaar", "passport", "voter_id", "dl")

    # Publishing requires BOTH an approved vehicle and an approved driver
    # identity. Kept separate from KYC_ENFORCE_PUBLISH (the vehicle control) so
    # either can be relaxed in local development without disabling both.
    DRIVER_KYC_ENFORCE_PUBLISH = _env_bool("DRIVER_KYC_ENFORCE_PUBLISH", True)
    RC_VERIFY_ENFORCE_PUBLISH = _env_bool("RC_VERIFY_ENFORCE_PUBLISH", True)

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
    if app_config.get("PAYMENT_PROVIDER") not in ("razorpay", "demo"):
        problems.append("PAYMENT_PROVIDER must be 'razorpay' or 'demo'")
    if app_config.get("PAYMENT_PROVIDER") == "razorpay":
        if _placeholder(app_config.get("RAZORPAY_KEY_ID")):
            problems.append("RAZORPAY_KEY_ID")
        if _placeholder(app_config.get("RAZORPAY_KEY_SECRET")):
            problems.append("RAZORPAY_KEY_SECRET")
        if _placeholder(app_config.get("RAZORPAY_WEBHOOK_SECRET")):
            problems.append("RAZORPAY_WEBHOOK_SECRET")

    # Payout provider must be one this build actually implements. An unknown
    # value would otherwise fall through to the `manual` branch at dispatch
    # time, i.e. silently degrade to "a human sends the money".
    #
    # An *absent* key is not an error: `Config` supplies "manual" as the
    # default, so a caller passing a partial dict gets the safe branch anyway.
    # Only an explicitly-set unrecognised value is a misconfiguration.
    payout_provider = app_config.get("PAYOUT_PROVIDER")
    if payout_provider and payout_provider not in ("manual", "razorpayx", "dryrun"):
        problems.append("PAYOUT_PROVIDER must be 'manual', 'razorpayx' or 'dryrun'")
    if payout_provider == "razorpayx" and _placeholder(
            app_config.get("RAZORPAY_KEY_ID")):
        # Deliberately NOT RAZORPAY_X_ACCOUNT_NUMBER. The destination account is
        # per driver, taken from their verified onboarding record, so there is
        # no single platform account number to require here. A deployment that
        # still sets one is carrying a setting that no longer does anything --
        # which is a misconfiguration worth flagging, because it suggests the
        # code that used to pay everyone into it is still in someone's head.
        problems.append("RAZORPAY_KEY_ID is required for PAYOUT_PROVIDER=razorpayx")
    if payout_provider == "razorpayx" and app_config.get("RAZORPAY_X_ACCOUNT_NUMBER"):
        problems.append(
            "RAZORPAY_X_ACCOUNT_NUMBER is set but unused: payouts go to each "
            "driver's own verified linked account, never a shared one. Remove it.")

    # A dry-run provider must never reach production: it deliberately does not
    # move money, so leaving it on would make every "settled" payout a fiction.
    if app_config.get("PAYOUT_DRY_RUN"):
        problems.append("PAYOUT_DRY_RUN must be false (it never moves real money)")
    if payout_provider == "dryrun":
        problems.append("PAYOUT_PROVIDER=dryrun is not allowed in production")

    # Identity documents (Aadhaar/passport) and RC papers must be encrypted at
    # rest, and `documents.py` refuses to store them unencrypted outside
    # development. The boot-time half of that guarantee is required exactly when
    # a production deployment enforces a gate that stores a private document --
    # a deployment that turned those gates off is not asked for a key it will
    # never use, but one enforcing them cannot boot without one.
    stores_private_documents = bool(
        app_config.get("DRIVER_KYC_ENFORCE_PUBLISH")
        or app_config.get("RC_VERIFY_ENFORCE_PUBLISH")
    )
    if stores_private_documents and _placeholder(
            app_config.get("DOCUMENT_ENCRYPTION_KEY")):
        problems.append(
            "DOCUMENT_ENCRYPTION_KEY is required (32 bytes, base64/hex) when a "
            "private-document KYC gate is enforced")

    # Commission is a frozen invariant. MIN_PLATFORM_FEE must stay 0, otherwise
    # gross != fee + net for low fares and reconciliation breaks.
    if float(app_config.get("MIN_PLATFORM_FEE", 0) or 0) != 0.0:
        problems.append("MIN_PLATFORM_FEE must remain 0 (it breaks the 30% invariant)")
    if float(app_config.get("PLATFORM_FEE_PERCENT", 30) or 0) <= 0:
        problems.append("PLATFORM_FEE_PERCENT must be greater than 0")

    if app_config.get("SETTLEMENT_ENABLED") is False:
        problems.append(
            "SETTLEMENT_ENABLED must stay true in production; driver earnings are "
            "owed money and a deployment that never settles them is unlawful")

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
