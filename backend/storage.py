"""File storage abstraction (local disk or S3-compatible) + magic-byte checks.

Layout (local backend):
    avatars/<user_id>/<random>.<ext>   -> public (served via /uploads/avatars/...)
    docs/   /<user_id>/<random>.<ext>  -> PRIVATE (never served by a static route;
                                          fetched through an authorized endpoint)

Document files are stored under random keys and never returned to non-owners.
"""

import os
from pathlib import Path

from .config import UPLOAD_DIR
from .errors import APIError

_MAGIC = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "image/webp": b"RIFF",
    "application/pdf": b"%PDF",
}
_EXT_BY_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
    b"RIFF": ("webp", "image/webp"),
    b"%PDF": ("pdf", "application/pdf"),
}
MAX_PREVIEW = 16


def _backend():
    from .config import Config

    return Config.STORAGE_BACKEND or "local"


def _bucket():
    from .config import Config

    return Config.STORAGE_BUCKET


def _s3_client():
    from .config import Config

    if not (Config.STORAGE_ACCESS_KEY and Config.STORAGE_SECRET_KEY and Config.STORAGE_BUCKET):
        return None
    try:
        import boto3
    except ImportError:
        return None
    kwargs = {}
    if Config.STORAGE_ENDPOINT:
        kwargs["endpoint_url"] = Config.STORAGE_ENDPOINT
    if Config.STORAGE_REGION:
        kwargs["region_name"] = Config.STORAGE_REGION
    return boto3.client("s3", aws_access_key_id=Config.STORAGE_ACCESS_KEY,
                        aws_secret_access_key=Config.STORAGE_SECRET_KEY, **kwargs)


def detect_extension(data, declared):
    header = data[:MAX_PREVIEW]
    if header[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp", "image/webp"
    if header[:4] == b"%PDF":
        return "pdf", "application/pdf"
    raise APIError("Unsupported file type. Allowed: PNG, JPG, WEBP, PDF.", 422, code="invalid_file")


def save_to_key(key, data, content_type):
    if _backend() == "s3":
        client = _s3_client()
        if client is None:
            raise APIError("Storage is misconfigured.", 503, code="storage_unconfigured")
        client.put_object(Bucket=_bucket(), Key=key, Body=data, ContentType=content_type)
        return True
    target = UPLOAD_DIR / key
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as fh:
        fh.write(data)
    return True


def read_key(key):
    """Returns (bytes, content_type, filename)."""
    if _backend() == "s3":
        client = _s3_client()
        if client is None:
            raise APIError("Storage is misconfigured.", 503, code="storage_unconfigured")
        obj = client.get_object(Bucket=_bucket(), Key=key)
        body = obj["Body"].read()
        ctype = obj.get("ContentType", "application/octet-stream")
        return body, ctype, os.path.basename(key)
    target = UPLOAD_DIR / key
    if not target.is_file():
        raise APIError("File not found.", 404, code="not_found")
    ext = target.suffix.lstrip(".").lower() or "bin"
    ctype = {  # noqa: E501
        "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
        "jpeg": "image/jpeg", "webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return target.read_bytes(), ctype, target.name


def delete_key(key):
    if _backend() == "s3":
        client = _s3_client()
        if client is None:
            return
        try:
            client.delete_object(Bucket=_bucket(), Key=key)
        except Exception:  # noqa: BLE001 - best effort
            pass
        return
    try:
        target = UPLOAD_DIR / key
        if target.is_file():
            os.remove(target)
    except OSError:
        pass


def avatar_key(user_id, ext):
    return f"avatars/{user_id}/{os.urandom(9).hex()}.{ext}"


def document_key(user_id, directory, ext):
    return f"docs/{directory}/{user_id}/{os.urandom(12).hex()}.{ext}"


def public_avatar_url(key):
    if not key:
        return ""
    if _backend() == "s3":
        return f"/api/uploads/avatar?key={key}"
    return f"/uploads/{key}"


def is_public_avatar(filename):
    """Only files under avatars/ are servable by the public static route."""
    return isinstance(filename, str) and filename.startswith("avatars/") and ".." not in filename