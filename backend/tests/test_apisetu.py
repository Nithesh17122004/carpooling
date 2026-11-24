"""Offline tests for the fail-closed API Setu response parser."""
from backend.apisetu import _consent_valid_shape, _flatten_xml, _date_norm, _norm


def _consent():
    return {
        "consent": {
            "consentId": "consent-id",
            "timestamp": "2026-11-24T07:37:47Z",
            "dataConsumer": {"id": "consumer"},
            "dataProvider": {"id": "provider"},
            "purpose": {"description": "Captain DL and RC verification"},
            "user": {"idType": "AADHAAR", "idNumber": "masked-or-authorized-id"},
            "data": {"id": "transport-certificate"},
            "permission": {
                "access": "RECEIVE",
                "dateRange": {"from": "2026-11-24T00:00:00Z",
                              "to": "2026-11-24T23:59:59Z"},
                "frequency": {"unit": "DAY", "value": 1, "repeats": 1},
            },
        },
        "signature": {"signature": "provider-issued-signed-value"},
    }


def test_consent_requires_signed_artifact_shape():
    assert _consent_valid_shape(_consent()) is True
    broken = _consent()
    broken["signature"]["signature"] = ""
    assert _consent_valid_shape(broken) is False


def test_xml_parser_extracts_attributes_and_text_without_assuming_pdf():
    xml = b'''<?xml version="1.0"?>
    <Certificate status="A" number="TN0119920009646">
      <IssuedTo><Person name="Sunil Kumar" dob="31-12-1980" /></IssuedTo>
    </Certificate>'''
    values = _flatten_xml(xml)
    assert values["status"] == "A"
    assert values["number"] == "TN0119920009646"
    assert values["name"] == "Sunil Kumar"
    assert values["dob"] == "31-12-1980"


def test_identity_normalizers_handle_common_separators():
    assert _norm("TN 01/1992-0009646") == "TN0119920009646"
    assert _date_norm("31-12-1980") == _date_norm("1980-12-31")
