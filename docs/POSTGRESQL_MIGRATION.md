# RideMate PostgreSQL migration plan

## Current state

The application uses PyMongo directly across route handlers, services, workers and tests. It depends on Mongo ObjectIds, collection queries, aggregation/update operators, unique/partial indexes and Mongo atomic updates. A PostgreSQL URL swap would break the application and cannot preserve live data.

**This branch deliberately does not switch production to PostgreSQL.** Keep MongoDB as the live source until the schema, idempotent ETL, integrity reconciliation, shadow reads and atomic booking/payment tests pass.

## Target architecture

- PostgreSQL with SQLAlchemy 2.x and Alembic for users, sessions, vehicles, rides, bookings, payments, ledger, KYC metadata, notifications and payouts.
- Redis remains for rate limits, SSE fan-out and ephemeral GPS updates.
- Private documents remain encrypted before object storage. Store only object keys and minimal metadata in PostgreSQL; never persist document bytes or permanent public URLs.
- Use PostgreSQL transactions and row-level locking for seat allocation, booking idempotency and payment claims.

## Suggested relational tables

| Table | Core fields |
|---|---|
| users | UUID PK, email unique, phone, password_hash, email_verified_at, phone_verified_at, role, timestamps |
| refresh_sessions | UUID PK, user_id FK, token_hash unique, expires_at, revoked_at |
| vehicles | UUID PK, owner_id FK, registration_number protected, seats, vehicle_type, verification_state |
| rides | UUID PK, owner_id FK, vehicle_id FK, origin/destination, departure_at timestamptz, seats, fare, status |
| bookings | UUID PK, ride_id FK, rider_id FK, seats, amount_minor, status, idempotency_key |
| payments | UUID PK, booking_id FK unique, provider_order_id unique, amount_minor, webhook idempotency key |
| ledger_entries | UUID PK, payment_id FK, entry_type, amount_minor, currency |
| documents | UUID PK, owner_id FK, vehicle_id FK nullable, kind, object_key unique, sha256, size_bytes, mime, review_state, provider metadata |
| document_reviews | UUID PK, document_id FK, reviewer_id FK, decision, reason, created_at |
| notifications | UUID PK, user_id FK, payload JSONB, read_at, created_at |
| location_samples | bigserial PK, ride_id FK, driver_id FK, geography point, recorded_at |

Use integer minor currency units or NUMERIC, never float for money. Add partial unique indexes for non-null idempotency keys, indexes on ride status/departure, and PostGIS only if geographic search requires it.

## Safe migration phases

1. Backup all Mongo collections and every private object; record counts and checksums.
2. Create SQLAlchemy models, Alembic baseline and a stable legacy-ID mapping table. Keep explicit Mongo ObjectId to UUID mappings.
3. Write restartable ETL with checkpoints, dead-letter export, UTC/type normalization and foreign-key ordering.
4. Import to staging PostgreSQL and reconcile counts, orphans, booking seats, payment totals, double-entry ledger balances, KYC state and ride history.
5. Shadow-read critical endpoints; load-test concurrent bookings with SELECT FOR UPDATE and duplicate payment webhooks.
6. Pause writes for final delta import, reconcile again, switch DATABASE_URL and monitor. Keep rollback backups and a tested rollback procedure.
7. Remove PyMongo only when route handlers, background workers, scripts and tests are fully converted.

## Document storage

Cloudflare R2 is an S3-compatible candidate. Configure the existing S3 adapter using STORAGE_BACKEND=s3, STORAGE_ENDPOINT, STORAGE_REGION=auto, STORAGE_BUCKET, STORAGE_ACCESS_KEY and STORAGE_SECRET_KEY. Keep the bucket private, encrypt sensitive KYC files before upload, generate short-lived signed URLs only after server authorization, and apply lifecycle deletion for replaced/expired documents. Verify pricing and data residency before selecting a provider.