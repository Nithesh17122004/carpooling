"""Vehicle-document privacy, upload validation, and authorization checks."""

import base64
import io

from backend.tests.conftest import _make_user, make_ride

# 1x1 valid PNG (Pillow can decode it)
_ONE_PX_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _png():
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 500


def _upload(client, auth, vid, name="doc.png", data=None):
    return client.post("/api/uploads/vehicle-doc",
                       headers=auth,
                       data={"vehicle_id": vid,
                             "file": (io.BytesIO(data if data is not None else _png()), name)},
                       content_type="multipart/form-data")


def test_valid_doc_upload_and_authorized_download(client, db, driver, rider, vehicle):
    r = _upload(client, driver["auth"], vehicle["id"])
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "uploaded"

    dl = client.get(f"/api/uploads/vehicle-doc/{vehicle['id']}/dl", headers=driver["auth"])
    assert dl.status_code == 200
    assert dl.data.startswith(b"\x89PNG")


def test_invalid_text_rejected(client, db, driver, rider, vehicle):
    r = _upload(client, driver["auth"], vehicle["id"], name="evil.txt", data=b"<script>x</script>")
    assert r.status_code == 422
    assert r.get_json()["error"]["code"] == "invalid_file"


def test_kind_invalid_rejected(client, db, driver, rider, vehicle):
    r = _upload(client, driver["auth"], vehicle["id"], name="doc.png")
    r = client.post("/api/uploads/vehicle-doc",
                    headers=driver["auth"],
                    data={"vehicle_id": vehicle["id"], "doc": "passport",
                          "file": (io.BytesIO(_png()), "doc.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 422


def test_owner_can_download_other_blocked(client, db, driver, rider, vehicle):
    assert _upload(client, driver["auth"], vehicle["id"]).status_code == 200
    url = f"/api/uploads/vehicle-doc/{vehicle['id']}/dl"
    assert client.get(url, headers=driver["auth"]).status_code == 200
    assert client.get(url, headers=rider["auth"]).status_code == 404
    assert client.get(url).status_code != 200


def test_documents_not_served_by_public_uploads(client, db, driver, rider, vehicle):
    assert _upload(client, driver["auth"], vehicle["id"]).status_code == 200
    # the legacy object-store style public URL must not resolve to documents
    assert client.get(f"/uploads/{vehicle['id']}/dl").status_code == 404
    assert client.get(f"/uploads/vehicle-doc/{vehicle['id']}/dl").status_code == 404


def test_vehicle_owner_check(client, db, driver, rider, vehicle):
    # uploading to someone else's vehicle must 404
    r = _upload(client, rider["auth"], vehicle["id"])
    assert r.status_code == 404


def test_avatar_upload_public_but_scoped(client, db, driver, rider, vehicle):
    r = client.post("/api/profile/avatar",
                    headers=rider["auth"],
                    data={"file": (io.BytesIO(_ONE_PX_PNG), "me.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    url = r.get_json()["user"]["photo_url"]
    assert "avatar" in url or "avatars" in url
    # avatars ARE public (no auth header needed)
    assert client.get(url).status_code == 200
    # but a document key must NOT resolve as an avatar
    assert client.get("/api/uploads/avatar?key=docs/whatever.pdf").status_code == 404