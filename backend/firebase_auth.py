"""Firebase Admin token verification for managed email-link sign-in.

Only Firebase ID tokens verified by the Admin SDK are accepted. The web
configuration is public; FIREBASE_SERVICE_ACCOUNT_JSON is a server-side secret.
"""
import json

from flask import current_app
from .errors import APIError


def firebase_is_configured(config):
    return bool(
        config.get("FIREBASE_WEB_API_KEY")
        and config.get("FIREBASE_AUTH_DOMAIN")
        and config.get("FIREBASE_PROJECT_ID")
        and config.get("FIREBASE_APP_ID")
        and config.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    )


def firebase_web_config(config):
    if not firebase_is_configured(config):
        return None
    return {
        "apiKey": config["FIREBASE_WEB_API_KEY"],
        "authDomain": config["FIREBASE_AUTH_DOMAIN"],
        "projectId": config["FIREBASE_PROJECT_ID"],
        "appId": config["FIREBASE_APP_ID"],
    }


def verify_firebase_id_token(raw_token):
    """Return trusted Firebase claims or a safe API error; never log the token."""
    cfg = current_app.config
    if not firebase_is_configured(cfg):
        raise APIError("Firebase email-link sign-in is not configured.", 503,
                       code="firebase_not_configured")
    try:
        import firebase_admin
        from firebase_admin import auth, credentials

        try:
            app = firebase_admin.get_app("ridemate")
        except ValueError:
            service_account = json.loads(cfg["FIREBASE_SERVICE_ACCOUNT_JSON"])
            app = firebase_admin.initialize_app(
                credentials.Certificate(service_account),
                {"projectId": cfg["FIREBASE_PROJECT_ID"]},
                name="ridemate",
            )
        claims = auth.verify_id_token(raw_token, app=app, check_revoked=True)
    except APIError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider errors must not expose token details
        current_app.logger.warning("Firebase ID token verification failed: %s", type(exc).__name__)
        raise APIError("Firebase sign-in link is invalid or expired. Request a new link.",
                       401, code="firebase_token_invalid") from exc

    if claims.get("aud") != cfg["FIREBASE_PROJECT_ID"] or claims.get("iss") != (
        "https://securetoken.google.com/" + cfg["FIREBASE_PROJECT_ID"]
    ):
        raise APIError("Firebase token belongs to a different project.", 401,
                       code="firebase_project_mismatch")
    if not claims.get("email") or not claims.get("email_verified"):
        raise APIError("Verify your email address with Firebase before continuing.",
                       403, code="firebase_email_unverified")
    if not claims.get("sub") or not isinstance(claims.get("sub"), str):
        raise APIError("Firebase token is missing a user identifier.", 401,
                       code="firebase_token_invalid")
    return claims
