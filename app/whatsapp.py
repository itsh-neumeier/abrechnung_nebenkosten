"""WhatsApp-Versand über n8n.

Die App kennt WhatsApp selbst nicht: Sie schickt je Partei einen Webhook an n8n (JSON mit Text,
Telefonnummer, PDF als Base64 und signiertem Download-Link). Der n8n-Flow stellt die Nachricht zu –
wahlweise über die Evolution API (selbst gehostet) oder die offizielle WhatsApp Business Cloud API –
und meldet das Ergebnis an ``/api/n8n/status`` zurück. Die fertigen Flows erzeugt :func:`flow`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Optional

import httpx

HEADER = "X-Abrechnung-Token"
WEBHOOK_PATH = "nebenkosten-whatsapp"
LINK_DAYS = 7
PROVIDERS = {
    "evolution": "Evolution API (selbst gehostet, Open Source)",
    "cloud": "WhatsApp Business Cloud API (Meta, offiziell)",
}


class WhatsAppError(Exception):
    pass


def configured(st: dict) -> bool:
    return bool(st.get("n8n_webhook_url"))


def ensure_secret(s, st: dict) -> str:
    """Gemeinsames Geheimnis App ↔ n8n; wird beim ersten Bedarf erzeugt."""
    if not st.get("n8n_secret"):
        from .db import save_settings

        st["n8n_secret"] = secrets.token_urlsafe(24)
        save_settings(s, {"n8n_secret": st["n8n_secret"]})
        s.commit()
    return st["n8n_secret"]


def normalize_phone(raw: str, country: str = "49") -> str:
    """'+49 151 234', '0049151234', '0151 234' → '49151234' (nur Ziffern, wie WhatsApp sie erwartet)."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:]
    if digits.startswith("0"):
        return country + digits[1:]
    return digits


def app_url(st: dict) -> str:
    from .config import config

    return (st.get("n8n_app_url") or config.app_base_url or "").strip().rstrip("/")


def sign(secret: str, bid: int, pid: int, exp: int) -> str:
    msg = f"{bid}:{pid}:{exp}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def verify(secret: str, bid: int, pid: int, exp: int, sig: str) -> bool:
    return bool(secret) and exp >= time.time() and hmac.compare_digest(sign(secret, bid, pid, exp), sig or "")


def pdf_link(st: dict, secret: str, bid: int, pid: int) -> str:
    exp = int(time.time()) + LINK_DAYS * 86400
    path = "test.pdf" if bid == 0 else f"invoice/{bid}/{pid}.pdf"
    return f"{app_url(st)}/api/n8n/{path}?exp={exp}&sig={sign(secret, bid, pid, exp)}"


def build_payload(st: dict, secret: str, *, bid: int, pid: int, name: str, unit_id: str, phone: str,
                  email: str, period: str, total: float, total_text: str, due: str, message: str,
                  filename: str, pdf: Optional[bytes], test: bool = False) -> dict:
    base = app_url(st)
    if not base:
        raise WhatsAppError("App-URL für n8n fehlt (WhatsApp / n8n → „App-URL aus Sicht von n8n“).")
    return {
        "event": "invoice.test" if test else "invoice.send",
        "test": test,
        "billing_id": bid,
        "party_id": pid,
        "name": name,
        "unit_id": unit_id,
        "phone": normalize_phone(phone),
        "email": email,
        "period": period,
        "total": total,
        "total_text": total_text,
        "due": due,
        "message": message,
        "filename": filename,
        "pdf_url": pdf_link(st, secret, bid, pid),
        "pdf_base64": base64.b64encode(pdf).decode() if (pdf is not None and st.get("n8n_pdf_base64")) else "",
        "status_url": f"{base}/api/n8n/status",
        "template": {"name": st.get("wa_template_name", ""), "language": st.get("wa_template_lang", "de"),
                     "params": [name, period, total_text]},
    }


def post(st: dict, secret: str, payload: dict, timeout: float = 20.0) -> str:
    url = st.get("n8n_webhook_url", "").strip()
    if not url:
        raise WhatsAppError("n8n-Webhook-URL ist nicht eingetragen (Menü WhatsApp / n8n).")
    if not payload.get("phone"):
        raise WhatsAppError("keine WhatsApp-Nummer hinterlegt")
    try:
        r = httpx.post(url, json=payload, headers={HEADER: secret}, timeout=timeout)
    except httpx.HTTPError as e:
        raise WhatsAppError(f"n8n nicht erreichbar: {e}") from e
    if r.status_code >= 400:
        hint = " – Flow in n8n aktiviert? (Test-URL /webhook-test/ nur bei „Listen for test event“)" \
            if r.status_code == 404 else ""
        raise WhatsAppError(f"n8n antwortet {r.status_code}{hint}")
    return "an n8n übergeben"


# --------------------------------------------------------------------------- n8n-Flows

def _node(name: str, type_: str, version: float, pos: tuple[int, int], params: dict, **extra) -> dict:
    node = {"parameters": params, "name": name, "type": type_, "typeVersion": version, "position": list(pos),
            "id": hashlib.md5(name.encode()).hexdigest()[:8] + "-0000-4000-8000-" + hashlib.md5(name.encode()).hexdigest()[:12]}
    node.update(extra)
    return node


def _set(name: str, pos, values: dict) -> dict:
    return _node(name, "n8n-nodes-base.set", 3.4, pos, {
        "mode": "manual",
        "assignments": {"assignments": [
            {"id": f"cfg{i}", "name": k, "value": v, "type": "string"} for i, (k, v) in enumerate(values.items())
        ]},
        "includeOtherFields": True,
        "options": {},
    })


def _http_json(name: str, pos, url: str, headers: dict, body_expr: str, **extra) -> dict:
    return _node(name, "n8n-nodes-base.httpRequest", 4.2, pos, {
        "method": "POST",
        "url": url,
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": k, "value": v} for k, v in headers.items()]},
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": body_expr,
        "options": {},
    }, **extra)


def _status_node(pos, provider: str, msg_id_expr: str) -> dict:
    body = ("={{ JSON.stringify({ billing_id: $('Webhook').item.json.body.billing_id, "
            "party_id: $('Webhook').item.json.body.party_id, ok: !$json.error, "
            "error: $json.error ? String($json.error.message || JSON.stringify($json.error)).slice(0, 300) : '', "
            f"provider: '{provider}', message_id: {msg_id_expr} }}) }}}}")
    return _http_json("Status an Abrechnung", pos, "={{ $('Webhook').item.json.body.status_url }}",
                      {HEADER: "={{ $('Webhook').item.json.headers['x-abrechnung-token'] }}"}, body)


def flow(provider: str, st: dict, secret: str) -> dict:
    """Fertiger n8n-Workflow (Import per Copy & Paste in den Editor oder „Import from File“)."""
    webhook = _node("Webhook", "n8n-nodes-base.webhook", 2, (0, 0), {
        "httpMethod": "POST", "path": WEBHOOK_PATH, "responseMode": "onReceived", "options": {},
    }, webhookId="5f0c7e1a-6b2d-4c8e-9a51-" + hashlib.md5(provider.encode()).hexdigest()[:12])
    check = _node("Token prüfen", "n8n-nodes-base.if", 2, (220, 0), {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
            "conditions": [{
                "id": "tok", "leftValue": "={{ $json.headers['x-abrechnung-token'] }}", "rightValue": secret,
                "operator": {"type": "string", "operation": "equals"},
            }],
            "combinator": "and",
        },
        "options": {},
    })
    on_error = {"onError": "continueRegularOutput"}
    if provider == "evolution":
        cfg = _set("Konfiguration", (440, 0), {
            "evolution_url": "http://evolution-api:8080",
            "instance": "nebenkosten",
            "apikey": "HIER-EVOLUTION-API-KEY-EINTRAGEN",
        })
        send = _http_json(
            "WhatsApp senden (Evolution)", (660, 0),
            "={{ $json.evolution_url }}/message/sendMedia/{{ $json.instance }}",
            {"apikey": "={{ $json.apikey }}"},
            "={{ JSON.stringify({ number: $json.body.phone, mediatype: 'document', mimetype: 'application/pdf', "
            "caption: $json.body.message, fileName: $json.body.filename, "
            "media: $json.body.pdf_base64 || $json.body.pdf_url }) }}",
            **on_error,
        )
        status = _status_node((880, 0), "evolution", "($json.key && $json.key.id) || ''")
        nodes = [webhook, check, cfg, send, status]
    elif provider == "cloud":
        cfg = _set("Konfiguration", (440, 0), {
            "graph_url": "https://graph.facebook.com/v21.0",
            "phone_number_id": "HIER-PHONE-NUMBER-ID-EINTRAGEN",
            "access_token": "HIER-ACCESS-TOKEN-EINTRAGEN",
            "template_name": st.get("wa_template_name") or "nebenkostenabrechnung",
            "template_lang": st.get("wa_template_lang") or "de",
        })
        get_pdf = _node("PDF laden", "n8n-nodes-base.httpRequest", 4.2, (660, 0), {
            "url": "={{ $json.body.pdf_url }}",
            "options": {"response": {"response": {"responseFormat": "file", "outputPropertyName": "data"}}},
        })
        upload = _node("PDF zu WhatsApp hochladen", "n8n-nodes-base.httpRequest", 4.2, (880, 0), {
            "method": "POST",
            "url": "={{ $('Konfiguration').item.json.graph_url }}/{{ $('Konfiguration').item.json.phone_number_id }}/media",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": "=Bearer {{ $('Konfiguration').item.json.access_token }}"}]},
            "sendBody": True,
            "contentType": "multipart-form-data",
            "bodyParameters": {"parameters": [
                {"name": "messaging_product", "value": "whatsapp"},
                {"name": "type", "value": "application/pdf"},
                {"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "data"},
            ]},
            "options": {},
        })
        send = _http_json(
            "Vorlage mit PDF senden", (1100, 0),
            "={{ $('Konfiguration').item.json.graph_url }}/{{ $('Konfiguration').item.json.phone_number_id }}/messages",
            {"Authorization": "=Bearer {{ $('Konfiguration').item.json.access_token }}"},
            "={{ JSON.stringify({ messaging_product: 'whatsapp', to: $('Webhook').item.json.body.phone, "
            "type: 'template', template: { name: $('Konfiguration').item.json.template_name, "
            "language: { code: $('Konfiguration').item.json.template_lang }, components: ["
            "{ type: 'header', parameters: [{ type: 'document', document: { id: $json.id, "
            "filename: $('Webhook').item.json.body.filename } }] }, "
            "{ type: 'body', parameters: $('Webhook').item.json.body.template.params"
            ".map(t => ({ type: 'text', text: String(t) })) }] } }) }}",
            **on_error,
        )
        status = _status_node((1320, 0), "cloud", "($json.messages && $json.messages[0].id) || ''")
        nodes = [webhook, check, cfg, get_pdf, upload, send, status]
    else:
        raise ValueError(provider)
    names = [n["name"] for n in nodes]
    connections = {}
    for a, b in zip(names, names[1:]):
        connections[a] = {"main": [[{"node": b, "type": "main", "index": 0}]] + ([[]] if a == "Token prüfen" else [])}
    return {
        "name": f"Nebenkostenabrechnung → WhatsApp ({'Evolution API' if provider == 'evolution' else 'Cloud API'})",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
        "pinData": {},
    }


def flow_json(provider: str, st: dict, secret: str) -> str:
    return json.dumps(flow(provider, st, secret), ensure_ascii=False, indent=2)
