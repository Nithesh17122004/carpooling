"""The Content-Security-Policy the app actually serves.

A CSP is only useful if it is read against what the app really loads. Getting it
wrong is silent and total: the browser refuses the resource and the payment
modal simply never appears, with nothing in the server logs.

The two failure modes worth testing are opposite and both bad:
* too tight  -> Razorpay Checkout is blocked and nobody can pay;
* too loose  -> the header is decorative, because it permits anything.
So these tests name the specific providers the frontend loads, and then check
that an unlisted host is still refused.
"""

import re

import pytest

from backend.app import _SECURITY_HEADERS as SECURITY_HEADERS

CSP = SECURITY_HEADERS["Content-Security-Policy"]


def _directive(name):
    """The source list for one directive, as a set of strings."""
    match = re.search(r"(?:^|;|\s)%s\s+([^;]+)" % re.escape(name), CSP)
    assert match, "the policy has no %s directive" % name
    return {token.strip() for token in match.group(1).split()}


# ------------------------------------------------------- the checkout must work
@pytest.mark.parametrize("directive,host", [
    ("script-src", "https://checkout.razorpay.com"),
    ("script-src-elem", "https://checkout.razorpay.com"),
    ("frame-src", "https://api.razorpay.com"),
    ("frame-src", "https://checkout.razorpay.com"),
    ("connect-src", "https://api.razorpay.com"),
])
def test_razorpay_is_allowed_where_it_is_actually_used(directive, host):
    """Checkout.js is a script, the payment modal is a frame, and the order and
    verify calls are fetches. Each needs its own directive; allowing only the
    script is the usual mistake and it still breaks checkout."""
    assert host in _directive(directive), (
        "%s is missing from %s, so the browser will block it: %s"
        % (host, directive, CSP))


@pytest.mark.parametrize("directive,host", [
    ("script-src", "https://accounts.google.com"),
    ("connect-src", "https://www.googleapis.com"),
    ("frame-src", "https://accounts.google.com"),
    ("style-src", "https://fonts.googleapis.com"),
    ("font-src", "https://fonts.gstatic.com"),
])
def test_google_sign_in_is_still_allowed(directive, host):
    """Adding the payment provider must not quietly break social login."""
    assert host in _directive(directive)


# --------------------------------------------------- and it must still be a CSP
def test_every_script_host_is_explicit():
    """`'unsafe-inline'` is a real weakness, so the host list is worth pinning.

    If this ever needs 'unsafe-eval' or a bare `https:` wildcard, the change
    should be a deliberate edit here rather than a silent broadening.
    """
    for directive in ("script-src", "script-src-elem"):
        sources = _directive(directive)
        assert "https:" not in sources, "%s permits any host" % directive
        assert "*" not in sources, "%s permits any host" % directive
        assert "'unsafe-eval'" not in sources, "%s permits eval" % directive
        assert "'self'" in sources


def test_the_policy_blocks_framing_and_plugins():
    assert "object-src 'none'" in CSP.replace("  ", " ")
    assert "frame-ancestors 'none'" in CSP.replace("  ", " ")
    # A base-tag injection rewrites every relative URL on the page.
    assert re.search(r"base-uri\s+'none'", CSP), CSP
    assert re.search(r"form-action\s+", CSP)


def test_form_action_covers_the_providers_the_app_posts_to():
    sources = _directive("form-action")
    assert "'self'" in sources
    assert "https://accounts.google.com" in sources


def test_the_policy_is_not_empty_or_wildcarded():
    assert CSP.strip()
    assert "default-src 'self'" in CSP
    # 'unsafe-inline' on scripts is the app's known weakness; it is recorded
    # here so that removing it is a test change rather than a silent diff.
    assert "'unsafe-inline'" in _directive("script-src")
