"""Payout providers: where a driver's money is actually sent.

The single most dangerous thing this file can do is send a driver's earnings to
an account that is not theirs. The previous implementation read one global
`RAZORPAY_X_ACCOUNT_NUMBER` from configuration and paid every driver into it. If
that account is a platform settlement account, no driver is ever paid; if it
belongs to a specific driver, every *other* driver's money is paid to them.

So a provider is only ever handed a destination that this module has resolved
from the driver's own onboarding record, and it refuses to send anything at all
when it cannot. There is deliberately no fallback to configuration.

Two providers exist:

`ManualPayoutProvider`
    Records the intent and moves nothing. Staff settle out of band and confirm
    with a bank reference. Useful in development and for TEST-mode runs, where
    there is no real money and no linked accounts.

`RazorpayXPayoutProvider`
    Calls RazorpayX. Requires the driver to hold a verified `account_id` from
    Razorpay's own linked-account onboarding flow. Passes an idempotency key so
    a retry after a timeout cannot produce a second transfer -- the classic
    double-payout.

Both classify their own failures, because the difference matters to the caller:

* a *transient* failure (timeout, 502, rate limit, or a driver who has not
  finished onboarding) must leave the payout retryable -- it is still pending,
  and the worker will try again once the cause clears;
* a *permanent* failure (the provider actively refused the transfer) must fail
  the payout, because retrying it forever hides a real problem.
"""

import uuid

from .errors import APIError


class PayoutProviderError(Exception):
    """A provider could not send the payout.

    `transient` decides whether the payout stays retryable. `reason` is a
    short, non-sensitive token safe to store on the payout document and safe to
    put in a log -- never a provider message, which can contain account
    details.
    """

    def __init__(self, reason, *, transient=False, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.transient = transient
        self.detail = detail or {}


class PayoutProvider:
    """Interface. `destination` is always a verified, driver-owned account."""

    name = "abstract"

    def destination_account_id(self, driver):
        raise NotImplementedError

    def send(self, payout, driver):
        raise NotImplementedError


class ManualPayoutProvider(PayoutProvider):
    """Records the intent to pay; moves no money.

    The payout stays in `processing` until a human confirms with a bank
    reference. Returning None for the reference is what marks it as awaiting
    manual settlement, and is why `confirm_payout` accepts a blank reference.
    """

    name = "manual"

    def destination_account_id(self, driver):
        return None

    def send(self, payout, driver):
        return None


class RazorpayXPayoutProvider(PayoutProvider):
    """Sends real money through RazorpayX to a driver-owned linked account."""

    name = "razorpayx"

    def destination_account_id(self, driver):
        """The driver's own verified linked account. Never a platform account.

        Raises when the driver has no verified account. There is deliberately
        no "or maybe the configured one" branch: a fallback here is precisely
        the bug this class exists to prevent.
        """
        from .onboarding import payout_account_of

        if not payout_account_of(driver):
            # Transient, not a rejection. The driver has not finished
            # onboarding yet, and the payout becomes sendable the moment they
            # do -- so it must stay retryable and the balance stay reserved.
            # Failing it would take a driver's earnings off the queue for a
            # reason that has nothing to do with the money.
            raise PayoutProviderError(
                "no_linked_account", transient=True,
                detail={"driver_id": str(driver.get("_id"))})
        return payout_account_of(driver)

    def _client(self):
        # Read from the running app first, falling back to the class-level
        # config. `current_app.config` is populated from Config at startup, so
        # this is identical in production and testable in a test.
        from flask import current_app

        from .config import Config

        app_cfg = getattr(current_app, "config", {}) or {}
        key_id = app_cfg.get("RAZORPAY_KEY_ID") or getattr(Config, "RAZORPAY_KEY_ID", "")
        key_secret = (app_cfg.get("RAZORPAY_KEY_SECRET")
                      or getattr(Config, "RAZORPAY_KEY_SECRET", ""))
        if not key_id or not key_secret:
            raise PayoutProviderError("not_configured", transient=True)
        try:
            import razorpay
        except ImportError:
            raise PayoutProviderError("sdk_unavailable", transient=True)
        return razorpay.Client(auth=(key_id, key_secret))

    def send(self, payout, driver):
        destination = self.destination_account_id(driver)
        client = self._client()
        amount_paise = int(round(float(payout["amount"]) * 100))
        request = {
            "account_number": destination,
            "amount": amount_paise,
            "currency": payout.get("currency", "INR"),
            "mode": "UPI",
            "purpose": "payout",
            # RazorpayX's own idempotency handle. Deterministic, so a retry
            # after a timeout is recognised as the same transfer rather than a
            # second one. `reference` is unique per payout row.
            "reference_id": payout["reference"],
        }
        try:
            resp = client.payout.create(
                request, idempotency_key="ridemate-%s" % payout["reference"])
        except Exception as exc:  # noqa: BLE001
            raise _classify(exc) from None
        return (resp or {}).get("id")


# Substrings that mean "the provider could not answer", as opposed to "the
# provider answered no". Spelling them out is deliberate: matching on a single
# word like "timeout" misses "Read timed out", and matching loosely enough to
# catch everything starts treating real rejections as retryable.
_TRANSIENT_MARKERS = (
    ("timeout", ("timeout", "timed out", "time out")),
    ("connection_error", ("connection", "unreachable", "network", "dns",
                          "name resolution", "reset by peer")),
    ("rate_limited", ("rate limit", "too many requests", "slow down")),
    ("server_error", ("500", "502", "503", "504", "bad gateway",
                      "service unavailable", "internal server error",
                      "upstream")),
)


def _classify(exc):
    """Turn a provider exception into a retryable or permanent failure.

    Defaults to permanent. An unknown error is more likely to be a bad request
    (a malformed account id, a rejected beneficiary) than a blip, and marking it
    transient would put the payout on an infinite retry loop that keeps
    hammering a real API with a request that can never succeed. An operator
    seeing a `failed` payout investigates; an operator seeing a `processing`
    payout stuck for a week does not.
    """
    text = ("%s %s" % (type(exc).__name__, exc)).lower()
    for reason, markers in _TRANSIENT_MARKERS:
        if any(marker in text for marker in markers):
            return PayoutProviderError(reason, transient=True)
    return PayoutProviderError("rejected")


_PROVIDERS = {
    ManualPayoutProvider.name: ManualPayoutProvider,
    RazorpayXPayoutProvider.name: RazorpayXPayoutProvider,
}


def provider_for(name):
    cls = _PROVIDERS.get((name or "").lower())
    if cls is None:
        raise APIError("Unknown payout provider '%s'." % (name or "none"), 500,
                       code="payout_provider_unknown",
                       details={"known": sorted(_PROVIDERS)})
    return cls()


def idempotency_key_for(payout):
    """A stable key for one payout row.

    Deterministic on purpose. A random key generated per attempt would defeat
    the entire point: the provider would see a new key each time and treat a
    retry as a fresh transfer.
    """
    return "ridemate-%s" % payout["reference"]


def new_attempt_key():
    """A fresh key for something that is genuinely a new attempt.

    Only for operations that are not "send this payout" -- an operator retry
    after fixing onboarding, for instance. Never used by the submit path.
    """
    return "ridemate-%s" % uuid.uuid4().hex
