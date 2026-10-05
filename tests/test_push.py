import base64
import json

import httpx
from cryptography.hazmat.primitives import hashes, hmac as _hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from app import webpush
from app.main import app

BILL = {"period_start": "2026-09-01", "period_end": "2026-09-30", "grid_kwh": "100", "energy_cost_net": "25,00",
        "fixed_cost_net": "10", "spot_price_ct": "10", "vat_rate": "19", "battery_rate_ct": "8", "pv_rate_ct": "5"}
VALUES = {"val__sensor.grid": "100", "val__sensor.total": "300", "val__sensor.eg": "120"}
AUTH = b"0123456789abcdef"


def h(key, data):
    m = _hmac.HMAC(key, hashes.SHA256())
    m.update(data)
    return m.finalize()


def device():
    """Simuliertes Browser-Gerät: Schlüsselpaar + Abo-JSON wie von pushManager.subscribe()."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    sub = {"endpoint": f"https://push.example.test/{webpush.b64u(pub[:9])}",
           "keys": {"p256dh": webpush.b64u(pub), "auth": webpush.b64u(AUTH)}}
    return priv, pub, sub


def decrypt(body: bytes, priv, pub: bytes) -> dict:
    """Empfängerseite nach RFC 8291 (so entschlüsselt der Browser)."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_pub, ct = body[21:21 + idlen], body[21 + idlen:]
    assert rs == 4096 and idlen == 65
    ecdh = priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = h(h(AUTH, ecdh), b"WebPush: info\x00" + pub + as_pub + b"\x01")
    prk = h(salt, ikm)
    plain = AESGCM(h(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]).decrypt(
        h(prk, b"Content-Encoding: nonce\x00\x01")[:12], ct, None)
    assert plain.endswith(b"\x02")
    return json.loads(plain[:-1])


def test_encrypt_and_vapid():
    priv, pub, sub = device()
    body = webpush.encrypt(json.dumps({"title": "Hallo ✓"}).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    assert decrypt(body, priv, pub) == {"title": "Hallo ✓"}

    key = webpush.generate_vapid()
    header = webpush.vapid_header(key, "https://fcm.googleapis.com/fcm/send/x", "mailto:a@b.de")
    token, k = header[len("vapid t="):].split(", k=")
    head, claims, sig = token.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    raw = pad(sig)
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pad(k)).verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert json.loads(pad(claims))["aud"] == "https://fcm.googleapis.com"
    assert k == webpush.public_key_b64(key)


def test_push_on_publish(monkeypatch):
    posted = []
    gone = set()

    def fake_post(url, content=None, headers=None, timeout=None):
        posted.append((url, content, headers))
        return httpx.Response(410 if url in gone else 201)

    monkeypatch.setattr(webpush.httpx, "post", fake_post)
    priv, pub, sub = device()

    with TestClient(app) as c:
        c.post("/admin/settings", data={"entity_grid": "sensor.grid", "entity_total": "sensor.total", "vat_rate": "19"})
        c.post("/admin/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1"})
        c.post("/admin/parties/0", data={"name": "Familie Muster", "unit_id": "WE-001", "meters": "sensor.eg",
                                   "active": "1", "portal": "1"})
        c.post("/setup", data={"username": "admin", "password": "admin-pass-1", "password2": "admin-pass-1"})
        c.post("/admin/users/0", data={"username": "muster", "role": "tenant", "party_id": "2", "password": "mieter-pass",
                                 "active": "1"})
        r = c.post("/admin/billings", data=BILL, follow_redirects=False)
        url = r.headers["location"].split("?")[0]
        c.post(url, data={"action": "save", **BILL, **VALUES})
        c.get("/logout")

        # ohne Login kein Abo
        assert c.post("/api/push/subscribe", json=sub).status_code == 401
        # Mieter meldet sein Gerät an
        c.post("/login", data={"username": "muster", "password": "mieter-pass"})
        cfg = c.get("/api/push/config").json()
        assert len(webpush.unb64u(cfg["publicKey"])) == 65 and cfg["devices"] == 0
        assert c.post("/api/push/subscribe", json={"endpoint": "http://unsicher", "keys": {}}).status_code == 400
        assert c.post("/api/push/subscribe", json=sub).json()["ok"]
        assert c.get("/api/push/config").json()["devices"] == 1
        assert "push-card" in c.get("/account").text and "push-card" in c.get("/").text
        # Test-Benachrichtigung an das eigene Gerät
        assert c.post("/api/push/test").json()["ok"] == 1
        assert decrypt(posted[-1][1], priv, pub)["title"] == "Test-Benachrichtigung"
        assert posted[-1][2]["Authorization"].startswith("vapid t=") and posted[-1][2]["Content-Encoding"] == "aes128gcm"
        c.get("/logout")

        # Verwalter schließt ab → automatisch veröffentlicht → Mieter bekommt Push
        c.post("/login", data={"username": "admin", "password": "admin-pass-1"})
        posted.clear()
        page = c.post(url, data={"action": "finalize", **BILL, **VALUES}).text
        assert "1 Mieter per Push benachrichtigt" in page
        assert len(posted) == 1 and posted[0][0] == sub["endpoint"]
        msg = decrypt(posted[0][1], priv, pub)
        assert msg["title"] == "Neue Nebenkostenabrechnung 09/2026" and "Familie Muster" in msg["body"]
        assert msg["url"].startswith("/portal/")

        # erneut veröffentlichen → keine doppelte Benachrichtigung
        c.post(url, data={"action": "unpublish"})
        c.post(url, data={"action": "publish"})
        assert len(posted) == 1

        # abgelaufenes Abo (410) wird entfernt
        gone.add(sub["endpoint"])
        from app.db import PushSubscription, SessionLocal
        from app import notify
        with SessionLocal() as s:
            ok, errors = notify.send_to(s, s.query(PushSubscription).all(), {"title": "x"})
            assert ok == 0 and errors and s.query(PushSubscription).count() == 0


def test_admin_notifications_with_amount_and_global_switches(monkeypatch):
    import asyncio
    import smtplib

    from app import service
    from app.db import SessionLocal
    from tests.test_import import AWATTAR_TEXT, make_pdf

    posted = []
    monkeypatch.setattr(webpush.httpx, "post",
                        lambda url, content=None, headers=None, timeout=None: posted.append(content) or httpx.Response(201))

    class FailSMTP:
        def __init__(self, *a, **kw):
            raise ConnectionRefusedError("Verbindung abgelehnt")

    monkeypatch.setattr(smtplib, "SMTP", FailSMTP)
    priv, pub, sub = device()

    with TestClient(app) as c:
        c.post("/setup", data={"username": "admin", "password": "admin-pass-1", "password2": "admin-pass-1"})
        assert c.post("/api/push/subscribe", json=sub).json()["ok"]
        page = c.get("/admin/users").text
        assert "Benachrichtigungen (global)" in page and "neue Stromrechnung eingegangen" in page

        # Eingang per Postfach → Verwalter bekommt Betrag, Zeitraum und kWh
        with SessionLocal() as s:
            b, _ = asyncio.run(service.import_invoice(s, make_pdf(), "r.pdf", "<m1@x>", source="E-Mail „Rechnung“"))
            bid = b.id
        msg = decrypt(posted[-1], priv, pub)
        assert msg["title"].startswith("Neue Stromrechnung: ") and msg["title"].endswith("€")
        assert "01.08.2026" in msg["body"] and "74,8 kWh" in msg["body"] and msg["url"] == f"/admin/billings/{bid}"

        # Versand schlägt fehl → Fehler-Benachrichtigung
        c.post("/admin/parties/0", data={"name": "Familie Muster", "meters": "sensor.eg", "active": "1", "email": "m@test.de"})
        with SessionLocal() as s:
            from app.db import Billing
            b = s.get(Billing, bid)
            b.result = service.recompute(s, b)
            b.status = "final"
            s.commit()
            service.send_invoices(s, b)
        err = decrypt(posted[-1], priv, pub)
        assert "Fehler" in err["title"] and "Familie Muster" in err["body"]

        # global abgeschaltet → keine Benachrichtigung mehr beim Eingang
        c.post("/admin/users/settings", data={"push_form": "1", "push_tenant_published": "1"})
        n = len(posted)
        with SessionLocal() as s:
            asyncio.run(service.import_invoice(s, make_pdf(AWATTAR_TEXT.replace("2026000001", "2026000009")),
                                               "r2.pdf", "<m2@x>", source="E-Mail „Rechnung“"))
        assert len(posted) == n
