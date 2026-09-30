"""Structured, append-only audit records.

Every money-moving, identity-related or privileged action writes one row here.
The design rules:

* **Append-only.** Nothing in this module ever updates or deletes a record, so
  the log cannot be rewritten by the same code path it is meant to police.
* **No secrets, ever.** `record()` scrubs the metadata through `redact()` and
  refuses to store values that look like credentials. An audit trail that leaks
  a webhook secret is worse than no trail.
* **Stable shape.** `action` is a dotted `domain.action` string so an operator
  can query one family (`payout.*`) without knowing every member, and
  `index_action()` refuses names that would break that convention.
* **Never raises into the request path.** A logging failure must not roll back
  a payment that already succeeded, so a broken audit sink degrades to a logged
  warning instead of a 500.
"""

import re
from datetime import datetime, timezone

from .db import get_db, utcnow
from .timeutil import iso_utc

# Domains whose actions are worth keeping forever.
FINANCIAL = "financial"
IDENTITY = "identity"
ADMIN = "admin"
SECURITY = "security"

# The complete set, derived from the constants so the two cannot drift.
VALID_DOMAINS = frozenset({FINANCIAL, IDENTITY, ADMIN, SECURITY})

_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$")

# Keys whose values must never be persisted, whatever the caller passes.
_SECRET_KEYS = (
    "password", "passwd", "secret", "token", "authorization", "cookie",
    "api_key", "apikey", "access_key", "private_key", "client_secret",
    "signature", "webhook_secret", "key_secret", "jwt", "credential",
    "card_number", "cvv", "account_number", "ifsc", "upi_id", "upi",
    "aadhaar", "aadhaar_number", "pan", "document", "document_data", "body",
    "raw_document", "key", "encryption_key",
    # Connection strings. These are credential carriers that routinely arrive
    # under a harmless-looking key, and the username:password is the part that
    # must never be written down.
    "dsn", "connection_string", "database_url", "mongo_uri", "redis_url",
    "uri", "conn_str",
)

# Value shapes that are credentials regardless of the key they arrived under.
#
# The anchor is a negative lookbehind rather than \b, because the real Razorpay
# key format is a *compound* prefix -- rzp_live_XXXX / rzp_test_XXXX. With \b,
# neither `rzp_` (only "live" follows, under the {8,} minimum) nor `live_`
# (preceded by "_", which is a word character, so there is no boundary) matches,
# and a live key sits in an audit row in plain text.
_SECRET_VALUE_RE = re.compile(
    r"""(?<![A-Za-z0-9_])(?:
          rzp_(?:live|test)_[A-Za-z0-9]{8,}   # Razorpay key id / key secret
        | (?:rzp|live|test)_[A-Za-z0-9]{12,}   # a bare-prefixed variant
        | AKIA[0-9A-Z]{16}                     # AWS access key id
        | AIza[0-9A-Za-z_\-]{30,}              # Google API key
        | gh[pousr]_[A-Za-z0-9]{20,}           # GitHub token
        | xox[abopsr]-[A-Za-z0-9-]{10,}        # Slack token
        | sk_(?:live|test)_[A-Za-z0-9]{10,}    # OpenAI-style key
        | eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}  # JWT
        | [a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s:/@]{3,}@[^\s/]+  # DSN w/ password
        | -----BEGIN [A-Z ]*PRIVATE KEY-----
    )""",
    re.VERBOSE,
)

_REDACTED = "[redacted]"


def redact(value, _depth=0):
    """Recursively strip anything that looks like a credential.

    Two independent passes, because either alone is bypassable: a secret can be
    hidden under an innocuous key, or arrive under a secret-looking key holding
    a harmless value.
    """
    if _depth > 8:
        return _REDACTED
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _is_secret_key(key):
                out[key] = _REDACTED
            else:
                out[key] = redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return _REDACTED if _SECRET_VALUE_RE.search(value) else value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return iso_utc(value)
    return redact(str(value), _depth + 1)


def _is_secret_key(key):
    low = str(key).lower()
    return any(needle in low for needle in _SECRET_KEYS)


def index_action(action, domain=None):
    """Normalise and validate an action name.

    Raises `ValueError` on a malformed name so a typo becomes an immediate,
    obvious failure rather than an audit row nobody ever queries again.

    The domain prefix must be one of `VALID_DOMAINS`, not merely a prefix of
    the action: otherwise `payout.create` -- a name matching no real domain --
    is accepted and the row lands in the collection unqueryable by domain.
    """
    action = str(action or "").strip().lower()
    if not _ACTION_RE.match(action):
        raise ValueError(
            "audit action must look like 'domain.action' (got %r)" % (action,))
    head = action.split(".", 1)[0]
    if head not in VALID_DOMAINS:
        raise ValueError(
            "audit action %r must start with a known domain (%s)"
            % (action, ", ".join(sorted(VALID_DOMAINS))))
    if domain and not action.startswith(domain + "."):
        raise ValueError(
            "audit action %r does not belong to domain %r" % (action, domain))
    return action


def record(action, *, domain=None, actor_id=None, actor_role=None,
           target_type=None, target_id=None, outcome="ok", reason=None,
           request_id=None, meta=None):
    """Append one audit row. Never raises.

    Returns the stored document on success and `None` if the audit sink itself
    is unavailable, so a caller can log that separately without turning a
    completed payment into a 500.
    """
    try:
        index_action(action, domain)
    except ValueError as exc:
        # A malformed action is a programming error. Surface it loudly in the
        # logs but still store the row under a sanitized name so the event is
        # not silently lost.
        action = "audit.invalid_action"
        reason = str(exc)

    doc = {
        "action": action,
        "actor_id": str(actor_id) if actor_id is not None else None,
        "actor_role": actor_role,
        "target_type": target_type,
        "target_id": str(target_id) if target_id is not None else None,
        "outcome": outcome,
        "reason": redact(reason) if reason else None,
        "request_id": request_id,
        "meta": redact(meta or {}),
        "created_at": utcnow(),
    }
    try:
        get_db().audit_logs.insert_one(doc)
    except Exception:  # noqa: BLE001 - auditing must never break the request
        import logging

        logging.getLogger("backend.audit").warning(
            "audit sink unavailable for action=%s", action, exc_info=True)
        return None
    doc["_id"] = str(doc["_id"]) if "_id" in doc else None
    return doc


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
