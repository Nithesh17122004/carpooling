"""Context-appropriate serializers.

Principle: expose the minimum data each consumer needs.

- private_user(): the account owner (name/email/phone/age/gender/bio...).
- public_user(): anyone browsing the product (no email, phone, government
  numbers, auth tokens).
- driver_snapshot(): a driver's public profile for ride cards/bookings.
- admin_user(): full record for authorized admins/support (includes
  verification state, no credentials).
"""

from .timeutil import iso_utc

VERIFICATION_FIELDS = (
    "email_verified",
    "phone_verified",
    "driver_verified",
    "licence_verified",
    "insurance_verified",
    "role",
)


def _base(user):
    user = dict(user or {})
    return user


def private_user(user):
    user = _base(user)
    user.pop("password_hash", None)
    user.pop("verify_email_token", None)
    user.pop("verify_email_token_expires", None)
    user.pop("verify_phone_token", None)
    for f in ("refresh_tokens",):
        user.pop(f, None)
    user["id"] = str(user.get("_id", "")) if user.get("_id") else user.get("id")
    out = {
        "id": str(user.pop("_id")) if user.get("_id") else user.get("id"),
        "name": user.get("name"),
        "email": user.get("email"),
        "phone": user.get("phone") or "",
        "age": user.get("age"),
        "gender": user.get("gender") or "",
        "bio": user.get("bio") or "",
        "photo_url": user.get("photo_url") or "",
        "rating": user.get("rating", 0) or 0,
        "total_rides": user.get("total_rides", 0) or 0,
        "auth_provider": user.get("auth_provider", "local"),
        "created_at": iso_utc(user.get("created_at")),
        "last_login_at": iso_utc(user.get("last_login_at")),
    }
    for f in VERIFICATION_FIELDS:
        out[f] = user.get(f, False if f != "role" else "user")
    return out


def public_user(user):
    user = _base(user)
    out = {
        "id": str(user.get("_id")) if user.get("_id") else user.get("id"),
        "name": user.get("name"),
        "photo_url": user.get("photo_url") or "",
        "rating": user.get("rating", 0) or 0,
        "total_rides": user.get("total_rides", 0) or 0,
        "bio": user.get("bio") or "",
        "email_verified": bool(user.get("email_verified", False)),
        "driver_verified": bool(user.get("driver_verified", False)),
    }
    return out


def driver_snapshot(user):
    """Compact driver profile embedded in ride/search/booking payloads."""
    user = _base(user)
    if not user.get("_id") and not user.get("id"):
        return None
    return {
        "id": str(user.get("_id")) if user.get("_id") else user.get("id"),
        "name": user.get("name"),
        "photo_url": user.get("photo_url") or "",
        "rating": user.get("rating", 0) or 0,
        "total_rides": user.get("total_rides", 0) or 0,
        "driver_verified": bool(user.get("driver_verified", False)),
    }


def rider_snapshot(user):
    """Profile shared with the driver who is hosting the rider."""
    user = _base(user)
    if not user.get("_id") and not user.get("id"):
        return None
    return {
        "id": str(user.get("_id")) if user.get("_id") else user.get("id"),
        "name": user.get("name"),
        "photo_url": user.get("photo_url") or "",
        "rating": user.get("rating", 0) or 0,
        "age": user.get("age"),
        "gender": user.get("gender") or "",
    }


def admin_user(user):
    user = _base(user)
    user.pop("password_hash", None)
    user.pop("verify_email_token", None)
    user.pop("verify_email_token_expires", None)
    user.pop("verify_phone_token", None)
    user["id"] = str(user.pop("_id")) if user.get("_id") else user.get("id")
    if "created_at" in user:
        user["created_at"] = iso_utc(user["created_at"])
    if "updated_at" in user:
        user["updated_at"] = iso_utc(user["updated_at"])
    if "last_login_at" in user:
        user["last_login_at"] = iso_utc(user["last_login_at"])
    if "last_logout_at" in user:
        user["last_logout_at"] = iso_utc(user["last_logout_at"])
    return user