import re

import httpx
from fastapi.testclient import TestClient

from app import api, webpush
from app.db import ApiKey, Building, Message, SessionLocal
from app.main import app
from tests.test_push import decrypt, device


def test_webhook_api_keys_and_notify(monkeypatch):
    posted = []
    monkeypatch.setattr(webpush.httpx, "post",
                        lambda url, content=None, headers=None, timeout=None: posted.append((url, content)) or httpx.Response(201))
    api.key_throttle.hits.clear()
    api.bad_throttle.hits.clear()
    d2, d3, da = device(), device(), device()

    with TestClient(app) as c:
        c.post("/admin/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1"})
        c.post("/admin/parties/0", data={"name": "Familie Muster", "unit_id": "WE-001", "meters": "sensor.eg",
                                         "active": "1", "portal": "1"})
        c.post("/admin/parties/0", data={"name": "Familie Andere", "unit_id": "WE-002", "meters": "sensor.og",
                                         "active": "1", "portal": "1"})
        c.post("/setup", data={"username": "admin", "password": "admin-pass-1", "password2": "admin-pass-1"})
        c.post("/api/push/subscribe", json=da[2])
        for name, pid in (("muster", 2), ("andere", 3)):
            c.post("/admin/users/0", data={"username": name, "role": "tenant", "party_id": str(pid),
                                           "password": "mieter-pass", "active": "1"})
        c.get("/logout")
        for name, dev in (("muster", d2), ("andere", d3)):
            c.post("/login", data={"username": name, "password": "mieter-pass"})
            c.post("/api/push/subscribe", json=dev[2])
            assert c.get("/admin/api-keys", follow_redirects=False).status_code == 303  # Mieter: kein Zugang
            c.get("/logout")

        # ohne / mit falschem Schlüssel
        assert c.post("/api/v1/notify", json={"title": "x"}).status_code == 401
        assert c.post("/api/v1/notify", json={"title": "x"}, headers={"X-Api-Key": "imv_falsch"}).status_code == 401

        # Super-Admin legt Schlüssel an; er wird genau einmal angezeigt
        c.post("/login", data={"username": "admin", "password": "admin-pass-1"})
        page = c.post("/admin/api-keys", data={"name": "Home Assistant", "allow_admins": "1"}).text
        key = re.search(r"(imv_[A-Za-z0-9_-]{20,})", page).group(1)
        assert "nur dieses eine Mal" in page and "rest_command" in page and "n8n-nodes-base.httpRequest" in page
        assert key not in c.get("/admin/api-keys").text
        with SessionLocal() as s:
            assert s.query(ApiKey).one().key_hash != key  # nur Hash gespeichert
        c.get("/logout")
        H = {"X-Api-Key": key}

        assert c.get("/api/v1/ping", headers=H).json()["ok"]
        listing = c.get("/api/v1/parties", headers={"Authorization": f"Bearer {key}"}).json()
        assert {p["unit_id"] for p in listing["parties"]} >= {"WE-001", "WE-002"}

        # Unicast an WE-001 per Wohnungsnummer → nur Gerät von Familie Muster, Mitteilung gespeichert
        posted.clear()
        r = c.post("/api/v1/notify", headers=H, json={"title": "Paket angekommen", "body": "Liegt im Flur.",
                                                       "parties": ["WE-001"], "priority": "high"}).json()
        assert r["ok"] and r["push_ok"] == 1 and r["parties"] == ["Familie Muster"] and r["message_id"]
        assert [u for u, _ in posted] == [d2[2]["endpoint"]]
        assert decrypt(posted[0][1], d2[0], d2[1])["title"].endswith("Paket angekommen")
        with SessionLocal() as s:
            m = s.get(Message, r["message_id"])
            assert m.sender == "API: Home Assistant" and m.party_ids == [2] and m.priority == "high"

        # Broadcast an alle (Mieter + Verwalter), nur Push ohne Speichern
        posted.clear()
        r = c.post("/api/v1/notify", headers=H, json={"title": "Stromausfall", "audience": "all",
                                                       "persist": False}).json()
        assert r["message_id"] is None and r["push_ok"] == 2 and r["admin_push_ok"] == 1
        assert {u for u, _ in posted} == {d2[2]["endpoint"], d3[2]["endpoint"], da[2]["endpoint"]}

        # Validierung
        assert c.post("/api/v1/notify", headers=H, json={"body": "ohne Titel"}).status_code == 422
        assert c.post("/api/v1/notify", headers=H, json={"title": "x", "priority": "egal"}).status_code == 422
        assert c.post("/api/v1/notify", headers=H, json={"title": "x", "parties": ["WE-999"]}).status_code == 404
        assert c.post("/api/v1/notify", headers=H, json={"title": "x", "building": "GID-XX"}).status_code == 404

        # auf Gebäude beschränkter Schlüssel ohne Verwalter-Recht
        with SessionLocal() as s:
            other = Building(code="GID-02", name="Nebenhaus")
            s.add(other)
            s.commit()
            k2, raw2 = api.new_key(s, "n8n", [other.id], allow_admins=False)
        H2 = {"X-Api-Key": raw2}
        assert c.post("/api/v1/notify", headers=H2, json={"title": "x", "audience": "admins"}).status_code == 403
        assert c.post("/api/v1/notify", headers=H2, json={"title": "x", "building": "1"}).status_code == 403
        assert c.post("/api/v1/notify", headers=H2, json={"title": "x", "parties": ["WE-001"]}).status_code == 404
        r = c.post("/api/v1/notify", headers=H2, json={"title": "Nur Nebenhaus"}).json()
        assert r["push_devices"] == 0 and r["parties"] == []
        with SessionLocal() as s:  # „alle“ eines beschränkten Schlüssels ist kein globaler Broadcast
            assert s.get(Message, r["message_id"]).party_ids == [-1]

        # gesperrter Schlüssel
        c.post("/login", data={"username": "admin", "password": "admin-pass-1"})
        c.post(f"/admin/api-keys/{k2.id}/toggle")
        c.get("/logout")
        assert c.get("/api/v1/ping", headers=H2).status_code == 401
