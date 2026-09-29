"""RideMate API — Flask application factory and entry point.

Boot order:
config -> logging -> Redis -> CORS -> error handlers -> blueprints
-> MongoDB -> routes.
"""

import logging
import os
import re
import time
import uuid

from flask import Flask, Response, g, jsonify, request, send_file
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from .blueprints import ALL_BLUEPRINTS
from .config import Config, validate_runtime
from .errors import register_error_handlers
from . import db, payments, storage


APP_VERSION = "2.1.0"


# ---------------------------------------------------------------------------
# Content Security Policy
#
# The frontend uses:
#   - Google Identity Services
#   - Leaflet from unpkg
#   - Google Fonts
#   - browser-side API calls
#
# Keep the policy explicit instead of using a completely open CSP.
# ---------------------------------------------------------------------------

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(self)",
    "Cross-Origin-Opener-Policy": "same-origin",

    # Required for the current frontend.
    #
    # unsafe-inline is currently required because index.html contains
    # a small inline configuration script.
    #
    # Once that inline script is moved to an external JS file, remove
    # 'unsafe-inline' from script-src.
    "Content-Security-Policy": (
        "default-src 'self'; "

        # Application JS + Google Sign-In + Leaflet
        "script-src 'self' 'unsafe-inline' "
        "https://unpkg.com "
        "https://accounts.google.com; "

        # Explicit script element policy
        "script-src-elem 'self' 'unsafe-inline' "
        "https://unpkg.com "
        "https://accounts.google.com; "

        # Application styles + Google Fonts + Leaflet CSS
        "style-src 'self' 'unsafe-inline' "
        "https://fonts.googleapis.com "
        "https://unpkg.com; "

        # Fonts
        "font-src 'self' data: "
        "https://fonts.gstatic.com; "

        # Images / avatars / Leaflet assets
        "img-src 'self' data: blob: https:; "

        # API/network requests
        "connect-src 'self' "
        "https://accounts.google.com "
        "https://*.googleapis.com; "

        # Google Sign-In may use an iframe
        "frame-src 'self' "
        "https://accounts.google.com; "

        # Leaflet may use workers/blob URLs
        "worker-src 'self' blob:; "

        # Do not allow plugins
        "object-src 'none'; "

        # Do not allow arbitrary framing
        "frame-ancestors 'none'; "

        # Forms should only submit to this application
        "form-action 'self' "
        "https://accounts.google.com; "

        # Upgrade insecure resource requests
        "upgrade-insecure-requests"
    ),
}


_ID_RE = re.compile(r"[^0-9A-Za-z:_-]")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _configure_logging(app):
    level = getattr(
        logging,
        app.config["LOG_LEVEL"].upper(),
        logging.INFO,
    )

    handler = logging.StreamHandler()

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        )
    )

    app.logger.handlers = [handler]
    app.logger.setLevel(level)

    # Quiet noisy third-party loggers.
    for noisy in ("werkzeug", "requests", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

def _init_redis(app):
    url = app.config.get("REDIS_URL", "")

    if not url:
        app.extensions["rm_redis"] = None
        app.logger.info(
            "Redis not configured; using in-memory fallback."
        )
        return

    try:
        import redis

        client = redis.Redis.from_url(
            url,
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=False,
            decode_responses=True,
        )

        client.ping()

        app.extensions["rm_redis"] = client

        app.logger.info(
            "Redis connected (realtime + rate limiting active)."
        )

    except Exception:
        app.extensions["rm_redis"] = None

        app.logger.warning(
            "Redis unavailable; falling back to in-memory rate limits."
        )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(config_object=Config):
    app = Flask(__name__)

    # Load configuration.
    app.config.from_object(config_object)

    # Always initialise the extension key.
    app.extensions["rm_redis"] = None

    # -----------------------------------------------------------------------
    # Production configuration validation
    # -----------------------------------------------------------------------

    validate_runtime(app.config)

    # -----------------------------------------------------------------------
    # Render / reverse proxy support
    # -----------------------------------------------------------------------

    proxy_count = app.config.get("TRUSTED_PROXY_COUNT", 0)

    if isinstance(proxy_count, int) and proxy_count > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=proxy_count,
            x_proto=1,
            x_host=1,
            x_port=1,
        )

    # -----------------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------------

    _configure_logging(app)

    # -----------------------------------------------------------------------
    # Redis
    # -----------------------------------------------------------------------

    _init_redis(app)

    # -----------------------------------------------------------------------
    # CORS
    # -----------------------------------------------------------------------

    frontend_origins = app.config.get(
        "FRONTEND_ORIGINS",
        [],
    )

    CORS(
        app,
        resources={
            r"/api/*": {
                "origins": frontend_origins
            }
        },
        supports_credentials=bool(
            app.config.get("ALLOW_CREDENTIALS", False)
        ),
        expose_headers=[
            "X-Request-Id"
        ],
    )

    # -----------------------------------------------------------------------
    # Error handlers
    # -----------------------------------------------------------------------

    register_error_handlers(app)

    # -----------------------------------------------------------------------
    # Request ID middleware
    # -----------------------------------------------------------------------

    @app.before_request
    def _assign_request_id():
        proposed = (
            request.headers.get(
                app.config["REQUEST_ID_HEADER"]
            )
            or uuid.uuid4().hex
        )

        # Sanitise client-supplied IDs so logs cannot be polluted.
        rid = _ID_RE.sub("", proposed)[:64]

        if not rid:
            rid = uuid.uuid4().hex

        g.request_id = rid

    # -----------------------------------------------------------------------
    # Request timer
    # -----------------------------------------------------------------------

    @app.before_request
    def _timer():
        g._t = time.time()

    # -----------------------------------------------------------------------
    # Security headers + request logging
    # -----------------------------------------------------------------------

    @app.after_request
    def _headers_and_log(response):
        response.headers["X-Request-Id"] = getattr(
            g,
            "request_id",
            "",
        )

        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)

        # HSTS only when request is actually HTTPS.
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )

        # Rate limit response.
        if response.status_code == 429:
            response.headers["Retry-After"] = str(
                getattr(
                    g,
                    "rl_retry_after",
                    60,
                )
            )

        # API responses should not be cached.
        if request.path.startswith("/api"):
            response.headers.setdefault(
                "Cache-Control",
                "no-store",
            )

        # Log API requests.
        if request.path.startswith("/api/"):
            elapsed_ms = (
                time.time()
                - g.get("_t", time.time())
            ) * 1000

            app.logger.info(
                "%s %s -> %s (%.1fms) uid=%s",
                request.method,
                request.path,
                response.status_code,
                elapsed_ms,
                getattr(
                    getattr(g, "user", None),
                    "_id",
                    None,
                )
                or "-",
            )

        return response

    # -----------------------------------------------------------------------
    # Register API blueprints
    # -----------------------------------------------------------------------

    for blueprint in ALL_BLUEPRINTS:
        app.register_blueprint(blueprint)

    # -----------------------------------------------------------------------
    # Database
    # -----------------------------------------------------------------------

    db.init_db(app)

    # -----------------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------------

    @app.get("/api/health")
    def health():
        """Liveness probe.

        The process is alive even if a dependency is temporarily unavailable.
        """

        db_ok = True

        try:
            db.get_db().command(
                {
                    "ping": 1
                }
            )
        except Exception:
            db_ok = False

        return {
            "ok": True,
            "version": APP_VERSION,
            "database": db_ok,
            "providers": {
                "maps": (
                    "google"
                    if app.config.get("MAPS_API_KEY")
                    else "osm"
                ),
                "payments": app.config.get(
                    "PAYMENT_PROVIDER",
                    "demo",
                ),
            },
        }

    # -----------------------------------------------------------------------
    # Readiness
    # -----------------------------------------------------------------------

    @app.get("/api/ready")
    def ready():
        """Readiness probe.

        Returns 503 when required dependencies are unavailable.
        """

        checks = {
            "database": False,
            "storage": True,
        }

        code = 200

        # Database.
        try:
            db.get_db().command(
                {
                    "ping": 1
                }
            )

            checks["database"] = True

        except Exception:
            code = 503

        # S3 storage.
        if app.config.get("STORAGE_BACKEND") == "s3":
            client = storage._s3_client()

            if client is None:
                checks["storage"] = False
                code = 503

        # Redis.
        if app.config.get("REDIS_URL"):
            redis_client = app.extensions.get(
                "rm_redis"
            )

            if redis_client is None:
                checks["redis"] = False
                code = 503

            else:
                try:
                    checks["redis"] = bool(
                        redis_client.ping()
                    )

                except Exception:
                    checks["redis"] = False
                    code = 503

        return jsonify(
            {
                "ok": code == 200,
                "checks": checks,
            }
        ), code

    # -----------------------------------------------------------------------
    # Root
    # -----------------------------------------------------------------------

    @app.get("/")
    def index():
        return {
            "ok": True,
            "service": "RideMate API",
            "version": APP_VERSION,
            "docs": "/api",
        }

    # -----------------------------------------------------------------------
    # API index
    # -----------------------------------------------------------------------

    @app.get("/api")
    def api_index():
        return {
            "ok": True,
            "service": "RideMate API",
            "version": APP_VERSION,
            "endpoint_guide": (
                "See HARDENING_REPORT.md / README "
                "for the route map."
            ),
        }

    # -----------------------------------------------------------------------
    # Payment webhook
    # -----------------------------------------------------------------------

    @app.route(
        app.config["PAYMENT_WEBHOOK_PATH"],
        methods=["POST"],
    )
    def payments_webhook():
        return payments.handle_webhook()

    # -----------------------------------------------------------------------
    # Payout webhook
    # -----------------------------------------------------------------------

    @app.route(
        "/api/payments/payout-webhook",
        methods=["POST"],
    )
    def payouts_webhook():
        return payments.handle_payout_webhook()

    # -----------------------------------------------------------------------
    # Avatar upload
    # -----------------------------------------------------------------------

    @app.get("/api/uploads/avatar")
    def uploads_avatar():
        """S3-backed avatars are fetched through this guarded route."""

        key = (
            request.args.get("key")
            or ""
        ).strip()

        if not key or not storage.is_public_avatar(key):
            return jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "not_found",
                        "message": "File not found.",
                    },
                }
            ), 404

        data, content_type, _name = storage.read_key(key)

        return Response(
            data,
            mimetype=content_type,
            headers={
                "Cache-Control": (
                    "public, "
                    "max-age=31536000, "
                    "immutable"
                )
            },
        )

    # -----------------------------------------------------------------------
    # Local uploaded avatar files
    # -----------------------------------------------------------------------

    @app.route(
        "/uploads/<path:filename>",
        methods=["GET", "HEAD"],
    )
    def uploaded_file(filename):
        """Serve only public avatars."""

        if not storage.is_public_avatar(filename):
            return jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "not_found",
                        "message": "File not found.",
                    },
                }
            ), 404

        target = app.config.get(
            "UPLOAD_DIR",
            storage.UPLOAD_DIR,
        )

        filepath = os.path.join(
            target,
            filename,
        )

        return send_file(
            filepath,
            conditional=True,
            max_age=31536000,
        )

    # -----------------------------------------------------------------------
    # Return Flask application
    # -----------------------------------------------------------------------

    return app


# ---------------------------------------------------------------------------
# WSGI application
# ---------------------------------------------------------------------------

app = create_app()


# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(
        host=app.config["HOST"],
        port=app.config["PORT"],
        debug=app.config["DEBUG"],
    )
