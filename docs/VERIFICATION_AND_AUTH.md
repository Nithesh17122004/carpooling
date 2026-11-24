# RideMate authentication and KYC verification

## Email and phone

Current email verification is optional by configuration. In production set REQUIRE_EMAIL_VERIFICATION=true, EMAIL_PROVIDER=resend, a verified EMAIL_FROM and a valid RESEND_API_KEY. The production config already rejects missing delivery configuration when email verification is enabled.

For phone ownership use Firebase Phone Authentication or an approved SMS OTP provider. The backend must verify a signed provider token and derive the verified phone claim server-side. Store a verification timestamp and provider reference. Do not trust a client boolean or a bare OTP submitted by the browser. Enforce attempt/resend limits, expiry, replay prevention and re-verification after phone changes.

## DL and RC verification

Existing identity and RC routes store encrypted private files and queue them for human review; clients cannot approve their own records. Preserve that rule.

For automated source verification, onboard RideMate as a DigiLocker/API Setu Requester (or contract a vetted licensed verification provider), obtain credentials and user consent, then validate the trusted provider response: issuer/source, document status, name/DOB or registered-owner match, class, validity and expiry. A file upload, OCR or format check is preliminary only and must never be labelled government-verified.

Until the approved provider is configured, leave records in manual review. Use distinct states: not_submitted, submitted, provider_pending, provider_verified, manual_review, rejected, expired and provider_unavailable.

Never log full DL/RC/Aadhaar identifiers, OTPs, uploaded bytes or provider tokens. Show precise badges such as Phone verified, DL provider verified, RC provider verified or Manually reviewed.

## Free DigiLocker-issued document workflow (no API credentials)

RideMate does not call DigiLocker, API Setu, Parivahan or any third-party verification API in this mode. The captain downloads the DL/RC from the DigiLocker **Issued Documents** section and uploads the document through the existing private upload flow. DigiLocker Drive uploads are not equivalent to documents fetched from an issuer.

Admin review steps:
1. Confirm the document is represented as an issuer-issued DigiLocker document, not a Drive upload.
2. Where an encrypted DigiLocker QR is present, scan it with the official DigiLocker mobile app and confirm the returned document details match the uploaded document. If the QR is absent or cannot be verified, do not label the file DigiLocker-verified.
3. For digitally signed PDFs, validate the signature and certificate chain using a trusted PDF signature-validation tool; check the signing certificate, issuer, integrity and signing status. A visible signature image is not a digital signature.
4. Compare captain name and DL details, vehicle registration number and RC owner/vehicle details. Check validity/expiry and any mismatch.
5. Approve only after the checks are complete. Otherwise reject with an actionable reason or leave the case pending manual review.

The current API continues to record only an administrator's decision; the upload itself does not prove DigiLocker origin. No OCR, filename, QR image alone, user-supplied boolean or client-side status may set a verified state. Ride publishing remains blocked until the existing server-side DL/insurance and RC approval gates are satisfied. This workflow is free to operate with the official DigiLocker app and suitable PDF signature-validation software, but the human review is not an automated government database check.

DigiLocker partner/API integration remains a separate future option and requires onboarding and authorised access. See the official [DigiLocker security architecture](https://www.digilocker.gov.in/web/security-architecture), [implementation model](https://www.digilocker.gov.in/web/implementation-model) and [FAQ on issued vs uploaded documents](https://wb.digilocker.gov.in/web/about/faq).


## Server-enforced conditional approval and review evidence

Approving a DL/insurance packet or RC now requires a server-validated review-evidence object. The admin screen prompts the reviewer to attest that they:

- confirmed the file is an issuer-issued DigiLocker document rather than a DigiLocker Drive upload;
- validated document integrity using either the QR in the official DigiLocker app or a PDF digital signature and certificate chain;
- matched the holder details and checked validity/expiry; and
- for an RC, matched the registration number and vehicle details to the RideMate vehicle.

The API rejects approval requests that omit the evidence, select an unsupported integrity method, or omit any required attestation. It stores the reviewer ID, timestamp, method, evidence flags and `automated: false` with the approved document record. A document replacement clears prior evidence. Rejections continue to require a reason.

**Important limitation:** these are server-enforced admin attestations, not cryptographic verification performed by RideMate. The app does not decode DigiLocker QR payloads, call a government service, or validate PDF certificate chains itself. A reviewer must perform those checks out of band before confirming them. The booleans sent by the admin UI are not proof by themselves; the trust boundary is the authenticated admin and the audit trail. Cases where QR/signature verification is unavailable must remain pending or be rejected, not approved as DigiLocker-verified.
