"""Web-Push-Benachrichtigungen (PWA) ohne Fremddienst-Konto.

Verschlüsselung nach RFC 8291 (``aes128gcm``), Absender-Nachweis per VAPID (RFC 8292, ES256-JWT).
Die VAPID-Schlüssel erzeugt die App selbst und speichert sie in der Datenbank (oder VAPID_PRIVATE_KEY
in der Umgebung). Zugestellt wird über den Push-Dienst des Browsers (bei Chrome/Android: Google FCM) –
der Inhalt ist Ende-zu-Ende verschlüsselt, der Dienst sieht ihn nicht.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("webpush")
RECORD_SIZE = 4096


class PushError(Exception):
    def __init__(self, msg: str, gone: bool = False):
        super().__init__(msg)
        self.gone = gone  # Abo existiert nicht mehr (404/410) → löschen


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# --------------------------------------------------------------------------- VAPID-Schlüssel
def generate_vapid() -> str:
    """Neuer privater Schlüssel (P-256) als Base64url der 32 Rohbytes."""
    priv = ec.generate_private_key(ec.SECP256R1())
    return b64u(priv.private_numbers().private_value.to_bytes(32, "big"))


def load_private(raw_b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64u(raw_b64), "big"), ec.SECP256R1())


def public_key_b64(raw_b64: str) -> str:
    """Öffentlicher Schlüssel für ``pushManager.subscribe({applicationServerKey})``."""
    return b64u(_public_bytes(load_private(raw_b64).public_key()))


def vapid_header(raw_b64: str, endpoint: str, subject: str, ttl_hours: int = 12) -> str:
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + ttl_hours * 3600,
              "sub": subject}
    signing = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." + \
        b64u(json.dumps(claims, separators=(",", ":")).encode())
    der = load_private(raw_b64).sign(signing.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    token = signing + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token}, k={public_key_b64(raw_b64)}"


# --------------------------------------------------------------------------- Verschlüsselung (RFC 8291)
def encrypt(payload: bytes, p256dh_b64: str, auth_b64: str, salt: Optional[bytes] = None,
            server_key: Optional[ec.EllipticCurvePrivateKey] = None) -> bytes:
    ua_public = unb64u(p256dh_b64)
    auth_secret = unb64u(auth_b64)
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise PushError("Nachricht zu lang für eine Push-Benachrichtigung")
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(server_key.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh = server_key.exchange(ec.ECDH(), ua_key)
    # IKM = HKDF(auth_secret, ecdh, "WebPush: info" || 0 || ua_public || as_public, 32)
    prk_key = _hmac(auth_secret, ecdh)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # 0x02 = letzter Datensatz
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


def send(sub: dict, data: dict, vapid_private: str, subject: str, ttl: int = 86400,
         urgency: str = "normal", timeout: float = 15.0) -> int:
    """Eine Benachrichtigung an ein Abo ``{endpoint, keys: {p256dh, auth}}`` schicken."""
    body = encrypt(json.dumps(data, ensure_ascii=False).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    headers = {
        "Authorization": vapid_header(vapid_private, sub["endpoint"], subject),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": urgency,
    }
    try:
        r = httpx.post(sub["endpoint"], content=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise PushError(f"Push-Dienst nicht erreichbar: {e}") from e
    if r.status_code in (404, 410):
        raise PushError("Abo abgelaufen", gone=True)
    if r.status_code >= 400:
        raise PushError(f"Push-Dienst antwortet {r.status_code}: {r.text[:200]}")
    return r.status_code
