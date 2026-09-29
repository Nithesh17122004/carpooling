"""Authentication endpoints: register, login, refresh, logout, me, password,
email verification, and Google OIDC (credential-verified, never client-trusted)."""

from flask import Blueprint, current_app, g, jsonify, request
from pymongo.errors import DuplicateKeyError

from .. import db as dbmodule
from ..db import get_db, utcnow
from ..errors import APIError
from ..google_oauth import get_google_provider
from ..ratelimit import rate_limit
from ..security import (
    hash_password,
    issue_access_token,
    issue_refresh_token,
    random_token,
    require_auth,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_password,
    password_strength,
)
from ..serializers import private_user
from ..validators import (
    GENDERS,
    as_int,
    as_str,
    body,
    require_fields,
    valid_email,
    valid_phone,
)

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def _refresh_cookie(jti):
    """(name, value, kwargs) for the refresh-token cookie."""
    cfg = current_app.config
    return (
        cfg["COOKIE_NAME"],
        jti,
        {
            "max_age": 3600 * 24 * cfg["JWT_REFRESH_TTL_DAYS"],
            "path": cfg["COOKIE_PATH"],
            "secure": cfg["COOKIE_SECURE"],
            "httponly": True,
            "samesite": "Lax",
            "domain": cfg["COOKIE_DOMAIN"],
        },
    )


def _session_user(user, extra_token={}):
    token = issue_access_token(user)
    jti = issue_refresh_token(user)
    name, value, kwargs = _refresh_cookie(jti)
    resp = jsonify({
        **extra_token,
        "ok": True,
        "user": private_user(user),
        "token": token,
        "refresh": False,
    })
    resp.set_cookie(name, value, **kwargs)
    return resp


def _clear_refresh_cookie(resp):
    cfg = current_app.config
    resp.delete_cookie(cfg["COOKIE_NAME"], path=cfg["COOKIE_PATH"], domain=cfg["COOKIE_DOMAIN"])
    return resp


def _create_user(name, email, password_hash, **extra):
    now = utcnow()
    doc = {
        "name": name,
        "email": email.lower().strip(),
        "password_hash": password_hash,
        "age": extra.get("age"),
        "phone": extra.get("phone"),
        "gender": extra.get("gender", ""),
        "bio": extra.get("bio", "") or "",
        "photo_url": "",
        "auth_provider": extra.get("auth_provider", "local"),
        "role": "user",
        "rating": 0.0,
        "total_rides": 0,
        "token_version": 0,
        "email_verified": bool(extra.get("email_verified", False)),
        "phone_verified": False,
        "driver_verified": False,
        "licence_verified": False,
        "insurance_verified": False,
        "created_at": now,
        "updated_at": now,
        "last_login_at": now,
        "last_logout_at": None,
    }
    if extra.get("google_id"):
        doc["google_id"] = extra["google_id"]
    try:
        result = get_db().users.insert_one(doc)
    except DuplicateKeyError:
        raise APIError("An account with this email already exists.", 409, code="email_taken")
    doc["_id"] = result.inserted_id
    return doc


def _touch_login(user_id, **extra):
    get_db().users.update_one({"_id": user_id},
                              {"$set": {"last_login_at": utcnow(), **extra}})


def _maybe_send_verification(user):
    """Issue a verify-email token unless verification is disabled or the email
    is already verified. Returns the token when REQUIRE_EMAIL_VERIFICATION is
    on (dev builds surface it via the response; production sends mail)."""
    if user.get("email_verified") or not current_app.config["REQUIRE_EMAIL_VERIFICATION"]:
        return None
    token = random_token(32)
    ttl_hours = current_app.config["EMAIL_VERIFY_TTL_HOURS"]
    get_db().users.update_one({"_id": user["_id"]}, {"$set": {
        "verify_email_token": token,
        "verify_email_token_expires": utcnow() + _ttl(ttl_hours),
    }})
    return token


# ---------------------------------------------------------------- register
@bp.post("/register")
@rate_limit("auth")
def register():
    data = body()
    require_fields(data, "name", "email", "password")
    name = as_str(data.get("name"), "name", max_len=80, required=True)
    email = valid_email(data.get("email"), required=True)
    password = as_str(data.get("password"), "password", max_len=200, required=True)
    if not password_strength(password):
        raise APIError(
            "Password must be at least 8 characters and include a letter and a number.",
            422, code="weak_password", details={"fields": ["password"]},
        )
    age = as_int(data.get("age"), "age", minimum=5, maximum=120)
    phone = valid_phone(data.get("phone"))
    gender = as_str(data.get("gender"), "gender", max_len=20)
    if gender and gender not in GENDERS:
        raise APIError("Invalid gender.", 422, code="validation_error", details={"fields": ["gender"]})

    user = _create_user(name, email, hash_password(password), age=age, phone=phone, gender=gender)
    dev_token = _maybe_send_verification(user)
    extra = {}
    if dev_token and current_app.config["ENV"] == "development":
        extra["dev_verify_token"] = dev_token
    return _session_user(user, extra_token=extra), 201


# ------------------------------------------------------------------ login
@bp.post("/login")
@rate_limit("auth")
def login():
    data = body()
    require_fields(data, "email", "password")
    email = valid_email(data.get("email"), required=True)
    password = as_str(data.get("password"), "password", max_len=200, required=True)

    user = get_db().users.find_one({"email": email})
    if user is None or not verify_password(password, user.get("password_hash", "")):
        # identical message for unknown email vs wrong password (anti-enumeration)
        raise APIError("Invalid email or password.", 401, code="invalid_credentials")

    if current_app.config["REQUIRE_EMAIL_VERIFICATION"] and not user.get("email_verified"):
        raise APIError(
            "Please verify your email address before signing in. Check your inbox.",
            403, code="email_not_verified",
        )
    _touch_login(user["_id"])
    return _session_user(user)


# -------------------------------------------------------------- providers
@bp.get("/providers")
@rate_limit("default")
def providers():
    """Which sign-in methods this deployment offers.

    The Google **client ID is public** (it identifies the app, not the user)
    and the browser needs it to render the Google Identity Services button.
    Publishing it here means the frontend can never run against a stale copy
    baked into HTML. The client SECRET is never exposed."""
    provider = get_google_provider()
    cfg = current_app.config
    return {
        "ok": True,
        "providers": {
            "password": True,
            "google": provider.configured(),
            "google_client_id": provider.client_id or "",
        },
        "flags": {
            "require_email_verification": bool(cfg.get("REQUIRE_EMAIL_VERIFICATION")),
            "demo_payments": cfg.get("PAYMENT_PROVIDER") == "demo",
        },
    }


# -------------------------------------------------------------- refresh
@bp.post("/refresh")
@rate_limit("strict")
def refresh():
    jti = request.cookies.get(current_app.config["COOKIE_NAME"])
    if not jti:
        raise APIError("No session found. Please log in.", 401, code="auth_required")

    row = get_db().refresh_tokens.find_one({"jti": jti})
    if not row:
        raise APIError("Session has ended. Please log in again.", 401, code="refresh_invalid")
    user = get_db().users.find_one({"_id": row["user_id"]})
    if not user:
        raise APIError("Session has ended. Please log in again.", 401, code="refresh_invalid")

    # rotate_refresh_token performs the revocation+replacement atomically and
    # independently detects expiry, user mismatch, and rotation-reuse.
    new_jti, family = rotate_refresh_token(jti, user)
    name, value, kwargs = _refresh_cookie(new_jti)
    resp = jsonify({
        "ok": True,
        "user": private_user(user),
        "token": issue_access_token(user),
        "refresh": True,
    })
    resp.set_cookie(name, value, **kwargs)
    return resp


# ---------------------------------------------------------------- logout
@bp.post("/logout")
@require_auth
def logout():
    jti = request.cookies.get(current_app.config["COOKIE_NAME"])
    if jti:
        revoke_refresh_token(jti)
    get_db().users.update_one({"_id": g.user["_id"]}, {"$set": {"last_logout_at": utcnow()}})
    resp = jsonify({"ok": True})
    return _clear_refresh_cookie(resp)


@bp.post("/logout-all")
@require_auth
def logout_all():
    revoke_all_refresh_tokens(g.user["_id"])
    get_db().users.update_one(
        {"_id": g.user["_id"]},
        {"$set": {"last_logout_at": utcnow(),
                  "token_version": (g.user.get("token_version", 0) or 0) + 1}},
    )
    resp = jsonify({"ok": True})
    return _clear_refresh_cookie(resp)


# ------------------------------------------------------------------- me
@bp.get("/me")
@require_auth
def me():
    return {"ok": True, "user": private_user(g.user)}


@bp.patch("/me")
@require_auth
def update_me():
    data = body()
    updates = {}
    if "name" in data:
        updates["name"] = as_str(data.get("name"), "name", max_len=80, required=True)
    if "age" in data:
        updates["age"] = as_int(data.get("age"), "age", minimum=5, maximum=120)
    if "phone" in data:
        updates["phone"] = valid_phone(data.get("phone"))
    if "gender" in data:
        gender = as_str(data.get("gender"), "gender", max_len=20)
        if gender and gender not in GENDERS:
            raise APIError("Invalid gender.", 422, code="validation_error", details={"fields": ["gender"]})
        updates["gender"] = gender
    if "bio" in data:
        updates["bio"] = as_str(data.get("bio"), "bio", max_len=300) or ""
    if not updates:
        raise APIError("Nothing to update.", 400, code="no_updates")

    updates["updated_at"] = utcnow()
    get_db().users.update_one({"_id": g.user["_id"]}, {"$set": updates})
    user = get_db().users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "user": private_user(user)}


# -------------------------------------------------------------- password
@bp.post("/password")
@require_auth
@rate_limit("strict")
def change_password():
    data = body()
    require_fields(data, "current_password", "new_password")
    current = as_str(data.get("current_password"), "current_password", max_len=200, required=True)
    new = as_str(data.get("new_password"), "new_password", max_len=200, required=True)
    if not verify_password(current, g.user.get("password_hash", "")):
        raise APIError("Current password is incorrect.", 401, code="wrong_password")
    if not password_strength(new):
        raise APIError(
            "Password must be at least 8 characters and include a letter and a number.",
            422, code="weak_password", details={"fields": ["new_password"]},
        )
    if current == new:
        raise APIError("New password must differ from the current one.", 422, code="same_password")

    # Change password -> revoke every session (including this one) and bump
    # token_version so all in-flight access tokens die immediately.
    revoke_all_refresh_tokens(g.user["_id"])
    next_ver = (g.user.get("token_version", 0) or 0) + 1
    get_db().users.update_one(
        {"_id": g.user["_id"]},
        {"$set": {"password_hash": hash_password(new), "updated_at": utcnow(),
                  "token_version": next_ver}},
    )
    resp = jsonify({"ok": True, "message": "Password updated. Please sign in again."})
    return _clear_refresh_cookie(resp)


# ------------------------------------------------------------- verify email
@bp.post("/verify-email/send")
@require_auth
@rate_limit("auth")
def send_verification():
    if g.user.get("email_verified"):
        return {"ok": True, "message": "Email already verified."}
    token = random_token(16)
    ttl_hours = current_app.config["EMAIL_VERIFY_TTL_HOURS"]
    get_db().users.update_one({"_id": g.user["_id"]}, {"$set": {
        "verify_email_token": token,
        "verify_email_token_expires": _ttl(ttl_hours),
    }})
    # NOTE: email delivery is a separate component; the token is returned in
    # the response ONLY for local development (no mail backend wired yet).
    return {"ok": True, "message": "Verification email sent.",
            "dev_token": token if current_app.config["ENV"] == "development" else None}


@bp.post("/verify-email/confirm")
@require_auth
@rate_limit("auth")
def confirm_verification():
    data = body()
    token = as_str(data.get("token"), "token", max_len=64, required=True)
    user = get_db().users.find_one({"_id": g.user["_id"]})
    if user.get("verify_email_token") != token:
        raise APIError("Verification link is invalid.", 422, code="invalid_verify_token")
    if (user.get("verify_email_token_expires") or _now()) < _now():
        raise APIError("Verification link has expired. Request a new one.", 422, code="verify_token_expired")
    get_db().users.update_one({"_id": user["_id"]}, {"$set": {
        "email_verified": True,
        "verify_email_token": None,
        "verify_email_token_expires": None,
        "updated_at": _now(),
    }})
    fresh = get_db().users.find_one({"_id": user["_id"]})
    return {"ok": True, "user": private_user(fresh)}


# ----------------------------------------------------------------- google
@bp.post("/google")
@rate_limit("auth")
def google_signin():
    """Exchange a Google ID token (credential) for a RideMate session.

    The credential is cryptographically verified against Google's JWKS; the
    email is extracted from the verified token, never taken from the client.
    """
    data = body()
    require_fields(data, "credential")
    content = as_str(data.get("credential"), "credential", min_len=8, max_len=12000, required=True)

    provider = get_google_provider()
    info = provider.verify_credential(content)

    user = get_db().users.find_one({"google_id": info["sub"]}) or \
        get_db().users.find_one({"email": info["email"]})
    if user is None:
        user = _create_user(
            info["name"],
            info["email"],
            hash_password(random_token(16)),
            auth_provider="google",
            google_id=info["sub"],
            email_verified=info["email_verified"],
            photo_url=info["picture"],
        )
    else:
        # Link google_id to an existing account so subsequent logins match.
        updates = {"last_login_at": utcnow()}
        if user.get("auth_provider") == "local" and not user.get("google_id"):
            updates["google_id"] = info["sub"]
            updates["email_verified"] = True if info["email_verified"] else user.get("email_verified", False)
        get_db().users.update_one({"_id": user["_id"]}, {"$set": updates})

    if current_app.config["REQUIRE_EMAIL_VERIFICATION"] and not user.get("email_verified") and not info["email_verified"]:
        raise APIError("Your Google email is not verified.", 403, code="email_not_verified")

    return _session_user(user)


def _ttl(hours):
    from datetime import timedelta

    return utcnow() + timedelta(hours=hours)


def _now():
    return utcnow()