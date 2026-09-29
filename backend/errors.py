"""Structured API errors and Flask error handlers."""

from flask import jsonify


class APIError(Exception):
    """Raised anywhere; converted to a JSON error response by the handler."""

    def __init__(self, message, status=400, code="bad_request", details=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details

    def to_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


def register_error_handlers(app):
    @app.errorhandler(APIError)
    def handle_api_error(err):
        return jsonify({"ok": False, "error": err.to_dict()}), err.status

    # Framework-raised HTTP errors (abort(), missing args, auth aborts) must
    # keep the JSON contract instead of Flask's default HTML pages.
    @app.errorhandler(400)
    def handle_400(_err):
        return (
            jsonify(
                {"ok": False, "error": {"code": "bad_request", "message": "Bad request."}}
            ),
            400,
        )

    @app.errorhandler(401)
    def handle_401(_err):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {"code": "auth_required", "message": "Authentication required."},
                }
            ),
            401,
        )

    @app.errorhandler(403)
    def handle_403(_err):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {"code": "forbidden", "message": "You do not have permission."},
                }
            ),
            403,
        )

    @app.errorhandler(404)
    def handle_404(_err):
        return (
            jsonify(
                {"ok": False, "error": {"code": "not_found", "message": "Resource not found."}}
            ),
            404,
        )

    @app.errorhandler(405)
    def handle_405(_err):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {"code": "method_not_allowed", "message": "Method not allowed."},
                }
            ),
            405,
        )

    @app.errorhandler(422)
    def handle_422(_err):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {"code": "validation_error", "message": "Request could not be processed."},
                }
            ),
            422,
        )

    @app.errorhandler(429)
    def handle_429(_err):
        return (
            jsonify(
                {"ok": False, "error": {"code": "rate_limited", "message": "Too many requests."}}
            ),
            429,
        )

    @app.errorhandler(413)
    def handle_413(_err):
        return (
            jsonify(
                {"ok": False, "error": {"code": "file_too_large", "message": "File too large."}}
            ),
            413,
        )

    @app.errorhandler(Exception)
    def handle_500(err):
        app.logger.exception("Unhandled error: %s", err)
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "server_error",
                        "message": "Something went wrong on our side.",
                    },
                }
            ),
            500,
        )