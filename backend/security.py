"""Password hashing, JWT issue/decode, session (refresh) tokens, auth decorators.

Session model
-------------
- Short-lived ACCESS token (default 15 min) in the payload/memory of the SPA,
  sent as `Authorization: Bearer ...`. Includes `ver = user.token_version`.
  Bumping `token_version` (password change, logout-all) instantly invalidates
  every outstanding access token.
- Long-lived REFRESH token stored HttpOnly+Secure+SameSite in a cookie and as
  a row in `refresh_tokens` (jti, user_id, expires_at, revoked, replaced_by).
  Refresh tokens are rotated: each use revokes the old row and issues a new one.
- Logout revokes the presented refresh token (state-changing logout call) and
  a `logout_all` revokes every refresh token for the user + bumps token_version.
"""

import re
import secrets
import time
import uuid
from datetime import timedelta
from functools import wraps

import bcrypt
import jwt
from bson import ObjectId
from flask import current_app, g, request

from . import db as dbmodule
from .errors import APIError
from .timeutil import utc_now

_PASSWORD_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,}$")


def password_strength(raw):
    return bool(_PASSWORD_RE.match(raw or ""))


def hash_password(raw):
    hashed = bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=12))
    return hashed.decode("utf-8")


def verify_password(raw, hashed):
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------- access token
def _token_version(user):
    try:
        return user.get("token_version", 0) or 0
    except AttributeError:
        return 0


def issue_access_token(user, ttl_minutes=None):
    secret = current_app.config["JWT_SECRET"]
    ttl = ttl_minutes or current_app.config["JWT_ACCESS_TTL_MINUTES"]
    now = int(time.time())
    payload = {
        "sub": str(user["_id"]),
        "iat": now,
        "exp": now + int(ttl * 60),
        "jti": uuid.uuid4().hex,
        "typ": "access",
        "ver": _token_version(user),
        "iss": current_app.config["JWT_ISSUER"],
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token):
    secret = current_app.config["JWT_SECRET"]
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=current_app.config["JWT_ISSUER"],
            options={"require": ["exp", "iat", "sub", "typ", "ver"]},
        )
    except jwt.ExpiredSignatureError:
        raise APIError("Session expired. Please log in again.", 401, code="token_expired")
    except jwt.InvalidTokenError:
        raise APIError("Invalid session. Please log in again.", 401, code="token_invalid")


# -------------------------------------------------------------- refresh token
def _now():
    return utc_now()


def issue_refresh_token(user, family=None):
    jti = uuid.uuid4().hex
    doc = {
        "jti": jti,
        "user_id": user["_id"],
        "family": family or secrets.token_urlsafe(8),
        "expires_at": _now() + timedelta(days=current_app.config["JWT_REFRESH_TTL_DAYS"]),
        "revoked": False,
        "replaced_by": None,
        "created_at": _now(),
    }
    dbmodule.get_db().refresh_tokens.insert_one(doc)
    return jti


def revoke_refresh_token(jti):
    dbmodule.get_db().refresh_tokens.update_one(
        {"jti": jti}, {"$set": {"revoked": True, "revoked_at": _now()}}
    )


def revoke_all_refresh_tokens(user_id):
    dbmodule.get_db().refresh_tokens.update_many(
        {"user_id": user_id, "revoked": False}, {"$set": {"revoked": True, "revoked_at": _now()}}
    )


def rotate_refresh_token(jti, user):
    """Atomically rotate the presented refresh token and mint its replacement.

    The rotation is a single conditional update (`revoked: False`), so of two
    concurrent requests only ONE can rotate a given jti:
      * the winner revokes the old row, links `replaced_by`, and returns the new
        jti + family;
      * the loser finds the token already rotated (`replaced_by` set). That is
        classic token REUSE: the entire family is revoked and the request is
        rejected, so a stolen-but-rotated token can never mint another session.

    The replacement row is inserted before the claim rather than after it, so
    that the family sweep triggered by reuse cannot miss it -- see the comment
    on the insert below for why that ordering is load-bearing.

    Raises 401 on unknown/revoked/expired/mismatched tokens. Returns the new jti
    and family so the caller can re-issue the cookie + access token.
    """
    db = dbmodule.get_db()
    now = _now()
    new_jti = uuid.uuid4().hex

    # The replacement is written BEFORE the old row is claimed, and that order is
    # the whole point.
    #
    # The loser of a race detects reuse by observing `replaced_by` on the
    # presented row, and then revokes every unrevoked token in the family. If
    # the winner minted its replacement afterwards, that replacement does not
    # exist yet when the sweep runs and survives it -- leaving a live token on
    # exactly the reuse event that was supposed to kill the session. Writing it
    # first guarantees that anything which can see the claim can also see the
    # token, so the sweep cannot miss it.
    #
    # The family is read up front for the same reason: a family sweep filters on
    # `family`, so a row written with an empty family and patched afterwards is
    # invisible to the sweep for exactly as long as it matters.
    probe = db.refresh_tokens.find_one({"jti": jti})
    if probe is None:
        raise APIError("Session has ended. Please log in again.", 401, code="refresh_revoked")
    family = probe.get("family") or probe.get("jti")

    db.refresh_tokens.insert_one({
        "jti": new_jti,
        "user_id": user["_id"],
        "family": family,
        "expires_at": now + timedelta(days=current_app.config["JWT_REFRESH_TTL_DAYS"]),
        "revoked": False,
        "replaced_by": None,
        "created_at": now,
    })

    def _discard():
        db.refresh_tokens.delete_one({"jti": new_jti})

    old = db.refresh_tokens.find_one_and_update(
        {"jti": jti, "revoked": False},
        {"$set": {"revoked": True, "revoked_at": now, "replaced_by": new_jti}})
    if old is None:
        _discard()
        row = db.refresh_tokens.find_one({"jti": jti})
        if row and row.get("replaced_by"):
            # The presented token was ALREADY rotated -> reuse attempt.
            db.refresh_tokens.update_many(
                {"family": row.get("family") or row.get("jti"), "revoked": False},
                {"$set": {"revoked": True, "revoked_at": now}})
            raise APIError(
                "Session reused. All sessions for this account were signed out.",
                401, code="refresh_reused")
        if row and row.get("revoked"):
            raise APIError("Session has ended. Please log in again.", 401, code="refresh_revoked")
        raise APIError("Session has ended. Please log in again.", 401, code="refresh_revoked")

    try:
        # A family never changes after it is created, so the label the
        # replacement was written with has to be the one the sweep will use.
        if (old.get("family") or old.get("jti")) != family:
            raise APIError("Invalid session.", 401, code="token_invalid")
        if old.get("expires_at", now) <= now:
            raise APIError("Session has expired. Please log in again.", 401, code="refresh_expired")
        if str(old.get("user_id")) != str(user["_id"]):
            raise APIError("Invalid session.", 401, code="token_invalid")
    except APIError:
        # The claim is spent, so this branch must not leave a usable token
        # behind for a session we are refusing.
        _discard()
        raise

    return new_jti, family


# ------------------------------------------------------------------- decorators
def _extract_token():
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.headers.get("X-Auth-Token") or "").strip()


def _load_user():
    token = _extract_token()
    if not token:
        return None
    payload = decode_access_token(token)
    user = dbmodule.get_db().users.find_one({"_id": ObjectId(payload["sub"])})
    if user is None or _token_version(user) != payload.get("ver"):
        raise APIError("Session is no longer valid. Please log in again.", 401, code="token_invalid")
    return user


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _load_user()
        if user is None:
            raise APIError("Authentication required.", 401, code="auth_required")
        g.user = user
        return fn(*args, **kwargs)

    return wrapper


def optional_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        g.user = None
        try:
            g.user = _load_user()
        except APIError:
            g.user = None
        return fn(*args, **kwargs)

    return wrapper


ADMIN_ROLES = ("admin", "support")


def require_role(*roles):
    """Authorization check server-side; roles are never accepted from the client."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = _load_user()
            if user is None:
                raise APIError("Authentication required.", 401, code="auth_required")
            if user.get("role", "user") not in roles:
                raise APIError("You do not have permission to do this.", 403, code="forbidden")
            g.user = user
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def require_admin(fn):
    return require_role("admin", "support")(fn)


def current_user_role():
    return getattr(g, "user", None) and g.user.get("role", "user")


# ------------------------------------------------------------------- csrf token
def csrf_token_pair():
    """Per-session CSRF value. For cookie-authenticated endpoints only: the SPA
    must send the token back in an `X-CSRF-Token` header on mutations. Access
    token auth (Authorization header) is CSRF-immune and does not use this."""
    if not hasattr(g, "_csrf"):
        g._csrf = secrets.token_urlsafe(24)
    return g._csrf


def require_csrf(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("X-Requested-With") or request.headers.get("X-CSRF-Token")
        if current_app.config.get("ENV") != "development":
            if not header or not secrets.compare_digest(header, request.cookies.get("rm_csrf", "")):
                raise APIError("CSRF validation failed.", 403, code="csrf_failed")
        return fn(*args, **kwargs)

    return wrapper


# ------------------------------------------------------------------- misc
def random_token(nbytes=24):
    return secrets.token_hex(nbytes)


def safe_filename(filename):
    name, _, ext = (filename or "file").rpartition(".")
    ext = (ext or "").lower()[:8]
    safe_name = re.sub(r"[^a-z0-9_-]+", "", name.lower())[:32] or "file"
    key = secrets.token_urlsafe(12)
    return f"{safe_name}-{key}.{ext}" if ext else f"{safe_name}-{key}"


def random_object_key(prefix, ext=None):
    key = f"{prefix}/{utc_now():%Y/%m}/{secrets.token_urlsafe(14)}"
    if ext:
        key += f".{ext.lstrip('.')}"
    return key


def safe_compare(a, b):
    return secrets.compare_digest(str(a), str(b))