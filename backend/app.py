"""RideMate API — Flask application factory and entry point.

Boot order: config -> logging -> Redis -> CORS -> error handlers -> blueprints
-> MongoDB (fail-fast candidates) -> routes.
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
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(self)",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; style-src 'unsafe-inline'",
}

_ID_RE = re.compile(r"[^0-9A-Za-z:_-]")


def _configure_logging(app):
    level = getattr(logging, app.config["LOG_LEVEL"].upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    app.logger.handlers = [handler]
    app.logger.setLevel(level)
    # quiet noisy third-party loggers
    for noisy in ("werkzeug", "requests", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _init_redis(app):
    url = app.config.get("REDIS_URL", "")
    if not url:
        app.extensions["rm_redis"] = None
        return
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=2, retry_on_timeout=False)
        client.ping()
        app.extensions["rm_redis"] = client
        app.logger.info("Redis connected (realtime + rate limiting active).")
    except Exception:  # noqa: BLE001 - app must still boot without Redis
        app.extensions["rm_redis"] = None
        app.logger.warning("Redis unavailable; falling back to in-memory rate limits.")


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)
    app.extensions["rm_redis"] = None

    # Fail fast on insecure production/staging configuration BEFORE serving.
    validate_runtime(app.config)

    # Trust X-Forwarded-* headers ONLY when a trusted proxy is configured.
    proxy_count = app.config.get("TRUSTED_PROXY_COUNT", 0)
    if isinstance(proxy_count, int) and proxy_count > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=proxy_count, x_proto=1, x_host=1, x_port=1
        )

    _configure_logging(app)
    _init_redis(app)

    CORS(
        app,
        resources={r"/api/*": {"origins": app.config["FRONTEND_ORIGINS"]}},
        supports_credentials=bool(app.config["ALLOW_CREDENTIALS"]),
        expose_headers=["X-Request-Id"],
    )

    register_error_handlers(app)

    # ----------------------------------------------------------- middleware
    @app.before_request
    def _assign_request_id():
        proposed = request.headers.get(app.config["REQUEST_ID_HEADER"]) or uuid.uuid4().hex
        # Sanitise client-supplied ids so logs cannot be polluted.
        rid = _ID_RE.sub("", proposed)[:64] or uuid.uuid4().hex
        g.request_id = rid

    @app.after_request
    def _headers_and_log(response):
        response.headers["X-Request-Id"] = getattr(g, "request_id", "")
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if response.status_code == 429:
            response.headers["Retry-After"] = str(getattr(g, "rl_retry_after", 60))
        if request.path.startswith("/api"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.path.startswith("/api/"):
            app.logger.info(
                "%s %s -> %s (%.1fms) uid=%s",
                request.method, request.path, response.status_code,
                (time.time() - g.get("_t", time.time())) * 1000,
                getattr(getattr(g, "user", None), "_id", None) or "-",
            )
        return response

    @app.before_request
    def _timer():
        g._t = time.time()

    for blueprint in ALL_BLUEPRINTS:
        app.register_blueprint(blueprint)

    db.init_db(app)

    # ------------------------------------------------------------- probes
    @app.get("/api/health")
    def health():
        """Liveness: the process is up. A failed DB is reported, not fatal."""
        db_ok = True
        try:
            db.get_db().command({"ping": 1})
        except Exception:  # noqa: BLE001 - report status only
            db_ok = False
        return {
            "ok": True,
            "version": APP_VERSION,
            "database": db_ok,
            "providers": {
                "maps": "google" if app.config.get("MAPS_API_KEY") else "osm",
                "payments": app.config.get("PAYMENT_PROVIDER", "demo"),
            },
        }

    @app.get("/api/ready")
    def ready():
        """Readiness: dependencies required to serve traffic are available."""
        checks = {"database": False, "storage": True}
        code = 200
        try:
            db.get_db().command({"ping": 1})
            checks["database"] = True
        except Exception:  # noqa: BLE001
            code = 503
        if app.config["STORAGE_BACKEND"] == "s3":
            client = storage._s3_client()
            if client is None:
                checks["storage"] = False
                code = 503
        if app.config.get("REDIS_URL"):
            redis_client = app.extensions.get("rm_redis")
            if redis_client is None:
                checks["redis"] = False
                code = 503
            else:
                try:
                    checks["redis"] = bool(redis_client.ping())
                except Exception:  # noqa: BLE001
                    checks["redis"] = False
                    code = 503
        return jsonify({"ok": code == 200, "checks": checks}), code

    @app.get("/")
    def index():
        return {
            "ok": True,
            "service": "RideMate API",
            "version": APP_VERSION,
            "docs": "/api",
        }

    @app.get("/api")
    def api_index():
        return {
            "ok": True,
            "service": "RideMate API",
            "version": APP_VERSION,
            "endpoint_guide": "See HARDENING_REPORT.md / README for the route map.",
        }

    # ------------------------------------------------- payments webhook
    @app.route(app.config["PAYMENT_WEBHOOK_PATH"], methods=["POST"])
    def payments_webhook():
        return payments.handle_webhook()

    # ------------------------------------------------- payouts webhook
    # Separate path and separate event stream: a payout confirmation must never
    # be confused with a payment capture, and each has its own dedup namespace.
    @app.route("/api/payments/payout-webhook", methods=["POST"])
    def payouts_webhook():
        return payments.handle_payout_webhook()

    # ------------------------------------------------- uploads (avatars only)
    @app.get("/api/uploads/avatar")
    def uploads_avatar():
        """S3-backed avatars are fetched by key through this guarded route."""
        key = (request.args.get("key") or "").strip()
        if not key or not storage.is_public_avatar(key):
            return jsonify({"ok": False,
                            "error": {"code": "not_found", "message": "File not found."}}), 404
        data, content_type, _name = storage.read_key(key)
        return Response(data, mimetype=content_type,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.route("/uploads/<path:filename>", methods=["GET", "HEAD"])
    def uploaded_file(filename):
        """Serves ONLY avatars. Private documents are fetched exclusively via
        the authorized /api/uploads/vehicle-doc endpoint."""
        if not storage.is_public_avatar(filename):
            return jsonify({"ok": False,
                            "error": {"code": "not_found", "message": "File not found."}}), 404
        target = app.config.get("UPLOAD_DIR", storage.UPLOAD_DIR)
        return send_file(os.path.join(target, filename),
                         conditional=True,
                         max_age=31536000)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host=app.config["HOST"], port=app.config["PORT"], debug=app.config["DEBUG"])