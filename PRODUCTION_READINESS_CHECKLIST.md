# RideMate production readiness checklist

**Current target:** public marketplace hosted on Render.
**Current decision blockers:** Razorpay sandbox evidence is not yet recorded; Firebase email-link auth needs a Firebase project and server credentials; manual DL/RC/insurance review is selected but must be exercised end to end; payout operations need owner sign-off.
**Release status: NOT APPROVED FOR PUBLIC PRODUCTION.**

This checklist tracks evidence, not intentions. A box is complete only when the evidence exists for the target environment. CI passing is not equivalent to live payments, verified identities, or a security-reviewed production deployment.

## Verified in GitHub Actions

- [ ] Backend tests pass in GitHub Actions against MongoDB 7. The baseline CI run for PR #2 passed, but the current PR #3 run is still pending.
- [x] Production Docker image builds successfully in the production-verification workflow.
- [x] Chromium browser smoke check passes against the running SPA and API (shell, core assets, API readiness, uncaught JS errors).
- [ ] Frontend syntax/dependency checks pass on the latest PR #3 head; confirm the current CI run before merge.
- [ ] Firebase email-link flow passes in a configured Firebase project; Firebase Admin verifies ID tokens; signup/login/session linking are tested in staging.
- [ ] Release artifact is built and scanned for secrets before publication.

Evidence links:
- [Production verification workflow](https://github.com/Nithesh17122004/carpooling/actions/workflows/production-verification.yml)
- [PR #3 CI](https://github.com/Nithesh17122004/carpooling/actions/workflows/ci.yml)

## Owner actions — blocking a public launch

### 1. Create and activate the payment account
- [ ] Confirm the Razorpay account is registered to the legal entity/operator running RideMate and that its marketplace/business model and payment flow are approved; do not assume a standard account permits every marketplace or split-settlement design.
- [x] Owner reports that Razorpay test credentials are already implemented/configured; this is not yet independently verified by a successful sandbox transaction.
- [ ] Configure test-mode keys and the webhook signing secret in Render's secret environment settings, and configure webhook endpoint `/api/payments/webhook` in the Razorpay dashboard.
- [ ] Run real sandbox checkout, valid and invalid webhook signatures, duplicate webhook replay/idempotency, failed payment, cancellation, refund and reconciliation tests.
- [ ] Obtain any required live-mode activation before accepting customer money. Never put keys in Git or this chat.

### 2. Configure managed email-link authentication (Firebase)
- [ ] Create a Firebase project and enable **Authentication → Email/Password → Email link (passwordless sign-in)**.
- [ ] Add the Render origin `https://carpooling-jm9s.onrender.com` to Firebase Authentication's authorized domains; ensure the action/continue URL is allowed.
- [ ] Set `FIREBASE_WEB_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`, and `FIREBASE_APP_ID` in Render. These web config values are public identifiers, not service-account secrets.
- [ ] Create a least-privilege service account for Firebase Admin token verification and set its full JSON as the Render secret `FIREBASE_SERVICE_ACCOUNT_JSON`. Never commit or paste the JSON into chat.
- [ ] Test send-link, expired/invalid links, verified-email enforcement, existing-account linking, session creation, logout, and link completion on desktop/mobile.
- [ ] Keep existing password login available during migration. Do not enable a mandatory provider-only rollout until Firebase sign-in and account recovery are tested; revoke the Resend key previously exposed in chat if it has not already been rotated.

### 3. Decide payout operations
- [ ] Choose **manual payouts** for a tightly controlled initial launch, with dual approval, bank-reference recording, reconciliation and an owner responsible for each payout; OR choose RazorpayX and complete provider onboarding plus signed payout webhook tests.
- [ ] Do not advertise automated payouts unless the provider integration and its failure/retry/reconciliation paths are tested.

### 4. Manual DL / RC / insurance review and document handling
- [x] Selected approach: human review first; no automatic government-record verification claim.
- [x] Vehicle submission requires DL number/document, RC number/document, and insurance number/document; document replacement or identity-detail changes reopen review.
- [x] Admin-only pending-review queue and approve/reject endpoint are available. Reviewers can access private documents only while a packet is submitted.
- [ ] Appoint trained reviewers; define review SLA, rejection reasons/appeals, and a second-person escalation for suspicious documents.
- [ ] Exercise the pending queue with a test vehicle, compare name/number/validity/vehicle class and plate details, approve one packet, reject another, and verify an unreviewed driver cannot publish.
- [ ] Set a strong `DOCUMENT_ENCRYPTION_KEY` in Render secrets; verify document access controls and retention/deletion policy before real identity documents are uploaded.

### 5. Set up Render staging and production
- [ ] Create separate staging and production services (or equivalent isolated environments); configure the Docker web service and set the health check path to `/api/ready`.
- [ ] Provision Redis/Key Value and configure `REDIS_URL`.
- [ ] Provision managed MongoDB (for example MongoDB Atlas) and configure `MONGO_URI` and `MONGO_DB_NAME`; allow only the necessary network access.
- [ ] Set unique strong `SECRET_KEY` and `JWT_SECRET`, `FLASK_ENV=production`, `REFRESH_COOKIE_SECURE=true`, exact HTTPS `FRONTEND_ORIGINS`, `PUBLIC_BASE_URL`, payment settings and document encryption key. Store secrets only in Render environment settings.
- [ ] Deploy staging, verify `/api/ready`, confirm database/Redis checks, HTTPS cookies, CORS, logs and error reporting.
- [ ] Configure database backups and perform a restore drill; configure monitoring/alerts and rehearse rollback.
- [ ] Configure a separate background worker for settlement processing if payout/settlement processing is enabled, and verify it is healthy and observable.
- [ ] After any config change, redeploy and verify the active deploy and runtime settings without exposing secret values.

### 6. Public-launch operational and legal sign-off
- [ ] Obtain qualified local legal/insurance review for the actual launch geography and business model: applicable transport/aggregator rules, driver/passenger terms, vehicle insurance, privacy/retention, consumer protection, refunds, grievance handling and safety incident response.
- [ ] Define support contact and incident escalation, SOS response ownership, fraud/report handling, cancellation/refund policy and a process to suspend unsafe users/drivers.
- [ ] Run a monitored pilot with real volunteers before public promotion; define launch metrics and stop/rollback criteria.
- [ ] Complete a final go/no-go review. Do not open public signups or accept real payments before all critical gates are evidenced.

## Decisions already provided by owner

- Hosting: **Render**
- Launch: **Public marketplace**
- Razorpay: **Test key reported as already configured; live approval and sandbox evidence pending**
- Email provider: **Firebase email-link auth selected; Firebase project and server credentials must be configured**
- Payout model: **Manual payouts preferred for a controlled pilot; dual approval and reconciliation must be operationally confirmed**
- KYC approach: **Manual DL/RC/insurance review selected; provider automation deferred**

## Status rule

Do not mark a box complete just because code exists or a mock test passes. Record the CI run, provider sandbox evidence, staging URL/check results, backup-restore evidence, or written legal/operational sign-off that proves it.
