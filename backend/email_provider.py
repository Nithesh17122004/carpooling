"""Transactional email delivery for RideMate.

Only server-side environment configuration is accepted; provider credentials
must never be exposed to the browser or logged.
"""
import requests
from flask import current_app


def send_verification_email(email, name, token):
    """Send an email verification code through Resend.

    Returns True on provider acceptance and raises RuntimeError on missing
    configuration or provider failure. Does not log credentials or response
    bodies, which may contain provider metadata.
    """
    cfg = current_app.config
    provider = (cfg.get("EMAIL_PROVIDER") or "disabled").lower()
    if provider != "resend":
        raise RuntimeError("Email delivery provider is not configured.")
    api_key = cfg.get("RESEND_API_KEY") or ""
    sender = cfg.get("EMAIL_FROM") or ""
    if not api_key or not sender:
        raise RuntimeError("Resend email configuration is incomplete.")

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": sender,
            "to": [email],
            "subject": "Verify your RideMate email",
            "text": (
                f"Hi {name or 'there'},\n\n"
                "Use the verification code below in your RideMate Profile to "
                "verify this email address. It expires in the configured "
                "verification window.\n\n"
                f"{token}\n\n"
                "If you did not create a RideMate account, you can ignore "
                "this email."
            ),
        },
        timeout=8,
    )
    if not response.ok:
        raise RuntimeError(f"Resend rejected the email request (HTTP {response.status_code}).")
    return True
