"""Private document storage for KYC and vehicle papers.

Identity documents (Aadhaar/passport) and registration certificates are the most
sensitive data this service holds, so they get their own storage path with three
properties the generic avatar/document helpers deliberately do not have:

1. **Encrypted at rest.** Bytes are sealed with AES-256-GCM before they touch
   disk or S3, using `DOCUMENT_ENCRYPTION_KEY`. GCM is authenticated, so a
   tampered blob fails to open instead of decrypting to garbage. A fresh random
   nonce per document means identical files never produce identical ciphertext.
2. **Never publicly addressable.** Keys live under `private/` and are only ever
   returned through an authorization-checked endpoint. The public static route
   refuses that prefix outright, so even a routing mistake cannot expose them.
3. **Type identified by content, never by the client.** The stored MIME type
   comes from magic bytes; a declared filename only narrows the search, and a
   mismatch is a hard rejection rather than a silent trust.

Where the existing architecture supports encryption, it is used. When no key is
configured this module refuses to store an identity document outside
development, so a production deployment cannot end up with plaintext Aadhaar
images on a volume.
"""

import base64
import binascii
import json
import os

from flask import current_app

from . import storage
from .db import utcnow
from .errors import APIError

# Magic-byte -> (extension, canonical MIME). Deliberately narrow: a KYC
# document must be a real image or PDF, and nothing that merely *claims* to be.
_DETECTORS = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"%PDF", "pdf", "application/pdf"),
)

ALLOWED_MIME = frozenset(mime for _, _, mime in _DETECTORS)
IMAGE_MIME = frozenset(mime for _, _, mime in _DETECTORS if mime.startswith("image/"))

# Extensions a client may legitimately claim, used only to detect a lie.
_ALLOWED_EXT = frozenset({"png", "jpg", "jpeg", "pdf"})

# The private storage namespace. `storage.is_public_avatar` already refuses any
# path outside avatars/; this prefix additionally documents intent and lets
# `assert_private` fail closed if that function is ever weakened.
PRIVATE_PREFIX = "private"


# ------------------------------------------------------------------ encryption
def _key_material():
    """Return the 32-byte DEK, or None when encryption is not configured."""
    raw = (current_app.config.get("DOCUMENT_ENCRYPTION_KEY") or "").strip()
    if not raw:
        return None
    try:
        # Accept urlsafe-base64 (what most secret managers emit) or hex.
        if len(raw) == 64:
            candidate = binascii.unhexlify(raw)
        else:
            candidate = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, binascii.Error):
        raise APIError(
            "Document encryption key is malformed.", 503,
            code="document_crypto_unavailable")
    if len(candidate) != 32:
        raise APIError(
            "Document encryption key must decode to exactly 32 bytes.", 503,
            code="document_crypto_unavailable")
    return candidate


def encryption_enabled():
    return _key_material() is not None


def assert_encryption_available(kind="identity"):
    """Refuse to store an unencrypted private document when that is not allowed.

    Development keeps working without a key (so a contributor can run the KYC
    flow offline), but staging/production refuse outright: there is no
    "unencrypted KYC" mode in a real deployment.
    """
    if encryption_enabled():
        return True
    env = current_app.config.get("ENV", "development")
    if env == "development":
        return False
    raise APIError(
        "Document storage is not configured on the server.", 503,
        code="document_crypto_unavailable",
        details={"required": "DOCUMENT_ENCRYPTION_KEY", "document": kind})


def _aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(_key_material())


def seal(plaintext, aad):
    """Encrypt `plaintext`, binding it to `aad` (owner id + document kind).

    `aad` is authenticated but not encrypted: it stops a sealed blob from being
    moved to a different user's folder, because decryption then fails.
    """
    if not encryption_enabled():
        # Development-only passthrough. Prefixed so a plaintext blob can never
        # be mistaken for a sealed one on read.
        return b"PLAIN1" + plaintext
    nonce = os.urandom(12)
    blob = _aesgcm().encrypt(nonce, plaintext, aad.encode("utf-8"))
    return b"SEAL1" + nonce + blob


def unseal(blob, aad):
    if blob[:6] == b"PLAIN1":
        return blob[6:]
    if blob[:5] != b"SEAL1":
        raise APIError("Stored document is corrupt.", 500, code="document_corrupt")
    try:
        return _aesgcm().decrypt(blob[5:17], blob[17:], aad.encode("utf-8"))
    except Exception:  # noqa: BLE001 - wrong key, wrong owner, or tampering
        raise APIError("Stored document could not be opened.", 500,
                       code="document_corrupt")


def aad_for(user_id, kind, doc_id):
    """Associated data binding a sealed blob to exactly one document slot."""
    return json.dumps({"u": str(user_id), "k": str(kind), "d": str(doc_id)},
                      sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------ validation
def detect_type(data):
    """Identify a document by its content. Raises for anything unsupported."""
    header = data[:16]
    for magic, ext, mime in _DETECTORS:
        if header.startswith(magic):
            return ext, mime
    # WEBP needs a second magic at offset 8; checked separately so the common
    # cases stay a cheap prefix test.
    if header[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise APIError(
        "Unsupported document. Allowed: PDF, PNG, JPG, WEBP.", 422,
        code="invalid_file")


def validate_upload(data, declared_filename=None, *, max_mb=None, require_image=False):
    """Validate size, content type and declared extension. Returns (ext, mime).

    Three independent checks, because each catches a different failure:
    * size, so a large file cannot be used to exhaust disk;
    * magic bytes, so a script renamed `.pdf` is not stored as a PDF;
    * declared extension, so a file whose name lies about its type is rejected
      rather than quietly stored under the real type.
    """
    if not data:
        raise APIError("No document provided.", 422, code="no_file")

    limit_mb = max_mb
    if limit_mb is None:
        limit_mb = int(current_app.config.get("DOCUMENT_MAX_SIZE_MB", 8) or 8)
    max_bytes = limit_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise APIError("Document is too large. Maximum size is %d MB." % limit_mb,
                       413, code="file_too_large")

    ext, mime = detect_type(data)

    if require_image and mime not in IMAGE_MIME:
        raise APIError("This document must be an image (PNG, JPG or WEBP).", 422,
                       code="invalid_file")

    declared = (declared_filename or "").rpartition(".")[2].lower()
    if declared:
        normalised = "jpg" if declared in ("jpg", "jpeg") else declared
        if declared not in _ALLOWED_EXT and declared != "webp":
            raise APIError("Unsupported document type.", 422, code="invalid_file")
        if normalised != ext and not (normalised == "jpg" and ext == "jpg"):
            raise APIError("File extension does not match its contents.", 422,
                           code="invalid_file")
    return ext, mime


# --------------------------------------------------------------------- storage
def _key(user_id, kind, doc_id, ext):
    return f"{PRIVATE_PREFIX}/{kind}/{user_id}/{doc_id}.{ext}"


def store_private_document(user_id, kind, doc_id, data, declared_filename=None,
                           *, require_image=False, extra=None):
    """Seal and persist a private document. Returns the metadata to persist.

    `doc_id` should be a fresh random id, so the stored path never reveals
    ownership ordering and two uploads never collide.
    """
    assert_encryption_available(kind)
    ext, mime = validate_upload(data, declared_filename, require_image=require_image)
    key = _key(user_id, kind, doc_id, ext)
    sealed = seal(data, aad_for(user_id, kind, doc_id))
    storage.save_to_key(key, sealed, "application/octet-stream")
    meta = {
        "doc_id": doc_id,
        "key": key,
        "content_type": mime,
        "size": len(data),
        "encrypted": encryption_enabled(),
        "uploaded_at": utcnow(),
    }
    if extra:
        meta.update(extra)
    return meta


def load_private_document(user_id, kind, doc_id, key):
    """Read, authenticate and decrypt a private document.

    The owner id is part of the AAD, so a document sealed for one user cannot be
    read through another user's record even if the key were somehow known.
    """
    blob, _ctype, _name = storage.read_key(key)
    data = unseal(blob, aad_for(user_id, kind, doc_id))
    return data, mime_from_key(key)


def delete_private_document(key):
    if key and isinstance(key, str) and key.startswith(PRIVATE_PREFIX + "/"):
        storage.delete_key(key)


def mime_from_key(key):
    ext = (key or "").rpartition(".")[2].lower()
    for _magic, name, mime in _DETECTORS:
        if name == ext:
            return mime
    if ext == "webp":
        return "image/webp"
    return "application/octet-stream"


def is_private_key(key):
    return isinstance(key, str) and key.startswith(PRIVATE_PREFIX + "/")


def assert_private(key):
    """Belt-and-braces guard: a private key must never be served publicly."""
    if not is_private_key(key):
        raise APIError("Document not found.", 404, code="not_found")
    return key


def public_metadata(meta):
    """The safe projection of stored document metadata.

    Deliberately excludes `key` (the storage locator). Nothing outside this
    module should ever need it, and a leaked key plus a storage misconfiguration
    would bypass the authorization checks entirely.
    """
    if not meta:
        return None
    return {
        "doc_id": meta.get("doc_id"),
        "content_type": meta.get("content_type"),
        "size": meta.get("size"),
        "encrypted": bool(meta.get("encrypted")),
        "uploaded_at": iso(meta.get("uploaded_at")),
    }


def iso(value):
    from .timeutil import iso_utc

    return iso_utc(value)
