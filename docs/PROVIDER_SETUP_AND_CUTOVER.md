# RideMate external verification provider setup

This repository deliberately keeps document approval server-side. No user-facing upload, OCR result, filename, or client flag is proof that a government issued a document.

## Phone OTP (Firebase)

The frontend now offers SMS OTP immediately after Firebase email-link sign-in when the account has a phone number that is not yet verified.

1. Use the same Firebase project for the browser app and Firebase Admin SDK.
2. Enable **Email link** and **Phone** providers in Firebase Authentication.
3. Add the production RideMate hostname to Firebase Authorized domains.
4. Configure `FIREBASE_WEB_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`, `FIREBASE_APP_ID` and server-only `FIREBASE_SERVICE_ACCOUNT_JSON`.
5. Configure SMS billing/quotas and reCAPTCHA restrictions in Firebase.
6. Keep `FIREBASE_AUTH_ENFORCE=false` until email-link signup, account linking, phone linking, recovery and duplicate-email cases are tested in staging. Enable only after those tests pass.
7. Verify that `POST /api/auth/phone/verify` rejects a token with no verified phone claim, a different Firebase UID, an expired token and a phone number that is not linked to the current account.

The backend stores a verified phone claim only after Firebase Admin validates the signed ID token and the token UID matches the user's linked Firebase UID. Do not accept client `phone_verified=true` flags.

## Driving licence, RC and identity verification

The current application supports secure uploads, encrypted private storage and human review. Automatic approval is intentionally not enabled because no authorised provider credentials or consent flow are present in the repository.

Before enabling automation, contract and onboard with an authorised service such as DigiLocker/API Setu or a vetted regulated verification partner. Confirm that the specific API product supports the exact DL/RC checks, the use case is permitted, and the platform is authorised to access it. A generic API Setu account or OCR provider alone does not grant access to every government dataset.

Required integration contract:

- Provider-issued client credentials stored only in the deployment secret manager.
- User-facing consent and purpose disclosure before fetching a document or validating a record.
- Server-to-server OAuth/token exchange; never expose provider secrets to the browser.
- Validate provider response signature/issuer, request correlation ID, subject identity, document type, registration number, owner match, validity/expiry and revocation/cancellation status where supplied.
- Persist provider, transaction reference, checked-at timestamp, outcome, minimal response evidence and a hash of the source document. Avoid retaining raw provider payloads unless the contract requires it.
- Idempotent callbacks, strict timeouts, retry limits, audit events and a manual-review fallback for unavailable/ambiguous results.
- Distinguish `provider_verified`, `manual_review`, `rejected`, `expired` and `provider_unavailable`. Never collapse all states into a generic green "verified" badge.
- Re-run checks when a user replaces a document or the document expires.

Do not mark a DL/RC automatically verified until a real provider integration is configured, legal terms are reviewed and provider sandbox tests pass.

## Private object storage (S3 / Cloudflare R2)

The existing private document module encrypts identity and vehicle documents with AES-256-GCM before sending bytes to storage. S3-compatible storage is already supported by `backend/storage.py`.

1. Create a private bucket with public access disabled.
2. Set `STORAGE_BACKEND=s3`, `STORAGE_ENDPOINT` (for R2), `STORAGE_REGION=auto`, `STORAGE_BUCKET`, `STORAGE_ACCESS_KEY` and `STORAGE_SECRET_KEY`.
3. Set `DOCUMENT_ENCRYPTION_KEY` to a securely generated 32-byte key (base64 or 64-character hex). Store it in Render/production secrets, not Git.
4. Back up and rotate the encryption key with a documented re-encryption plan. Losing this key makes stored KYC documents unreadable.
5. Keep access through the existing owner/admin-authorized API routes; do not make private object keys public or return permanent public URLs.
6. Test upload, authorized download, cross-user denial, deletion and recovery before switching production storage.

## PostgreSQL migration status

PostgreSQL is not activated by setting `DATABASE_URL`. The current runtime uses PyMongo collections throughout routes, services, workers and tests. A safe conversion requires relational models, migration revisions, ObjectId-to-UUID mapping, a restartable ETL, data reconciliation, shadow reads, concurrency tests for seat claims/payment idempotency and a controlled cutover. Keep MongoDB as the source of truth until all phases in `POSTGRESQL_MIGRATION.md` are complete.
