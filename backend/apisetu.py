"""API Setu MoRTH transport certificate client.

The API Setu collection returns certificate content, not a simple boolean. This
client therefore fails closed: it only labels a certificate matched when the
returned XML exposes an explicit active status and the expected identity fields.
Unknown response layouts remain pending for manual review.
"""
import hashlib
import json
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from flask import current_app


class APISetuError(Exception):
    def __init__(self, code, message, status=502):
        super().__init__(message)
        self.code = code
        self.status = status


def _norm(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _date_norm(value):
    value = str(value or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:10], fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    return _norm(value)


def _flatten_xml(raw):
    root = ET.fromstring(raw)
    values = {}
    for node in root.iter():
        for key, value in node.attrib.items():
            values[key.lower().replace("-", "").replace("_", "")] = value
        if node.text and node.text.strip():
            tag = node.tag.split("}")[-1].lower().replace("-", "").replace("_", "")
            values[tag] = node.text.strip()
    return values


def _pick(values, names):
    for name in names:
        value = values.get(name.lower().replace("-", "").replace("_", ""))
        if value:
            return value
    return None


def _consent_valid_shape(artifact):
    if not isinstance(artifact, dict):
        return False
    consent = artifact.get("consent") or {}
    signature = artifact.get("signature") or {}
    permission = consent.get("permission") or {}
    return bool(
        consent.get("consentId")
        and consent.get("timestamp")
        and (consent.get("dataConsumer") or {}).get("id")
        and (consent.get("dataProvider") or {}).get("id")
        and (consent.get("purpose") or {}).get("description")
        and (consent.get("user") or {}).get("idNumber")
        and (consent.get("data") or {}).get("id")
        and permission.get("access")
        and (permission.get("dateRange") or {}).get("from")
        and (permission.get("dateRange") or {}).get("to")
        and signature.get("signature")
    )


def _auth_headers():
    api_key = current_app.config.get("APISETU_API_KEY", "")
    client_id = current_app.config.get("APISETU_CLIENT_ID", "")
    if not api_key or not client_id:
        raise APISetuError("apisetu_credentials_missing",
                          "API Setu client ID and API key are required.", 503)
    # API Setu's current SOP names these two headers. The approved collection
    # subscription may specify an alternative; follow provider-issued docs if so.
    return {
        "X-APISETU-CLIENTID": client_id,
        "X-APISETU-APIKEY": api_key,
        "Content-Type": "application/json",
        "Accept": "application/xml, application/json",
    }

def verify_certificate(kind, parameters, consent_artifact):
    if not current_app.config.get("APISETU_ENABLED", False):
        raise APISetuError("apisetu_disabled",
                          "API Setu verification is not enabled for this deployment.", 503)
    if kind not in ("drvlc", "rvcer"):
        raise APISetuError("invalid_document_type", "Unsupported certificate type.", 422)
    if not _consent_valid_shape(consent_artifact):
        raise APISetuError("consent_artifact_invalid",
                          "A complete, validly signed consent artifact is required.", 422)

    base = (current_app.config.get("APISETU_BASE_URL") or "").rstrip("/")
    if not base.startswith("https://"):
        raise APISetuError("apisetu_endpoint_invalid",
                          "API Setu base URL must use HTTPS.", 503)
    txn_id = str(uuid.uuid4())
    body = {
        "txnId": txn_id,
        "format": "xml",
        "certificateParameters": parameters,
        "consentArtifact": consent_artifact,
    }
    try:
        response = requests.post(
            base + "/" + kind,
            headers=_auth_headers(),
            json=body,
            timeout=int(current_app.config.get("APISETU_TIMEOUT_SECONDS", 20)),
        )
    except requests.RequestException:
        raise APISetuError("apisetu_unavailable",
                          "API Setu could not be reached. Manual review remains available.", 502)

    digest = hashlib.sha256(response.content).hexdigest()
    if response.status_code != 200:
        # Do not forward provider response bodies: they may contain personal data.
        raise APISetuError("apisetu_provider_error",
                          "API Setu returned HTTP %s." % response.status_code,
                          502 if response.status_code >= 500 else 422)
    try:
        values = _flatten_xml(response.content)
    except (ET.ParseError, ValueError):
        # Some API deployments return JSON-wrapped/PDF content. It is not safe
        # to infer validity from HTTP 200 alone.
        return {
            "provider": "apisetu_morth",
            "endpoint": kind,
            "txn_id": txn_id,
            "response_sha256": digest,
            "provider_http_status": 200,
            "decision": "manual_review",
            "reason": "certificate_format_not_machine_comparable",
            "checked_at": datetime.now(timezone.utc),
        }

    status = (_pick(values, ("status", "certificateStatus", "dlStatus", "rcStatus")) or "").strip().upper()
    active = status in ("A", "ACTIVE", "VALID", "ISSUED")
    if kind == "drvlc":
        returned_number = _pick(values, ("dlno", "dlNumber", "licenceNumber", "licenseNumber", "certificateNumber", "number"))
        returned_name = _pick(values, ("holderName", "ownerName", "fullName", "name"))
        returned_dob = _pick(values, ("dob", "dateOfBirth", "birthDate"))
        expected = {
            "licence_number": bool(returned_number and _norm(returned_number) == _norm(parameters.get("dlno"))),
        }
        # Some MoRTH responses expose only the certificate metadata (number,
        # type and status); the API lookup itself was already constrained by
        # UID, full name and DOB. If holder fields are present, compare them too.
        if returned_name and _norm(returned_name) not in ("DRIVINGLICENSE", "DRVLC"):
            expected["holder_name"] = _norm(returned_name) == _norm(parameters.get("FullName"))
        if returned_dob:
            expected["date_of_birth"] = _date_norm(returned_dob) == _date_norm(parameters.get("DOB"))
    else:
        returned_number = _pick(values, ("reg_no", "regNo", "registrationNumber", "vehicleNumber", "certificateNumber", "number"))
        returned_chassis = _pick(values, ("chasis_no", "chassisNo", "chassisNumber", "chasisNumber"))
        returned_name = _pick(values, ("ownerName", "registeredOwner", "fullName", "name"))
        expected = {
            "registration_number": bool(returned_number and _norm(returned_number) == _norm(parameters.get("reg_no"))),
        }
        # The provider lookup is constrained by chassis number, UID and owner
        # name. Compare additional returned fields whenever the XML exposes them.
        if returned_chassis:
            expected["chassis_number"] = _norm(returned_chassis) == _norm(parameters.get("chasis_no"))
        if returned_name and _norm(returned_name) not in ("VEHICLEREGISTRATION", "RVCER"):
            expected["owner_name"] = _norm(returned_name) == _norm(parameters.get("FullName"))

    matched = active and all(expected.values())
    return {
        "provider": "apisetu_morth",
        "endpoint": kind,
        "txn_id": txn_id,
        "response_sha256": digest,
        "provider_http_status": 200,
        "provider_certificate_status": status or None,
        "field_matches": expected,
        "decision": "matched" if matched else "manual_review",
        "reason": None if matched else "status_or_identity_fields_not_confirmed",
        "checked_at": datetime.now(timezone.utc),
    }
