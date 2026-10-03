import base64
import json
import time

import httpx
from fastapi.testclient import TestClient

from app import whatsapp
from app.main import app

BILL = {"period_start": "2026-09-01", "period_end": "2026-09-30", "grid_kwh": "100", "energy_cost_net": "25,00",
        "fixed_cost_net": "10", "spot_price_ct": "10", "vat_rate": "19", "battery_rate_ct": "8", "pv_rate_ct": "5"}
VALUES = {"val__sensor.grid": "100", "val__sensor.total": "300", "val__sensor.eg": "120"}
APP = "http://abrechnung.test:8000"


def test_normalize_phone():
    assert whatsapp.normalize_phone("+49 151 2345-678") == "491512345678"
    assert whatsapp.normalize_phone("0049 151 2345678") == "491512345678"
    assert whatsapp.normalize_phone("0151 2345678") == "491512345678"
    assert whatsapp.normalize_phone("") == ""


def test_sign_verify():
    exp = int(time.time()) + 60
    sig = whatsapp.sign("geheim", 3, 2, exp)
    assert whatsapp.verify("geheim", 3, 2, exp, sig)
    assert not whatsapp.verify("geheim", 3, 1, exp, sig)
    assert not whatsapp.verify("anders", 3, 2, exp, sig)
    old = int(time.time()) - 1
    assert not whatsapp.verify("geheim", 3, 2, old, whatsapp.sign("geheim", 3, 2, old))


def test_flows_are_valid_n8n_workflows():
    st = {"wa_template_name": "nebenkostenabrechnung", "wa_template_lang": "de"}
    for provider in whatsapp.PROVIDERS:
        wf = json.loads(whatsapp.flow_json(provider, st, "TOKEN123"))
        names = [n["name"] for n in wf["nodes"]]
        assert names[0] == "Webhook" and names[-1] == "Status an Abrechnung"
        assert len(set(names)) == len(names) and len({n["id"] for n in wf["nodes"]}) == len(names)
        assert wf["nodes"][0]["parameters"]["path"] == whatsapp.WEBHOOK_PATH
        assert "TOKEN123" in json.dumps(wf)
        for src, conn in wf["connections"].items():  # jede Verbindung zeigt auf einen vorhandenen Knoten
            assert src in names
            for out in conn["main"]:
                for c in out:
                    assert c["node"] in names
        # alle Knoten außer dem letzten sind verbunden
        assert set(wf["connections"]) == set(names[:-1])


def test_whatsapp_send_and_status(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        return httpx.Response(200, json={"message": "Workflow was started"})

    monkeypatch.setattr(whatsapp.httpx, "post", fake_post)

    with TestClient(app) as c:
        c.post("/settings", data={"entity_grid": "sensor.grid", "entity_total": "sensor.total",
                                  "landlord_name": "Max Vermieter", "vat_rate": "19"})
        c.post("/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1"})
        c.post("/parties/0", data={"name": "Familie Muster", "unit_id": "WE-001", "meters": "sensor.eg",
                                   "active": "1", "phone": "0151 2345678", "channel": "whatsapp"})
        assert "💬 0151 2345678" in c.get("/parties").text

        page = c.get("/whatsapp").text
        assert "Flow kopieren" in page and "Evolution API" in page and "Cloud API" in page
        c.post("/whatsapp", data={"n8n_webhook_url": "http://n8n.test/webhook/x", "n8n_app_url": APP + "/",
                                  "wa_provider": "evolution", "n8n_pdf_base64": "1",
                                  "wa_message": "Hallo {name}, {betrag} bis {faellig}",
                                  "wa_template_name": "nebenkostenabrechnung", "wa_template_lang": "de"})
        flow = c.get("/whatsapp/flow/cloud.json")
        assert flow.status_code == 200 and json.loads(flow.text)["nodes"][0]["type"] == "n8n-nodes-base.webhook"

        r = c.post("/billings", data=BILL, follow_redirects=False)
        url = r.headers["location"].split("?")[0]
        bid = int(url.rsplit("/", 1)[1])
        c.post(url, data={"action": "save", **BILL, **VALUES})
        page = c.post(url, data={"action": "finalize", **BILL, **VALUES}).text
        assert "abgeschlossen" in page

        c.post(url, data={"action": "send_all"})
        assert len(calls) == 1
        hook, payload, headers = calls[0]
        assert hook == "http://n8n.test/webhook/x"
        secret = headers[whatsapp.HEADER]
        assert payload["phone"] == "491512345678" and payload["name"] == "Familie Muster"
        assert payload["message"].startswith("Hallo Familie Muster, ") and " bis " in payload["message"]
        assert base64.b64decode(payload["pdf_base64"])[:4] == b"%PDF"
        assert payload["filename"] == "Nebenkostenabrechnung_2026-09_WE-001.pdf"
        assert payload["status_url"] == APP + "/api/n8n/status"
        assert payload["template"]["params"][0] == "Familie Muster"
        assert "💬 an n8n übergeben" in c.get(url).text

        # n8n lädt das PDF über den signierten Link (ohne Login)
        path = payload["pdf_url"][len(APP):]
        assert c.get(path).content[:4] == b"%PDF"
        assert c.get(path.replace("sig=", "sig=x")).status_code == 403

        # Rückmeldung aus n8n
        status = {"billing_id": bid, "party_id": payload["party_id"], "ok": True, "provider": "evolution"}
        assert c.post("/api/n8n/status", json=status).status_code == 403
        assert c.post("/api/n8n/status", json=status, headers={whatsapp.HEADER: secret}).json()["ok"]
        assert "💬 zugestellt" in c.get(url).text

        # Test-Nachricht inkl. Rückmeldung
        c.post("/whatsapp/test", data={"phone": "+49 170 1111111"})
        assert calls[-1][1]["test"] and calls[-1][1]["phone"] == "491701111111"
        assert c.get(calls[-1][1]["pdf_url"][len(APP):]).content[:4] == b"%PDF"
        c.post("/api/n8n/status", json={"billing_id": 0, "party_id": 0, "ok": False, "error": "nicht registriert",
                                        "provider": "cloud"}, headers={whatsapp.HEADER: secret})
        assert "nicht registriert" in c.get("/whatsapp").text


def test_native_cloud_api(monkeypatch):
    """Direkter Versand über die Cloud API inkl. Statusmeldung über den Meta-Webhook."""
    import hashlib
    import hmac

    import dataclasses

    from app import main, wa_cloud
    from app.config import config

    cfg = dataclasses.replace(config, wa_token="TOK", wa_phone_number_id="PNID", wa_app_secret="APPSECRET")
    monkeypatch.setattr(wa_cloud, "config", cfg)
    monkeypatch.setattr(main, "config", cfg)
    graph = []

    def fake_post(url, headers=None, data=None, files=None, json=None, timeout=None):
        graph.append({"url": url, "auth": headers.get("Authorization"), "data": data, "files": files, "json": json})
        if url.endswith("/media"):
            assert files["file"][1][:4] == b"%PDF" and files["file"][2] == "application/pdf"
            return httpx.Response(200, json={"id": "MEDIA1"})
        if json["to"] == "4900000000":
            return httpx.Response(400, json={"error": {"message": "Template name does not exist", "code": 132001}})
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(graph)}"}]})

    def fake_get(url, headers=None, params=None, timeout=None):
        return httpx.Response(200, json={"verified_name": "Hausverwaltung", "display_phone_number": "+49 9545 1",
                                         "quality_rating": "GREEN"})

    monkeypatch.setattr(wa_cloud.httpx, "post", fake_post)
    monkeypatch.setattr(wa_cloud.httpx, "get", fake_get)

    with TestClient(app) as c:
        c.post("/settings", data={"entity_grid": "sensor.grid", "entity_total": "sensor.total", "vat_rate": "19"})
        c.post("/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1"})
        c.post("/parties/0", data={"name": "Familie Muster", "unit_id": "WE-001", "meters": "sensor.eg",
                                   "active": "1", "phone": "+49 151 2345678", "channel": "whatsapp"})
        c.post("/whatsapp", data={"wa_mode": "native", "wa_template_name": "nebenkostenabrechnung",
                                  "wa_template_lang": "de"})
        page = c.get("/whatsapp").text
        assert "✔ eingerichtet" in page and "PNID" in page
        assert "Hausverwaltung" in c.post("/whatsapp/check").text

        r = c.post("/billings", data=BILL, follow_redirects=False)
        url = r.headers["location"].split("?")[0]
        c.post(url, data={"action": "save", **BILL, **VALUES})
        c.post(url, data={"action": "finalize", **BILL, **VALUES})
        c.post(url, data={"action": "send_all"})
        upload, msg = graph[0], graph[1]
        assert upload["url"] == "https://graph.facebook.com/v21.0/PNID/media" and upload["auth"] == "Bearer TOK"
        tpl = msg["json"]["template"]
        assert msg["json"]["to"] == "491512345678" and tpl["name"] == "nebenkostenabrechnung"
        assert tpl["components"][0]["parameters"][0]["document"] == {
            "id": "MEDIA1", "filename": "Nebenkostenabrechnung_2026-09_WE-001.pdf"}
        assert [p["text"] for p in tpl["components"][1]["parameters"]][0] == "Familie Muster"
        assert "💬 gesendet" in c.get(url).text

        # Webhook-Einrichtung (Verify) und Statusmeldungen von Meta
        verify = c.get("/whatsapp").text.split("Prüf-Token")[1].split("<code>")[1].split("</code>")[0]
        ok = c.get("/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": verify,
                                                    "hub.challenge": "1234"})
        assert ok.status_code == 200 and ok.text == "1234"
        assert c.get("/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                                      "hub.challenge": "1"}).status_code == 403

        def hook(status, wamid="wamid.2", **extra):
            body = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
                "statuses": [{"id": wamid, "status": status, "recipient_id": "491512345678", **extra}]}}]}]}).encode()
            sig = "sha256=" + hmac.new(b"APPSECRET", body, hashlib.sha256).hexdigest()
            return c.post("/api/whatsapp/webhook", content=body,
                          headers={"x-hub-signature-256": sig, "content-type": "application/json"})

        assert c.post("/api/whatsapp/webhook", content=b"{}", headers={"x-hub-signature-256": "sha256=00"}).status_code == 403
        assert hook("read").json()["matched"] == 1
        assert hook("delivered").json()["matched"] == 1  # verspätet – „gelesen“ bleibt
        assert "💬 gelesen" in c.get(url).text
        hook("failed", errors=[{"code": 131026, "title": "Message undeliverable"}])
        page = c.get(url).text
        assert "💬 Fehler" in page and "Message undeliverable" in page

        # Testnachricht direkt; Fehler der API wird lesbar angezeigt
        assert "gesendet" in c.post("/whatsapp/test", data={"phone": "+49 170 1111111"}).text
        assert "Vorlage existiert nicht" in c.post("/whatsapp/test", data={"phone": "+49 00000000"}).text
