# Production readiness checklist

This file tracks evidence, not intentions. A check is **verified** only when its stated evidence exists for the target environment. CI passing is not equivalent to a live payment, a verified identity provider, or a security-reviewed production deployment.

## Automated evidence

- [ ] Backend tests pass in GitHub Actions against MongoDB 7.
- [ ] Frontend JavaScript syntax checks pass.
- [ ] Production Docker image builds.
- [ ] Browser smoke check passes against the running SPA and API with a real Chromium browser.
- [ ] Release artifact is built and scanned for secrets before publication.

## Requires owner access or an external system

- [ ] Razorpay test-mode checkout, signed `payment.captured` webhook, duplicate webhook replay, failed payment and refund tested against Razorpay.
- [ ] If RazorpayX payouts will be used: test linked-account onboarding and provider-confirmed success/failure webhooks. If payouts remain manual, document dual-control and bank-reconciliation procedures instead.
- [ ] Email provider configured; registration and resend-verification emails arrive; expiry, replay and invalid-token behavior verified.
- [ ] Decide whether human KYC review is acceptable for the pilot or select and contract a compliant verification provider. The current code workflow is not evidence of third-party identity validation.
- [ ] Browser test with Razorpay test checkout and mobile/desktop viewport evidence.
- [ ] Staging deployment passes readiness checks; HTTPS, CORS allowlist, Redis, managed MongoDB, encrypted private documents, backups and a successful restore drill are evidenced.
- [ ] Production secrets are in the deployment secret manager, never in Git; access is restricted and rotation/recovery is documented.
- [ ] Monitoring and alerting cover payment/webhook failures, payout failures, 5xx, readiness, queue backlog and database/Redis outages; rollback is rehearsed.
- [ ] Local legal counsel/insurance review covers applicable state rules, platform/aggregator classification, driver eligibility, insurance, privacy/retention, consumer terms, refunds, grievance handling and incident response.
- [ ] Small monitored pilot completes with consent, support coverage, incident handling and defined stop criteria before wider launch.

## Owner inputs needed

1. **Email:** choose an SMTP/email provider (e.g. SES, Resend, SendGrid, Postmark) and provide sender domain/address after configuring secrets in the deployment platform. Never paste secrets into chat or commit them.
2. **Payments:** confirm the Razorpay account is yours/your organisation's and available for RideMate's intended marketplace/payment model; create test keys and a webhook endpoint/secret in the deployment secret store. Live approval is a separate provider decision.
3. **Payouts:** choose manual payouts or RazorpayX. For RazorpayX, complete the required merchant and linked-account onboarding.
4. **KYC:** choose human review for a limited pilot or name the KYC provider you want evaluated. Do not send real Aadhaar/passport/driver documents in this chat.
5. **Deployment:** provide the host/platform choice and configure the production/staging environment there. Share only non-secret service URLs and deployment status here.
6. **Policy/legal:** confirm initial launch geography, whether this is a campus/employer/private pilot or public marketplace, intended driver compensation, and who will review legal/insurance documents.

## Status rule

Do not change a box to complete solely because code exists or a mock test passes. Record the CI run, provider test evidence, staging URL/check results, or signed-off operational review that proves it.
