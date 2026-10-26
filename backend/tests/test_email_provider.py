"""Tests for the server-side Resend transactional email adapter."""
from types import SimpleNamespace

import pytest

from backend.email_provider import send_verification_email


def test_resend_verification_email_request(app, monkeypatch):
    app.config.update({
        "EMAIL_PROVIDER": "resend",
        "RESEND_API_KEY": "test-resend-key",
        "EMAIL_FROM": "RideMate <verify@example.com>",
    })
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return SimpleNamespace(ok=True, status_code=200)

    monkeypatch.setattr("backend.email_provider.requests.post", fake_post)
    with app.app_context():
        assert send_verification_email("rider@example.com", "Rider", "safe-token")
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer test-resend-key"
    assert captured["json"]["to"] == ["rider@example.com"]
    assert "safe-token" in captured["json"]["text"]
    assert captured["timeout"] == 8


def test_resend_requires_server_configuration(app):
    app.config.update({"EMAIL_PROVIDER": "resend", "RESEND_API_KEY": "", "EMAIL_FROM": ""})
    with app.app_context(), pytest.raises(RuntimeError, match="configuration is incomplete"):
        send_verification_email("rider@example.com", "Rider", "safe-token")


def test_resend_provider_failure_is_not_silenced(app, monkeypatch):
    app.config.update({
        "EMAIL_PROVIDER": "resend",
        "RESEND_API_KEY": "test-resend-key",
        "EMAIL_FROM": "RideMate <verify@example.com>",
    })
    monkeypatch.setattr(
        "backend.email_provider.requests.post",
        lambda *args, **kwargs: SimpleNamespace(ok=False, status_code=401),
    )
    with app.app_context(), pytest.raises(RuntimeError, match="HTTP 401"):
        send_verification_email("rider@example.com", "Rider", "safe-token")


def test_registration_sends_verification_email_when_required(app, client, monkeypatch):
    from backend.blueprints import auth as auth_blueprint

    app.config.update({
        "REQUIRE_EMAIL_VERIFICATION": True,
        "EMAIL_PROVIDER": "resend",
        "RESEND_API_KEY": "test-resend-key",
        "EMAIL_FROM": "RideMate <verify@example.com>",
    })
    sent = []
    monkeypatch.setattr(
        auth_blueprint, "send_verification_email",
        lambda email, name, token: sent.append((email, name, token)) or True,
    )
    response = client.post("/api/auth/register", json={
        "name": "Email Test",
        "email": "email-verification@test.in",
        "password": "Password123",
    })
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["verification_email_sent"] is True
    assert len(sent) == 1
    assert sent[0][0] == "email-verification@test.in"
    assert sent[0][2]
