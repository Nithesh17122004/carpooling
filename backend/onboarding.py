"""Driver payout onboarding.

A driver cannot be paid into an account the platform has never verified. This
module is the state machine that gets them there, and the gate that refuses
settlement until it is done.

    not_started --begin--> pending --submit--> submitted --verify--> verified
                                              |                       |
                                       reject  |                       | (suspend)
                                              v                       v
                                          rejected <--reject--    suspended
                                              |                       |
                                              +------- resume --------+

`suspended` is the state that matters operationally: a provider or an operator
can freeze a driver's payouts (fraud signal, account closed, a dispute) without
losing the onboarding they already completed, and resuming returns the driver to
`verified` rather than making them re-upload everything.

**What is stored.** Only opaque, provider-safe identifiers: the provider's
contact id and linked-account id, the last four of the account, and a
fingerprint. No account number, IFSC, UPI id, card detail, token or secret is
written to this service at any point -- those live only at the provider. The
`begin`/`submit` endpoints accept no banking data in the request body at all,
which makes it impossible to accidentally persist one.

**Manual review.** RazorpayX can leave a linked account in `submitted` rather
than activating it immediately. When `PAYOUT_ONBOARDING_MANUAL_REVIEW` is on
(normally), a human must approve it before settlement is allowed, so a
half-created account never becomes a payout destination.
"""

import secrets

from flask import current_app

from .db import get_db, utcnow
from .errors import APIError
from .timeutil import iso_utc

ONBOARDING_NOT_STARTED = "not_started"
ONBOARDING_PENDING = "pending"
ONBOARDING_SUBMITTED = "submitted"
ONBOARDING_VERIFIED = "verified"
ONBOARDING_REJECTED = "rejected"
ONBOARDING_SUSPENDED = "suspended"

ONBOARDING_STATES = {
    ONBOARDING_NOT_STARTED, ONBOARDING_PENDING, ONBOARDING_SUBMITTED,
    ONBOARDING_VERIFIED, ONBOARDING_REJECTED, ONBOARDING_SUSPENDED,
}

_TRANSITIONS = {
    ONBOARDING_NOT_STARTED: {ONBOARDING_PENDING},
    ONBOARDING_PENDING: {ONBOARDING_SUBMITTED, ONBOARDING_REJECTED},
    ONBOARDING_SUBMITTED: {ONBOARDING_VERIFIED, ONBOARDING_REJECTED, ONBOARDING_PENDING},
    ONBOARDING_VERIFIED: {ONBOARDING_SUSPENDED},
    ONBOARDING_REJECTED: {ONBOARDING_PENDING, ONBOARDING_SUSPENDED},
    ONBOARDING_SUSPENDED: {ONBOARDING_VERIFIED, ONBOARDING_REJECTED, ONBOARDING_PENDING},
}

# The only states in which money may leave the platform to this driver.
SETTLEMENT_ALLOWED_STATES = {ONBOARDING_VERIFIED}

_MAX_REASON = 300

# Fields a client is forbidden from sending. Enforced by `assert_no_banking_data`
# so a future frontend change cannot quietly start persisting an account number.
_FORBIDDEN_FIELDS = (
    "account_number", "account_no", "bank_account", "ifsc", "ifsc_code",
    "upi", "upi_id", "vpa", "card", "card_number", "cvv", "account_holder",
    "routing_number", "sort_code", "iban", "swift", "secret", "token",
    "api_key", "password", "auth_token", "signature",
)


def required():
    return bool(current_app.config.get("PAYOUT_ONBOARDING_REQUIRED", True))


def manual_review_required():
    return bool(current_app.config.get("PAYOUT_ONBOARDING_MANUAL_REVIEW", True))


def state_of(user):
    return (user or {}).get("payout_onboarding_status") or ONBOARDING_NOT_STARTED


def can_transition(source, target):
    return target in _TRANSITIONS.get(source, set())


def is_settled_ready(user):
    return state_of(user) in SETTLEMENT_ALLOWED_STATES


def assert_no_banking_data(payload):
    """Refuse a request that carries banking/secrets.

    This exists because the safest way to guarantee "we never store sensitive
    banking data" is to refuse to accept it in the first place, rather than to
    rely on every future caller remembering to drop the fields.
    """
    if not isinstance(payload, dict):
        return
    lowered = {str(k).lower() for k in payload}
    bad = sorted(lowered.intersection(_FORBIDDEN_FIELDS))
    if bad:
        raise APIError(
            "Banking details and secrets must not be sent to this service. "
            "They are held only by the payment provider.", 422,
            code="banking_data_rejected", details={"rejected_fields": bad})


def _assert_transition(user, target):
    source = state_of(user)
    if not can_transition(source, target):
        raise APIError(
            "Payout onboarding cannot move from '%s' to '%s'." % (source, target),
            409, code="onboarding_invalid_transition",
            details={"from": source, "to": target})


def _action_name(action):
    """`onboarding.begin` -> `financial.onboarding.begin`.

    The domain prefix is applied here rather than at each call site because
    `audit.index_action` rejects any action that does not start with its
    domain, and getting that wrong would silently rewrite every row to
    `audit.invalid_action` -- exactly the rows an operator needs to trust.

    The `onboarding.` segment is also added here, and an action that already
    carries it is left alone, so the prefix cannot end up doubled
    (`financial.onboarding.onboarding.begin`) by a call site that spells it out.
    """
    from . import audit

    action = str(action or "").strip().lower().lstrip(".")
    if action.startswith("onboarding."):
        return "%s.%s" % (audit.FINANCIAL, action)
    return "%s.onboarding.%s" % (audit.FINANCIAL, action)


def _record_action(user_id, action, actor_id, extra=None, reason=None, actor_role=None):
    """Append one audit row for an onboarding transition."""
    from . import audit

    audit.record(_action_name(action), domain=audit.FINANCIAL,
                 actor_id=actor_id, actor_role=actor_role, target_type="user",
                 target_id=user_id, meta=extra or {}, reason=reason)


# ------------------------------------------------------------------ lifecycle
def begin_onboarding(user_id, *, account_last4=None, provider=None, actor_id=None):
    """not_started|rejected -> pending. Opens the onboarding flow.

    `account_last4` is four digits for the driver's own reference only. It is
    never sufficient to move money and never leaves this service.
    """
    db = get_db()
    user = db.users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")
    _assert_transition(user, ONBOARDING_PENDING)

    last4 = "".join(ch for ch in str(account_last4 or "") if ch.isdigit())[-4:]
    now = utcnow()
    fields = {
        "payout_onboarding_status": ONBOARDING_PENDING,
        "payout_onboarding_provider": provider or current_app.config.get("PAYOUT_PROVIDER", "manual"),
        "payout_onboarding_account_last4": last4 or None,
        "payout_onboarding_reference": "ONB-" + secrets.token_hex(8).upper(),
        "payout_onboarding_started_at": now,
        "payout_onboarding_updated_at": now,
        "payout_onboarding_submitted_at": None,
        "payout_onboarding_verified_at": None,
        "payout_onboarding_reviewed_at": None,
        "payout_onboarding_reviewed_by": None,
        "payout_onboarding_reason": None,
    }
    db.users.update_one({"_id": user_id}, {"$set": fields})
    _record_action(user_id, "onboarding.begin", actor_id or user_id,
                   {"status": ONBOARDING_PENDING})
    return db.users.find_one({"_id": user_id})


def submit_onboarding(user_id, *, contact_id=None, linked_account_id=None,
                      provider_status=None, actor_id=None):
    """pending|rejected -> submitted. The provider accepted the details.

    `contact_id` / `linked_account_id` are the provider's own opaque ids. They
    are the only account identifiers this service ever stores.
    """
    db = get_db()
    user = db.users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")
    _assert_transition(user, ONBOARDING_SUBMITTED)

    now = utcnow()
    fields = {
        "payout_onboarding_status": ONBOARDING_SUBMITTED,
        "payout_onboarding_updated_at": now,
        "payout_onboarding_submitted_at": now,
        "payout_onboarding_reason": None,
    }
    if contact_id:
        fields["payout_onboarding_contact_id"] = str(contact_id)[:64]
    if linked_account_id:
        fields["payout_onboarding_account_id"] = str(linked_account_id)[:64]
    if provider_status:
        fields["payout_onboarding_provider_status"] = str(provider_status)[:40]

    db.users.update_one({"_id": user_id}, {"$set": fields})
    _record_action(user_id, "onboarding.submit", actor_id or user_id,
                   {"status": ONBOARDING_SUBMITTED,
                    "linked_account_id": fields.get("payout_onboarding_account_id")})
    return db.users.find_one({"_id": user_id})


def payout_account_of(user):
    """The verified linked account a payout for this driver may be sent to.

    Returns None unless onboarding is `verified` *and* the provider gave us an
    account id. Both halves are required: a driver verified by hand with no
    provider account has nowhere to send money, and a stale account id on a
    driver whose onboarding was revoked is worse than none.

    The result is the only value `payout_providers` is allowed to use as a
    destination. It is never combined with, or overridden by, a configured
    account number.
    """
    if not user:
        return None
    if user.get("payout_onboarding_status") != ONBOARDING_VERIFIED:
        return None
    account = str(user.get("payout_onboarding_account_id") or "").strip()
    return account or None


def verify_onboarding(user_id, reviewer_id, *, provider_status=None, actor_role=None):
    """submitted -> verified. The only path to a payout-eligible driver.

    When manual review is enabled the reviewer must be a real account id: the
    point of the control is that a person, not the submitter, authorises the
    destination.
    """
    db = get_db()
    user = db.users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")
    _assert_transition(user, ONBOARDING_VERIFIED)

    if manual_review_required() and not reviewer_id:
        raise APIError(
            "Payout onboarding requires a named reviewer before it can be verified.",
            422, code="onboarding_reviewer_required")

    now = utcnow()
    db.users.update_one({"_id": user_id}, {"$set": {
        "payout_onboarding_status": ONBOARDING_VERIFIED,
        "payout_onboarding_verified_at": now,
        "payout_onboarding_reviewed_at": now,
        "payout_onboarding_reviewed_by": reviewer_id,
        "payout_onboarding_updated_at": now,
        "payout_onboarding_reason": None,
        "payout_onboarding_provider_status": provider_status
        or user.get("payout_onboarding_provider_status"),
    }})
    _record_action(user_id, "onboarding.verify", reviewer_id,
                   {"status": ONBOARDING_VERIFIED}, actor_role=actor_role)
    return db.users.find_one({"_id": user_id})


def reject_onboarding(user_id, reviewer_id, reason, *, actor_role=None):
    """submitted|pending -> rejected. A reason is mandatory."""
    db = get_db()
    user = db.users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")
    if not (reason or "").strip():
        raise APIError("A reason is required when rejecting payout onboarding.", 422,
                       code="onboarding_reason_required")
    _assert_transition(user, ONBOARDING_REJECTED)

    now = utcnow()
    db.users.update_one({"_id": user_id}, {"$set": {
        "payout_onboarding_status": ONBOARDING_REJECTED,
        "payout_onboarding_reviewed_at": now,
        "payout_onboarding_reviewed_by": reviewer_id,
        "payout_onboarding_updated_at": now,
        "payout_onboarding_reason": str(reason).strip()[:_MAX_REASON],
    }})
    _record_action(user_id, "onboarding.reject", reviewer_id,
                   reason=str(reason)[:_MAX_REASON], actor_role=actor_role)
    return db.users.find_one({"_id": user_id})


def suspend_onboarding(user_id, actor_id, reason, *, actor_role=None):
    """verified -> suspended. Freezes payouts without losing the onboarding."""
    db = get_db()
    user = db.users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")
    if not (reason or "").strip():
        raise APIError("A reason is required when suspending payout onboarding.", 422,
                       code="onboarding_reason_required")
    _assert_transition(user, ONBOARDING_SUSPENDED)

    now = utcnow()
    db.users.update_one({"_id": user_id}, {"$set": {
        "payout_onboarding_status": ONBOARDING_SUSPENDED,
        "payout_onboarding_updated_at": now,
        "payout_onboarding_suspended_at": now,
        "payout_onboarding_reason": str(reason).strip()[:_MAX_REASON],
    }})
    _record_action(user_id, "onboarding.suspend", actor_id,
                   reason=str(reason)[:_MAX_REASON], actor_role=actor_role)
    return db.users.find_one({"_id": user_id})


def resume_onboarding(user_id, actor_id, *, actor_role=None):
    """suspended|rejected -> verified (from suspended) or pending (from rejected).

    Suspension is reversible without re-onboarding: the verified destination is
    still on file, so a resolved fraud hold should not cost the driver their
    payout setup.
    """
    db = get_db()
    user = db.users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")
    source = state_of(user)
    target = ONBOARDING_VERIFIED if source == ONBOARDING_SUSPENDED else ONBOARDING_PENDING
    _assert_transition(user, target)

    now = utcnow()
    db.users.update_one({"_id": user_id}, {"$set": {
        "payout_onboarding_status": target,
        "payout_onboarding_updated_at": now,
        "payout_onboarding_reason": None,
        "payout_onboarding_resumed_at": now,
    }})
    _record_action(user_id, "onboarding.resume", actor_id,
                   {"from": source, "to": target}, actor_role=actor_role)
    return db.users.find_one({"_id": user_id})


# ----------------------------------------------------------------------- gate
def assert_settlement_allowed(user, *, action="settle"):
    """Block settlement for a driver whose payout account is not verified.

    Called from payout creation and from the settlement worker. The messages
    differ per state so the driver knows whether to act, wait, or contact
    support.
    """
    if not required():
        return
    if is_settled_ready(user):
        return
    state = state_of(user)
    detail = {"payout_onboarding_status": state, "action": action}

    if state == ONBOARDING_NOT_STARTED:
        raise APIError(
            "Set up your payout account before you can be paid.", 403,
            code="payout_onboarding_required", details=detail)
    if state == ONBOARDING_PENDING:
        raise APIError(
            "Finish your payout account setup.", 403,
            code="payout_onboarding_incomplete", details=detail)
    if state == ONBOARDING_SUBMITTED:
        raise APIError(
            "Your payout account is being reviewed. Payouts start once it is "
            "approved.", 403, code="payout_onboarding_pending_review", details=detail)
    if state == ONBOARDING_REJECTED:
        raise APIError(
            "Your payout account setup was rejected. Please resubmit it.", 403,
            code="payout_onboarding_rejected",
            details={**detail, "reason": (user or {}).get("payout_onboarding_reason")})
    if state == ONBOARDING_SUSPENDED:
        raise APIError(
            "Your payouts are temporarily suspended. Contact support.", 403,
            code="payout_onboarding_suspended",
            details={**detail, "reason": (user or {}).get("payout_onboarding_reason")})
    raise APIError("Payouts are unavailable for this account.", 403,
                   code="payout_onboarding_required", details=detail)


# ---------------------------------------------------------------------- views
def onboarding_summary(user):
    """Driver-facing view. Contains no provider ids beyond the safe ones and
    never any banking data, because there is none to show."""
    user = user or {}
    state = state_of(user)
    return {
        "status": state,
        "provider": user.get("payout_onboarding_provider")
        or current_app.config.get("PAYOUT_PROVIDER", "manual"),
        "account_last4": user.get("payout_onboarding_account_last4"),
        "reference": user.get("payout_onboarding_reference"),
        "required": required(),
        "manual_review": manual_review_required(),
        # Exactly what the driver must do next.
        "can_begin": state in (ONBOARDING_NOT_STARTED, ONBOARDING_REJECTED),
        "can_submit": state == ONBOARDING_PENDING,
        "settlement_allowed": is_settled_ready(user),
        "reason": user.get("payout_onboarding_reason"),
        "started_at": iso_utc(user.get("payout_onboarding_started_at")),
        "submitted_at": iso_utc(user.get("payout_onboarding_submitted_at")),
        "verified_at": iso_utc(user.get("payout_onboarding_verified_at")),
        "reviewed_at": iso_utc(user.get("payout_onboarding_reviewed_at")),
        "updated_at": iso_utc(user.get("payout_onboarding_updated_at")),
    }


def admin_onboarding_view(user):
    """Reviewer view, including the provider ids needed to adjudicate."""
    user = user or {}
    return {
        "user_id": str(user["_id"]),
        "name": user.get("name"),
        "email": user.get("email"),
        "status": state_of(user),
        "provider": user.get("payout_onboarding_provider"),
        "provider_status": user.get("payout_onboarding_provider_status"),
        "contact_id": user.get("payout_onboarding_contact_id"),
        "linked_account_id": user.get("payout_onboarding_account_id"),
        "account_last4": user.get("payout_onboarding_account_last4"),
        "reference": user.get("payout_onboarding_reference"),
        "reviewer_id": str(user["payout_onboarding_reviewed_by"])
        if user.get("payout_onboarding_reviewed_by") else None,
        "reviewed_at": iso_utc(user.get("payout_onboarding_reviewed_at")),
        "submitted_at": iso_utc(user.get("payout_onboarding_submitted_at")),
        "reason": user.get("payout_onboarding_reason"),
        "started_at": iso_utc(user.get("payout_onboarding_started_at")),
    }
