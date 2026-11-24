# RideMate authentication and KYC verification

## Email and phone

Current email verification is optional by configuration. In production set REQUIRE_EMAIL_VERIFICATION=true, EMAIL_PROVIDER=resend, a verified EMAIL_FROM and a valid RESEND_API_KEY. The production config already rejects missing delivery configuration when email verification is enabled.

For phone ownership use Firebase Phone Authentication or an approved SMS OTP provider. The backend must verify a signed provider token and derive the verified phone claim server-side. Store a verification timestamp and provider reference. Do not trust a client boolean or a bare OTP submitted by the browser. Enforce attempt/resend limits, expiry, replay prevention and re-verification after phone changes.

## DL and RC verification

Existing identity and RC routes store encrypted private files and queue them for human review; clients cannot approve their own records. Preserve that rule.

For automated source verification, onboard RideMate as a DigiLocker/API Setu Requester (or contract a vetted licensed verification provider), obtain credentials and user consent, then validate the trusted provider response: issuer/source, document status, name/DOB or registered-owner match, class, validity and expiry. A file upload, OCR or format check is preliminary only and must never be labelled government-verified.

Until the approved provider is configured, leave records in manual review. Use distinct states: not_submitted, submitted, provider_pending, provider_verified, manual_review, rejected, expired and provider_unavailable.

Never log full DL/RC/Aadhaar identifiers, OTPs, uploaded bytes or provider tokens. Show precise badges such as Phone verified, DL provider verified, RC provider verified or Manually reviewed.