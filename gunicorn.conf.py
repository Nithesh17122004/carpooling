"""Gunicorn production configuration.

Run from the repo root:  gunicorn -c gunicorn.conf.py backend.wsgi:application
"""

import multiprocessing
import os
import tempfile

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
workers = int(os.environ.get("GUNICORN_WORKERS", min(multiprocessing.cpu_count() * 2 + 1, 6)))
threads = int(os.environ.get("GUNICORN_THREADS", 2))
worker_tmp_dir = tempfile.mkdtemp(prefix="gunicorn-")

# SSE streams are long-lived: a generous timeout avoids killing them.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 120))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", 30))
keepalive = 5
max_requests = 2000
max_requests_jitter = 200

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
capture_output = True

forwarded_allow_ips = "*"  # trusts X-Forwarded-For from the reverse proxy only
proxy_protocol = False

# Security: run as a non-privileged user when available (container).
try:
    import pwd

    _user = pwd.getpwnam("nobody")
    _uid = _user.pw_uid
    _gid = _user.pw_gid
except Exception:  # noqa: BLE001 - Windows/local dev keeps current user
    _uid = _gid = None
if _uid is not None and os.geteuid and os.geteuid() == 0:
    user = _uid
    group = _gid