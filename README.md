# RideMate

Carpooling platform — Flask + MongoDB API, vanilla-JS SPA, real-time notifications.

- **Backend**: Flask 3 app factory in `backend/` (state machines, JWT + refresh-cookie session, atomic seat booking, demo/Razorpay payments + double-entry ledger, timezone-aware scheduling, S3/local private documents, rate limiting, admin routes).
- **Frontend**: static SPA in `frontend/` (no build step — open `frontend/index.html` or serve the folder).

## Quickstart

### 1. MongoDB

Run any reachable mongod; the app pings candidates in order:

```
mongod --dbpath <data-dir> --port 27017
```

### 2. Backend

```
cd backend
python -m venv .venv ; .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # then edit secrets (or set real env vars)
python -m backend.app          # from the repo root, or: flask --app backend.app run
```

Boot-time health: `GET /api/health`, readiness `GET /api/ready`.

Seed demo data (optional): `python -m backend.seed`

### 3. Frontend

```
cd frontend
python -m http.server 5500     # or any static server
```

`frontend/js/api.js` reads `window.RIDEMATE_API` for a cross-origin API base
(e.g., `http://localhost:5000`); otherwise it uses the same origin. The API's
CORS allow-list is `FRONTEND_ORIGINS`.

## Configuration

All config keys come from environment variables / `backend/.env` — see
`backend/.env.example` for the full list with commentary. Required in
production:

| Key | Purpose |
| --- | --- |
| `SECRET_KEY` | Flask signing (placeholder values fail fast when `FLASK_ENV=production`) |
| `JWT_SECRET` | 32+ random chars; rotate to invalidate all sessions |
| `MONGO_URI` | Primary MongoDB (local or Atlas `mongodb+srv://...`) |
| `MONGO_URI_FALLBACKS` | Comma-separated candidates tried in order; use local mongod as offline fallback |
| `PAYMENT_PROVIDER` | `demo` (default) or `razorpay` |
| `GOOGLE_CLIENT_ID` | Set to enable true Google ID-token sign-in; blank ⇒ `503 google_not_configured` |
| `REDIS_URL` | Optional; gives distributed rate limiting + realtime fan-out |
| `REFRESH_COOKIE_SECURE` | **Must be `true` behind TLS** |

## Session & security model

- Access JWT (`rm_token`, 15 min) is returned in the login/register/refresh body
  and sent as `Authorization: Bearer`. The refresh token is an **HttpOnly,
  SameSite=Strict cookie** (`rm_refresh`), rotated on every refresh.
- The SPA silently retries a 401 by calling `POST /api/auth/refresh` exactly
  once before surfacing a session-expired logout (`frontend/js/api.js`).
- Access tokens are short-lived; nothing sensitive is kept in the cookie.
- Input validation is server-side-only (password policy, age, rate, geo,
  document mime/size, allowed rides, payment amounts are recomputed, not trusted
  from the client).
- Rate limits: strict buckets for auth, booking mutations, refresh, and
  payments; per-route defaults from config, in-process buckets, or Redis.
- Private documents (RC/DL/insurance) are served **only** through the authorized
  endpoint `GET /api/uploads/vehicle-doc/<vehicle_id>/<kind>` (owner/admin); the
  public `/uploads/<path>` route refuses them. Avatars are the only public files.

## Testing

```
python -m pytest backend/tests -q     # uses the ridemate_test DB (cleaned per test)
python ...\smoke_hardened.py          # end-to-end black-box smoke against a live server
```

The pytest suite covers auth (rotation, logout-all, password change, fake-Google
rejection, anti-enumeration, rate limiting), bookings (full lifecycle, refunds,
two-phase concurrency with 100 distinct riders, idempotency), timezone (IST/DST/
midnight round-trips), uploads (mime/owner IDOR), document & profile privacy,
search radius/visibility, and payments/ledger integrity. It has caught real
production bugs — e.g., the unique-but-null `idempotency_key` index that silently
allowed only one booking per ride (see `backend/db.py:_unique_string_index`).

## Deployment

- WSGI entry point: `backend/wsgi.py` (`application`).
  `gunicorn -b 0.0.0.0:5000 --timeout 60 backend.wsgi:application`
- Container: `docker build -t ridemate . && docker run -p 5000:5000 ridemate`
  (`backend/.env`, uploads, and logs are excluded from the image — see
  `.dockerignore`).
- The DB indexes (including partial unique indexes) are created automatically at
  app boot (`backend/db.py:create_indexes`).
- Multiple workers/shared rate limits require `REDIS_URL`; the demo payment
  provider is single-process-safe and for development only.