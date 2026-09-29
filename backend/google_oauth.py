"""Google OAuth/OIDC verification.

The frontend NEVER sends the user's email/name for "Sign in with Google".
Instead it obtains a Google ID token (credential) from Google Identity
Services and POSTs it here. We cryptographically verify:
  - signature (JWKS),
  - audience == our GOOGLE_CLIENT_ID,
  - issuer == accounts.google.com,

and only then trust `sub`/`email` extracted from the verified token.

If GOOGLE_CLIENT_ID is not configured the endpoint returns an explicit
configuration error -- there is deliberately NO fake bypass.
"""

import time

from .errors import APIError


class GoogleProvider:
    def __init__(self):
        self.client_id = None
        self._verify = None
        self._load()

    def _load(self):
        from .config import Config

        self.client_id = (Config.GOOGLE_CLIENT_ID or "").strip()
        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests

            self._requests = google_requests.Request()
            self._id_token = id_token
        except ImportError:
            self._id_token = None

    def configured(self):
        return bool(self.client_id)

    def verify_credential(self, credential):
        """Verify a Google ID token. Returns {sub, email, email_verified, name}.

        Raises APIError(503/401) with explicit codes -- never falls back to
        trusting a client-supplied email.
        """
        if not self.configured():
            raise APIError(
                "Google sign-in is not configured on this server. "
                "Set GOOGLE_CLIENT_ID to enable it.",
                503,
                code="google_not_configured",
            )
        if not credential or not isinstance(credential, str) or len(credential) > 12000:
            raise APIError("Missing Google credential.", 400, code="google_credential_required")
        if self._id_token is None:
            raise APIError(
                "Google verification library is not installed. "
                "Install 'google-auth' to enable Google sign-in.",
                503,
                code="google_not_configured",
            )

        try:
            info = self._id_token.verify_oauth2_token(
                credential,
                self._requests,
                self.client_id,
                clock_skew_in_seconds=30,
            )
        except ValueError as exc:
            raise APIError(
                "Invalid Google sign-in credential.", 401, code="google_verify_failed", details={"reason": str(exc)}
            )
        except Exception as exc:  # noqa: BLE001 - network/discovery failures
            raise APIError(
                "Could not verify the Google sign-in (network or key error). Try again shortly.",
                502,
                code="google_verify_unavailable",
                details={"reason": type(exc).__name__},
            )

        verified_email = bool(info.get("email_verified"))
        if not verified_email:
            raise APIError("Your Google account email is not verified.", 403, code="google_email_unverified")

        return {
            "sub": str(info["sub"]),
            "email": (info.get("email") or "").lower(),
            "email_verified": verified_email,
            "name": (info.get("name") or "").strip() or (info.get("given_name") or ""),
            "picture": (info.get("picture") or "").strip() or "",
        }


_provider = None


def get_google_provider():
    global _provider
    if _provider is None:
        _provider = GoogleProvider()
    return _provider