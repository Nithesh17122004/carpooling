# API Setu MoRTH integration

RideMate exposes an authenticated server-side integration for the API Setu MoRTH transport collection documented in the supplied OpenAPI 3.0 schema.

## Status and safety

- POST /api/vehicles/<vehicle_id>/government-verification calls /drvlc and /rvcer.
- Each operation requires its own complete, signed consentArtifact. The application checks the required structure; API Setu remains responsible for validating the signature and consent.
- Aadhaar UID, date of birth, consent artifacts and provider certificate bodies are not persisted. The vehicle record stores only match flags, transaction IDs, response hashes and timestamps.
- A certificate is marked matched only when the XML response contains an explicit active status and all expected identity fields. PDF, unknown XML layouts, missing fields and provider errors fail closed to manual review.
- A matched RC response approves the already-uploaded RC record. A matched DL response is recorded separately; overall vehicle KYC remains subject to the existing insurance verification gate.
- No API Setu credentials are committed to the repository.

## Render environment variables

Set these privately on the RideMate Render web service after the organization has been approved and the MoRTH collection subscription is active:

| Variable | Value |
|---|---|
| APISETU_ENABLED | true only after provider approval |
| APISETU_BASE_URL | https://apisetu.gov.in/certificate/v3/transport |
| APISETU_API_KEY | API key/access token issued for this subscription |
| APISETU_AUTH_HEADER | Exact header name from the approved subscription |
| APISETU_AUTH_SCHEME | Exact scheme/prefix from the approved subscription; blank if none |
| APISETU_TIMEOUT_SECONDS | 20 |

The current API Setu SOP specifies X-APISETU-CLIENTID and X-APISETU-APIKEY headers. If the approved MoRTH subscription issues different authentication instructions, follow those provider-issued instructions.

## Request body

The caller must own the vehicle. The vehicle must already contain a DL number, registration number and uploaded RC. chasis_no follows the spelling in the supplied MoRTH schema.

```json
{
  "uid": "captain-provided UID required by the approved API use case",
  "full_name": "Name as recorded on the certificate",
  "date_of_birth": "DD-MM-YYYY",
  "chasis_no": "Chassis number from the RC",
  "consent_artifacts": {
    "dl": { "consent": {}, "signature": { "signature": "provider-issued-signature" } },
    "rc": { "consent": {}, "signature": { "signature": "provider-issued-signature" } }
  }
}
```

The consent objects above are illustrative structure only. RideMate does not create signatures or fabricate consent. Actual artifacts must be produced through the consent/signing mechanism approved by API Setu/MoRTH for this subscription.

## Important limitation

The uploaded OpenAPI schema says successful responses contain certificate data as XML/PDF/JSON but does not define the actual MoRTH certificate XML field layout or the API authentication header. The client requests XML and uses a strict, conservative field matcher. Before enabling live automatic approvals, test against the provider's official sandbox and confirm the real XML fields and auth method. If the response uses a different schema, the result remains manual-review rather than being incorrectly approved.