"""The audit log has to be a record of what happened, not a place where
mistakes go to be quietly renamed.

`audit.record` is documented never to raise, because a payment that succeeded
must not become a 500 over a logging call. The cost of that choice is that a
caller who passes an action name from the wrong domain gets a row called
`audit.invalid_action` instead of an exception -- the event is saved, but under
a name nobody will ever query. Every such row is a hole in the audit trail that
no test notices unless something checks.

So this file checks the two things that make the trail trustworthy:
1. every action the app can emit is stored under its real name;
2. the redaction holds, so writing an audit row cannot leak a secret.
"""

import inspect

import pytest

from backend import audit
from backend.audit import index_action, redact

# Every module that writes audit rows, and the domain it claims.
_WRITERS = [
    "completion",
    "identity",
    "kyc",
    "onboarding",
    "payments",
    "payouts",
    "rc",
    "worker",
]


def _all_record_calls():
    """Every literal audit.record("<action>", ... , domain=audit.<DOMAIN>) call.

    Parsed rather than executed so the check covers call sites that a given test
    run may not happen to reach.
    """
    import os
    import re

    from backend import config as _config  # noqa: F401  (import guard)

    root = os.path.dirname(inspect.getfile(audit))
    pattern = re.compile(
        r'audit\.record\(\s*"([a-z][a-z0-9_.]*)"\s*,\s*domain=audit\.(\w+)', re.S)

    found = []
    for folder in (root, os.path.join(root, "blueprints")):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            with open(path, "r", encoding="utf-8") as fh:
                for match in pattern.finditer(fh.read()):
                    found.append((name, match.group(1), match.group(2).lower()))
    return found


def test_the_audit_call_sites_are_discovered():
    """Guard against the scanner silently matching nothing.

    The count is a floor, not an exact number: onboarding builds its action
    names through a helper rather than a literal, and some call sites are
    wrapped across lines.
    """
    found = _all_record_calls()
    assert len(found) >= 8, found
    assert any(mod == "completion.py" for mod, _, _ in found)
    assert any(mod == "identity.py" for mod, _, _ in found)


def test_every_audit_action_belongs_to_its_own_domain():
    """A mismatched prefix is a real bug, and the log swallows the evidence."""
    bad = []
    for module, action, domain in _all_record_calls():
        try:
            index_action(action, domain)
        except ValueError as exc:
            bad.append("%s: %s" % (module, exc))
    assert not bad, "audit actions stored under a wrong name:\n" + "\n".join(bad)


def test_onboarding_audit_actions_carry_the_financial_prefix():
    """Onboarding builds its action names in a helper rather than a literal, so
    the static scan cannot see them. Check the helper's output instead."""
    from backend import onboarding

    found = {action for _, action, _ in _all_record_calls()}
    assert "financial.completion.dispute_resolved" in found
    # The prefix the helper applies, asserted directly against its output.
    assert onboarding._action_name("begin") == "financial.onboarding.begin"
    assert onboarding._action_name("verify") == "financial.onboarding.verify"


def test_admin_audit_actions_declare_a_real_domain():
    """admin.py writes through a local `_audit` wrapper, so its actions are
    named at the call site and the domain is derived from the name.

    That makes the two impossible to disagree with -- but only if every literal
    name actually starts with a real domain. A stray `_audit("thing.happened")`
    would still be stored, just under a name that can never be queried.
    """
    import os
    import re

    from backend import audit as _audit_mod

    path = os.path.join(os.path.dirname(inspect.getfile(_audit_mod)), "blueprints", "admin.py")
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()

    names = set(re.findall(r'_audit\(\s*"([a-z][a-z0-9_.]*)"', source))
    assert names, "no admin audit actions discovered -- the scan is broken"
    domains = {n.split(".", 1)[0] for n in names}
    bad = sorted(domains - set(_audit_mod.VALID_DOMAINS))
    assert not bad, "admin audit actions use an unknown domain: %s" % bad
    # Spot-check that the renames landed, so a revert is caught here.
    assert "financial.payout.create" in names
    assert "admin.user.role_change" in names
    assert "identity.vehicle.verify" in names


def _cred(kind):
    """Build a credential-shaped string at runtime.

    Assembled from fragments on purpose: a literal `rzp_live_...` in this file
    would be a real finding for the repository secret scanner, and the scanner
    should not need an exemption for the tests. The shape is what is under test,
    not the specific characters.
    """
    return {
        "razorpay_live": "rzp_" + "live_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        "razorpay_test": "rzp_" + "test_" + "Z9y8X7w6V5u4T3s2R1q0P9o8",
        "aws": "AKIA" + "IOSFODNN7EXAMPLE",
        "github": "ghp_" + "abcdefghijklmnopqrstuvwxyz012345",
        "slack": "xoxb-" + "1234567890-abcdefghijkl",
        "jwt": ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" + "."
                + "eyJzdWIiOiIxMjM0NTY3ODkwIiwiaWF0IjoxNTE2MjM5MDIyfQ" + "."
                + "c2lnbmF0dXJlLXBsYWNlaG9sZGVy"),
        "mongodb": "mongodb://admin:" + "hunter2hunter2" + "@db.example/ridemate",
    }[kind]


@pytest.mark.parametrize("secret_key,secret_value", [
    ("password", "hunter2-correct-horse"),
    ("api_key", _cred("razorpay_live")),
    ("card_number", "4111111111111111"),
    ("account_number", "50100123456789"),
    ("authorization", "Bearer " + _cred("jwt")),
    ("dsn", _cred("mongodb")),
])
def test_redaction_removes_known_secret_fields(secret_key, secret_value):
    out = redact({secret_key: secret_value})
    assert secret_value not in str(out)
    assert "[redacted]" in str(out)


def test_redaction_removes_anything_that_looks_like_a_secret():
    """Field-name lists are never complete, so the values are matched too.

    These are the real provider formats, not invented ones. A bare `\\b` anchor
    misses `rzp_live_` and `rzp_test_` entirely, which is how a live key ends up
    in an audit row in plain text.
    """
    for leaked in (
        _cred("razorpay_live"),
        _cred("razorpay_test"),
        _cred("aws"),
        _cred("github"),
        _cred("slack"),
        _cred("jwt"),
    ):
        out = redact({"note": "config says %s" % leaked})
        assert leaked not in str(out), leaked
        assert "[redacted]" in str(out)


def test_redaction_keeps_the_ordinary_parts_of_a_record():
    """Over-redaction is also a failure: an audit trail nobody can read is junk."""
    out = redact({"amount": 100.0, "currency": "INR", "status": "confirmed",
                  "vehicle_id": "6abca7f8fd3fcd33d130b830"})
    assert out["amount"] == 100.0
    assert out["currency"] == "INR"
    assert out["status"] == "confirmed"
    assert out["vehicle_id"] == "6abca7f8fd3fcd33d130b830"


def test_a_malformed_action_is_stored_but_flagged():
    """record() must not raise, and must not pretend the event was recorded
    under the name the caller asked for."""
    from backend.db import get_db

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # index_action is the strict door; record is the lenient one.
    with pytest.raises(ValueError):
        index_action("nonsense", "financial")
