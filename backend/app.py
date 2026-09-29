"""RideMate API — Flask application factory and entry point.

Boot order: config -> logging -> Redis -> CORS -> error handlers -> blueprints
-> MongoDB -> routes -> frontend serving.
"""

import logging
import os
import re
import time
import uuid

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    request,
    send_file,
    send_from_directory,
)
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from .blueprints import ALL_BLUEPRINTS
from .config import Config, validate_runtime
from .errors import register_error_handlers
from . import db, payments, storage

APP_VERSION = "2.1.0"

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(self)",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' https://unpkg.com; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https: http://localhost:*; "
        "object-src 'none'"
    ),
}

_ID_RE = re.compile(r"[^0-9A-Za-z:_-]")


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


def _init_redis(app):
    url = app.config.get("REDIS_URL", "")

    if not url:
        app.extensions["rm_redis"] = None
        return

    try:
        import redis

        client = redis.Redis.from_url(
            url,
            socket_connect_timeout=2,
            retry_on_timeout=False,
        )

        client.ping()

        app.extensions["rm_redis"] = client

        app.logger.info(
            "Redis connected (realtime + rate limiting active)."
        )

    except Exception:  # noqa: BLE001
        app.extensions["rm_redis"] = None

        app.logger.warning(
            "Redis unavailable; falling back to in-memory rate limits."
        )


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)

    # ---------------------------------------------------------
    # Frontend directory
    #
    # Docker structure:
    #
    # /app/
    #   backend/
    #   frontend/
    #
    # Since Flask's root_path is /app/backend,
    # ../frontend resolves to /app/frontend.
    # ---------------------------------------------------------

    frontend_dir = os.path.abspath(
        os.path.join(app.root_path, "..", "frontend")
    )

    app.logger.info(
        "Frontend directory configured as: %s",
        frontend_dir,
    )

    # Fail fast on insecure production/staging configuration
    # BEFORE serving requests.
    validate_runtime(app.config)

    app.extensions["rm_redis"] = None

    # ---------------------------------------------------------
    # Proxy configuration
    # ---------------------------------------------------------

    proxy_count = app.config.get("TRUSTED_PROXY_COUNT", 0)

    if isinstance(proxy_count, int) and proxy_count > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=proxy_count,
            x_proto=1,
            x_host=1,
            x_port=1,
        )

    # ---------------------------------------------------------
    # Logging / Redis
    # ---------------------------------------------------------

    _configure_logging(app)
    _init_redis(app)

    # ---------------------------------------------------------
    # CORS
    # ---------------------------------------------------------

    CORS(
        app,
        resources={
            r"/api/*": {
                "origins": app.config["FRONTEND_ORIGINS"]
            }
        },
        supports_credentials=bool(
            app.config["ALLOW_CREDENTIALS"]
        ),
        expose_headers=["X-Request-Id"],
    )

    # ---------------------------------------------------------
    # Error handlers
    # ---------------------------------------------------------

    register_error_handlers(app)

    # ---------------------------------------------------------
    # Middleware
    # ---------------------------------------------------------

    @app.before_request
    def _assign_request_id():
        proposed = (
            request.headers.get(
                app.config["REQUEST_ID_HEADER"]
            )
            or uuid.uuid4().hex
        )

        # Sanitize client-supplied IDs.
        rid = _ID_RE.sub("", proposed)[:64] or uuid.uuid4().hex

        g.request_id = rid

    @app.before_request
    def _timer():
        g._t = time.time()

    @app.after_request
    def _headers_and_log(response):
        response.headers["X-Request-Id"] = getattr(
            g,
            "request_id",
            "",
        )

        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)

        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )

        if response.status_code == 429:
            response.headers["Retry-After"] = str(
                getattr(g, "rl_retry_after", 60)
            )

        if request.path.startswith("/api"):
            response.headers.setdefault(
                "Cache-Control",
                "no-store",
            )

        if request.path.startswith("/api/"):
            app.logger.info(
                "%s %s -> %s (%.1fms) uid=%s",
                request.method,
                request.path,
                response.status_code,
                (
                    time.time()
                    - g.get("_t", time.time())
                )
                * 1000,
                getattr(
                    getattr(g, "user", None),
                    "_id",
                    None,
                )
                or "-",
            )

        return response

    # ---------------------------------------------------------
    # API blueprints
    # ---------------------------------------------------------

    for blueprint in ALL_BLUEPRINTS:
        app.register_blueprint(blueprint)

    # ---------------------------------------------------------
    # Database
    # ---------------------------------------------------------

    db.init_db(app)

    # =========================================================
    # API PROBES
    # =========================================================

    @app.get("/api/health")
    def health():
        """Liveness endpoint."""

        db_ok = True

        try:
            db.get_db().command({"ping": 1})
        except Exception:  # noqa: BLE001
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

    @app.get("/api/ready")
    def ready():
        """Readiness endpoint."""

        checks = {
            "database": False,
            "storage": True,
        }

        code = 200

        # MongoDB
        try:
            db.get_db().command({"ping": 1})
            checks["database"] = True

        except Exception:  # noqa: BLE001
            code = 503

        # S3
        if app.config["STORAGE_BACKEND"] == "s3":
            client = storage._s3_client()

            if client is None:
                checks["storage"] = False
                code = 503

        # Redis
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

                except Exception:  # noqa: BLE001
                    checks["redis"] = False
                    code = 503

        return jsonify(
            {
                "ok": code == 200,
                "checks": checks,
            }
        ), code

    # =========================================================
    # FRONTEND
    # =========================================================

    @app.get("/")
    def index():
        """
        Serve the RideMate frontend.

        Before this change `/` returned API JSON.
        It now serves frontend/index.html.
        """

        index_file = os.path.join(
            frontend_dir,
            "index.html",
        )

        if not os.path.isfile(index_file):
            app.logger.error(
                "Frontend index.html not found: %s",
                index_file,
            )

            return jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "frontend_not_found",
                        "message": (
                            "Frontend index.html was not found."
                        ),
                    },
                }
            ), 500

        return send_from_directory(
            frontend_dir,
            "index.html",
        )

    # ---------------------------------------------------------
    # Frontend static files
    #
    # /css/styles.css
    # /js/app.js
    # /assets/...
    # ---------------------------------------------------------

    @app.get("/css/<path:filename>")
    def frontend_css(filename):
        return send_from_directory(
            os.path.join(frontend_dir, "css"),
            filename,
        )

    @app.get("/js/<path:filename>")
    def frontend_js(filename):
        return send_from_directory(
            os.path.join(frontend_dir, "js"),
            filename,
        )

    @app.get("/assets/<path:filename>")
    def frontend_assets(filename):
        return send_from_directory(
            os.path.join(frontend_dir, "assets"),
            filename,
        )

    # =========================================================
    # API INDEX
    # =========================================================

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

    # =========================================================
    # PAYMENTS WEBHOOK
    # =========================================================

    @app.route(
        app.config["PAYMENT_WEBHOOK_PATH"],
        methods=["POST"],
    )
    def payments_webhook():
        return payments.handle_webhook()

    # =========================================================
    # PAYOUTS WEBHOOK
    # =========================================================

    @app.route(
        "/api/payments/payout-webhook",
        methods=["POST"],
    )
    def payouts_webhook():
        return payments.handle_payout_webhook()

    # =========================================================
    # AVATAR UPLOAD
    # =========================================================

    @app.get("/api/uploads/avatar")
    def uploads_avatar():
        """S3-backed avatars are fetched by key."""

        key = (
            request.args.get("key") or ""
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
                    "public, max-age=31536000, immutable"
                )
            },
        )

    # =========================================================
    # UPLOADED AVATARS
    # =========================================================

    @app.route(
        "/uploads/<path:filename>",
        methods=["GET", "HEAD"],
    )
    def uploaded_file(filename):
        """
        Serves ONLY public avatars.

        Private vehicle documents are fetched through the
        authorized API endpoint.
        """

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

        return send_file(
            os.path.join(target, filename),
            conditional=True,
            max_age=31536000,
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=app.config["HOST"],
        port=app.config["PORT"],
        debug=app.config["DEBUG"],
    )
