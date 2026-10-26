# RideMate production readiness checklist

**Current target:** public marketplace hosted on Render.
**Current decision blockers:** no Razorpay account yet; email, payout model, and KYC method are undecided.
**Release status: NOT APPROVED FOR PUBLIC PRODUCTION.**

This checklist tracks evidence, not intentions. A box is complete only when the evidence exists for the target environment. CI passing is not equivalent to live payments, verified identities, or a security-reviewed production deployment.

## Verified in GitHub Actions

- [ ] Backend tests pass in GitHub Actions against MongoDB 7. The baseline CI run for PR #2 passed, but the current PR #3 run is still pending.
- [x] Production Docker image builds successfully in the production-verification workflow.
- [x] Chromium browser smoke check passes against the running SPA and API (shell, core assets, API readiness, uncaught JS errors).
- [ ] Frontend syntax/dependency checks pass on the latest PR #3 head; confirm the current CI run before merge.
- [ ] Release artifact is built and scanned for secrets before publication.

Evidence links:
- [Production verification workflow](https://github.com/Nithesh17122004/carpooling/actions/workflows/production-verification.yml)
- [PR #3 CI](https://github.com/Nithesh17122004/carpooling/actions/workflows/ci.yml)

## Owner actions — blocking a public launch

### 1. Create and activate the payment account
- [ ] Create a Razorpay account for the legal entity/operator running RideMate.
- [ ] Confirm Razorpay approves the intended marketplace/business model and payment flow; do not assume a standard account permits every marketplace or split-settlement design.
- [ ] Generate **test-mode** keys and configure the webhook endpoint `/api/payments/webhook` and a webhook signing secret in Render's secret environment settings.
- [ ] Run real sandbox checkout, valid and invalid webhook signatures, duplicate webhook replay/idempotency, failed payment, cancellation, refund and reconciliation tests.
- [ ] Obtain any required live-mode activation before accepting customer money. Never put keys in Git or this chat.

### 2. Choose and configure email delivery
- [ ] Choose a provider (recommended short-list: Amazon SES, Resend, SendGrid or Postmark).
- [ ] Verify a sender domain/address and configure the provider credentials securely in Render.
- [ ] Implement/configure actual registration and resend-verification email delivery; current code generates a token but has no mail transport wired in.
- [ ] Make email verification mandatory for a public launch only after delivery is working; test delivered links, expiration, replay, invalid tokens and resend rate limits.

### 3. Decide payout operations
- [ ] Choose **manual payouts** for a tightly controlled initial launch, with dual approval, bank-reference recording, reconciliation and an owner responsible for each payout; OR choose RazorpayX and complete provider onboarding plus signed payout webhook tests.
- [ ] Do not advertise automated payouts unless the provider integration and its failure/retry/reconciliation paths are tested.

### 4. Decide identity verification and document handling
- [ ] Decide whether to run a manual-review operation or integrate a compliant third-party KYC provider.
- [ ] If manual review is chosen, appoint trained reviewers, define review SLAs/rejection appeals and limit who can access identity documents.
- [ ] If a vendor is chosen, complete vendor/legal onboarding and test the vendor's verification result and failure states.
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
- Razorpay account: **None yet**
- Email provider: **Undecided**
- Payout model: **Undecided**
- KYC approach: **Undecided**

## Status rule

Do not mark a box complete just because code exists or a mock test passes. Record the CI run, provider sandbox evidence, staging URL/check results, backup-restore evidence, or written legal/operational sign-off that proves it.
