"""A repository secret scanner, as a test.

Credential scanners usually live in CI as a separate binary nobody runs. This
one runs with the suite, so a secret committed in the same change that needs
reviewing is caught by the same command that runs the tests.

The bar is deliberately high, because a scanner that cries wolf gets deleted.
Every pattern here must be matched by a real secret in the fixtures below; if a
rule is not provably useful it does not belong in the file. Placeholder values
from .env.example are expected and are the one thing allowed to look like a key.

Run it standalone for a report:
    python -m backend.tools.scan_secrets [path ...]
"""

import os
import re
import sys

# Directories that are not ours to judge: dependencies and interpreter caches.
_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".idea", ".vscode",
}

# Files whose whole purpose is to hold a placeholder.
_ALLOWLIST = {".env.example", ".env.sample", ".env.template"}

# (rule name, pattern, what a true positive looks like)
_RULES = [
    ("razorpay_key_id", re.compile(r"\brzp_live_[A-Za-z0-9]{10,}\b"),
     "a live Razorpay key id"),
    ("razorpay_key_secret", re.compile(r"\brzp_(?:live|test)_[A-Za-z0-9]{24,}\b"),
     "a Razorpay key secret"),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "a Google API key"),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"), "a GitHub token"),
    ("slack_token", re.compile(r"\bxox[abopsr]-[A-Za-z0-9-]{10,}\b"), "a Slack token"),
    ("openai_key", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b"),
     "an OpenAI-style API key"),
    ("private_key_block",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
     "a private key"),
    ("stripe_secret", re.compile(r"\bsk_live_[A-Za-z0-9]{20,}\b"),
     "a Stripe live secret key"),
    # A JWT is only a finding when it is a real one: three base64url segments
    # with a non-empty signature. The app's own test tokens are caught by this,
    # which is why the allowlist below covers backend/tests fixtures.
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\b"),
     "a signed JWT"),
    # connection strings with an inline password
    ("db_password_in_url",
     re.compile(r"mongodb(?:\+srv)?://[^:/\s]+:[^@/\s]{3,}@"),
     "a database URL with an inline password"),
    # Hardcoded assignment of a long random-looking literal to a secret-ish name
    ("hardcoded_secret_assignment",
     re.compile(
         r"""(?ix)
         \b(?:secret|password|passwd|api_?key|access_?key|private_?key|
              client_?secret|webhook_?secret|token)\b
         \s*[:=]\s*
         ['"][^'"\n]{16,}['"]
         """),
     "a secret assigned from a literal"),
]

# An assignment is only a finding if the value is not an obvious placeholder.
_PLACEHOLDER = re.compile(
    r"""(?ix)
    ^(?:
        |x{3,}|y{3,}|z{3,}|changeme|change[-_]?me|placeholder|example|dummy
      |your[-_]?\w*|my[-_]?\w*secret|test|testing|fake|sample|redacted
      |todo|none|null|secret|password|pass|abc123|12345678|0123456789
      |os\.environ|os\.getenv|getenv|environ
    )$""")

# Files where long random strings are expected and not secrets.
_SKIP_RULES_BY_FILE = {
    "backend/tests/": ("jwt", "hardcoded_secret_assignment"),
    "backend/tools/scan_secrets.py": ("hardcoded_secret_assignment", "jwt"),
}


def _rules_for(relpath):
    skipped = ()
    for prefix, names in _SKIP_RULES_BY_FILE.items():
        if relpath.startswith(prefix):
            skipped = skipped + names
    return [(name, pattern) for name, pattern, _ in _RULES if name not in skipped]


def _is_placeholder(value):
    stripped = value.strip().strip("'\"")
    return bool(_PLACEHOLDER.match(stripped))


def _candidate_files(roots):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in sorted(filenames):
                yield os.path.join(dirpath, name)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def _is_text(path, blob):
    if os.path.splitext(path)[1].lower() in (
            ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz",
            ".whl", ".so", ".pyc", ".woff", ".woff2", ".ttf", ".eot", ".mp4"):
        return False
    return "\x00" not in blob[:4096]


def _display_path(path):
    """Repo-relative when possible.

    os.path.relpath raises ValueError when the path is on a different drive or
    share from the working directory, which is the normal case for a temp
    directory on Windows. Fall back to the absolute path rather than crashing.
    """
    try:
        return os.path.relpath(path).replace(os.sep, "/")
    except ValueError:
        return path.replace(os.sep, "/")


def scan(roots):
    """Yield (relpath, line number, rule, line) for every finding."""
    for path in _candidate_files(roots):
        relpath = _display_path(path)
        base = os.path.basename(path)
        blob = _read(path)
        if not _is_text(path, blob):
            continue
        rules = _rules_for(relpath)
        if not rules:
            continue
        for lineno, line in enumerate(blob.splitlines(), start=1):
            if base in _ALLOWLIST:
                continue
            for name, pattern in rules:
                for match in pattern.finditer(line):
                    value = match.group(0)
                    if _is_placeholder(value):
                        continue
                    if name == "hardcoded_secret_assignment" and _is_placeholder(
                            value.split("=", 1)[-1]):
                        continue
                    yield relpath, lineno, name, line.strip()[:120]


# --------------------------------------------------------------------- CLI
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    roots = argv or ["."]
    findings = list(scan(roots))
    if not findings:
        print("no secrets found")
        return 0
    for relpath, lineno, name, line in findings:
        print("%s:%d: %s: %s" % (relpath, lineno, name, line))
    print("\n%d potential secret(s) found." % len(findings))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
