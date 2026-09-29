"""Rate limiting: Redis-backed when configured, in-process memory fallback.

Usage:
    @bp.post('/login')
    @rate_limit('auth')
    def login(): ...

Bursts are tracked per key (typically client IP, optionally + user id). A
fixed-window counter with JSON continuation is fine for our protection goals.
"""

import threading
import time
from functools import wraps

from flask import current_app, g, request

from .errors import APIError

_MEM_LOCK = threading.Lock()
_MEM = {}  # key -> (bucket_ts, count)

# Default fixed window. Overridable per app so a long-lived deployment can widen
# it, and so tests are not sensitive to a minute boundary rolling over mid-test.
DEFAULT_WINDOW = 60  # seconds


def _window():
    return int(getattr(current_app, "config", {}).get(
        "RATE_LIMIT_WINDOW_SECONDS", DEFAULT_WINDOW) or DEFAULT_WINDOW)


def reset_memory_buckets():
    """Clear the in-process counters. Test-support only: Redis-backed counters
    are unaffected, so this is a no-op in a Redis-backed deployment."""
    with _MEM_LOCK:
        _MEM.clear()

# Bucket names tuned by config:  high-traffic noise vs. security-sensitive.
BUCKETS = {}


def _redis():
    return getattr(current_app, "extensions", {}).get("rm_redis")


def _bucket_limits():
    cfg = current_app.config
    return {
        "default": cfg["RATE_LIMIT_DEFAULT"],
        "auth": cfg["RATE_LIMIT_AUTH"],
        "strict": cfg["RATE_LIMIT_STRICT"],
    }


def _client_ip():
    """IP used for rate limiting.

    Only `request.remote_addr` is trusted. When a trusted reverse proxy is
    configured (TRUSTED_PROXY_COUNT>0) werkzeug's ProxyFix rewrites
    remote_addr from X-Forwarded-For; otherwise the raw header is IGNORED so
    clients cannot rotate it to bypass limits.
    """
    return request.remote_addr or "unknown"


def _bucket_end_seconds():
    now = int(time.time())
    window = _window()
    return int(window - (now % window)) or 1


def _try_redis(key, limit):
    client = _redis()
    if client is None:
        return None
    try:
        now = int(time.time())
        window = _window()
        bucket = int(now // window)
        rkey = f"rl:{key}:{bucket}"
        pipe = client.pipeline()
        pipe.incr(rkey)
        pipe.expire(rkey, window + 5)
        count, _ = pipe.execute()
        if int(count) > limit:
            return True
        return False
    except Exception:  # noqa: BLE001 - fall back to memory if Redis hiccups
        return None


def _try_memory(key, limit):
    now = int(time.time())
    bucket = int(now // _window())
    with _MEM_LOCK:
        # Bound the dictionary so spoofed/rotated keys cannot grow it forever.
        if len(_MEM) > 10000:
            cutoff = bucket - 2  # keep keys younger than two windows
            stale = [k for k, (ts, _c) in _MEM.items() if ts < cutoff]
            for k in stale:
                del _MEM[k]
        ts, count = _MEM.get(key, (bucket, 0))
        if ts != bucket:
            _MEM[key] = (bucket, 1)
            return False
        if count >= limit:
            return True
        _MEM[key] = (bucket, count + 1)
        return False


def _limited(key, limit):
    """Returns True when the caller is over the limit for `key`."""
    result = _try_redis(key, limit)
    if result is not None:
        return result
    return _try_memory(key, limit)


def rate_limit(bucket="default"):
    """Decorator enforcing a per-window request budget for an endpoint."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            limits = _bucket_limits()
            limit = limits.get(bucket, limits["default"])
            user_id = ""
            try:
                user = g.get("user")
                if user is not None:
                    user_id = str(user["_id"])
            except (AttributeError, KeyError, TypeError):
                pass
            key = f"{request.endpoint}:{_client_ip()}:{user_id}"
            if _limited(key, limit):
                g.rl_retry_after = _bucket_end_seconds()
                raise APIError(
                    "Too many requests. Please try again shortly.",
                    429,
                    code="rate_limited",
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator