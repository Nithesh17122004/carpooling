"""The secret scanner has to be able to fail, or it is decoration.

A scanner that reports "no secrets found" because its rules stopped matching is
worse than no scanner, because it is trusted. So this file writes a real
credential of each shape to a temp directory, points the scanner at it, and
requires a finding. Then it requires the opposite on a clean file, so the rules
cannot be loosened into uselessness either.
"""

import os
import textwrap

import pytest

from backend.tools import scan_secrets


def _s(*parts):
    """Join credential fragments.

    The scanner reads source text, so a credential-shaped value written as one
    literal in this file is a real finding -- and test_the_repository_itself_is_clean
    scans this file. The shape is what's under test, not the characters, so the
    values are assembled at runtime.
    """
    return "".join(parts)


# One real-shaped credential per rule. If a rule is added without a case here,
# this file does not know about it, which is the point.
CREDENTIALS = {
    "razorpay_key_id": _s("rzp_", "live_", "A1b2C3d4E5f6G7h8I9j0"),
    "razorpay_key_secret": _s("rzp_", "test_", "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6"),
    "aws_access_key": _s("AKIA", "IOSFODNN7EXAMPLE"),
    "github_token": _s("ghp_", "abcdefghijklmnopqrstuvwxyz0123456789"),
    "slack_token": _s("xoxb-", "1234567890-abcdefghijkl"),
    "openai_key": _s("sk_", "live_", "abcdefghijklmnopqrstuvwx"),
    "stripe_secret": _s("sk_", "live_", "abcdefghijklmnopqrstuvwx"),
    "private_key_block": _s("-----BEGIN ", "RSA PRIVATE KEY-----"),
    "jwt": _s("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", ".",
              "eyJzdWIiOiIxMjM0NTY3ODkwIiwiaWF0IjoxNTE2MjM5MDIyfQ", ".",
              "c2lnbmF0dXJlLXBsYWNlaG9sZGVy"),
    "db_password_in_url": _s("mongodb://", "admin:", "sup3rsecret",
                             "@db.example/ridemate"),
}

# A shorter variant of the same shapes, for the allowlist / skip-directory cases.
_RAZORPAY_SHAPED = _s("rzp_", "live_", "A1b2C3d4E5f6G7h8I9j0")
_AWS_SHAPED = _s("AKIA", "IOSFODNN7EXAMPLE")


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.mark.parametrize("rule,credential", sorted(CREDENTIALS.items()))
def test_the_scanner_catches_every_shape_it_claims_to(tmp_path, rule, credential):
    _write(tmp_path, "config.py", "SETTING = %r\n" % credential)
    found = list(scan_secrets.scan([str(tmp_path)]))
    assert found, "the scanner missed a %s" % rule
    assert any(f[2] == rule for f in found), [f[2] for f in found]


def test_the_scanner_catches_a_dsn_with_an_inline_password(tmp_path):
    _write(tmp_path, "settings.py",
           "MONGO = %r\n" % CREDENTIALS["db_password_in_url"])
    found = list(scan_secrets.scan([str(tmp_path)]))
    assert [f[2] for f in found] == ["db_password_in_url"]


def test_the_scanner_catches_a_hardcoded_assignment(tmp_path):
    _write(tmp_path, "app.py",
           'WEBHOOK_SECRET = "8f14e45fceea167a5a36dedd4bea2543"\n')
    found = list(scan_secrets.scan([str(tmp_path)]))
    assert "hardcoded_secret_assignment" in [f[2] for f in found]


def test_ordinary_source_is_not_flagged(tmp_path):
    """A scanner that cries wolf gets deleted, so the clean case matters."""
    _write(tmp_path, "app.py", """
        from flask import Flask
        app = Flask(__name__)
        PORT = 5000
        MAX_RIDES = 25

        def handler(request):
            url = "https://api.example.com/v1/rides?limit=25"
            return {"ok": True, "count": len(request.args)}
    """)
    assert list(scan_secrets.scan([str(tmp_path)])) == []


def test_env_reads_are_not_flagged(tmp_path):
    _write(tmp_path, "app.py", """
        import os
        SECRET_KEY = os.environ["SECRET_KEY"]
        API_KEY = os.getenv("RAZORPAY_KEY_ID")
        TOKEN = os.environ.get("JWT_SECRET", "")
    """)
    assert list(scan_secrets.scan([str(tmp_path)])) == []


def test_the_env_example_is_allowed_to_look_like_a_key(tmp_path):
    """The whole point of .env.example is to show the shape of a value."""
    _write(tmp_path, ".env.example", "RAZORPAY_KEY_ID=%s\n" % _RAZORPAY_SHAPED)
    assert list(scan_secrets.scan([str(tmp_path)])) == []


def test_a_secret_hiding_in_an_ignored_directory_is_not_scanned(tmp_path):
    _write(tmp_path, ".venv/lib/leaked.py", "K = %r\n" % _RAZORPAY_SHAPED)
    _write(tmp_path, "__pycache__/x.py", "K = %r\n" % _RAZORPAY_SHAPED)
    assert list(scan_secrets.scan([str(tmp_path)])) == []


def test_binary_files_are_skipped(tmp_path):
    path = tmp_path / "logo.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + _RAZORPAY_SHAPED.encode())
    assert list(scan_secrets.scan([str(tmp_path)])) == []


def test_the_repository_itself_is_clean():
    """The real assertion: this repository contains no committed secret."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(scan_secrets.__file__))))
    findings = list(scan_secrets.scan([root]))
    assert not findings, "\n".join(
        "%s:%d %s: %s" % (path, line, rule, text)
        for path, line, rule, text in findings)


def test_the_cli_reports_findings_and_exits_nonzero(tmp_path, capsys):
    _write(tmp_path, "leak.py", "K = %r\n" % _AWS_SHAPED)
    assert scan_secrets.main([str(tmp_path)]) == 1
    assert "potential secret" in capsys.readouterr().out



def test_the_cli_reports_a_clean_tree_and_exits_zero(tmp_path, capsys):
    _write(tmp_path, "clean.py", "x = 1\n")
    assert scan_secrets.main([str(tmp_path)]) == 0
    assert "no secrets found" in capsys.readouterr().out
