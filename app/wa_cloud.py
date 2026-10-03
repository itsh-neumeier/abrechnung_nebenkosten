"""Direkter Versand über die offizielle WhatsApp Business Cloud API (Meta Graph API).

Ablauf je Abrechnung: PDF hochladen (``/{phone-number-id}/media``) → genehmigte Vorlage mit dem PDF als
Dokument-Kopfzeile senden (``/{phone-number-id}/messages``). Rechnungen gehen meist außerhalb des
24-Stunden-Fensters raus, deshalb immer als Vorlage. Zustellstatus (sent/delivered/read/failed) kommt
über den Webhook ``/api/whatsapp/webhook`` (Signatur mit dem App-Geheimnis).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

import httpx

from .config import config

STATUS_DE = {"sent": "gesendet", "delivered": "zugestellt", "read": "gelesen", "failed": "Fehler"}
_RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 9}


class CloudError(Exception):
    pass


def configured() -> bool:
    return bool(config.wa_token and config.wa_phone_number_id)


def _error(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        msg = err.get("error_user_msg") or err.get("message") or r.text
        details = (err.get("error_data") or {}).get("details")
        code = err.get("code")
        hint = ""
        if code == 132001:
            hint = " – Vorlage existiert nicht/ist nicht genehmigt (Name und Sprache prüfen)"
        elif code in (190, 10):
            hint = " – Zugriffstoken ungültig/abgelaufen oder ohne Berechtigung (WA_TOKEN)"
        elif code == 131030:
            hint = " – Empfängernummer ist im Testmodus nicht als Empfänger freigeschaltet"
        return f"{msg}{' (' + details + ')' if details else ''} [Code {code}]{hint}"
    except Exception:  # noqa: BLE001
        return f"HTTP {r.status_code}: {r.text[:200]}"


class CloudClient:
    def __init__(self, token: str = "", phone_number_id: str = "", version: str = "", base: str = "",
                 timeout: float = 30.0):
        self.token = token or config.wa_token
        self.phone_id = phone_number_id or config.wa_phone_number_id
        if not (self.token and self.phone_id):
            raise CloudError("WhatsApp Cloud API nicht eingerichtet (WA_TOKEN / WA_PHONE_NUMBER_ID in .env / Portainer).")
        self.base = f"{(base or config.wa_graph_url).rstrip('/')}/{version or config.wa_api_version}"
        self.timeout = timeout

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _check(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            raise CloudError(_error(r))
        return r.json()

    def info(self) -> dict:
        """Nummer und Anzeigename (Verbindungstest)."""
        r = httpx.get(f"{self.base}/{self.phone_id}", headers=self._auth, timeout=self.timeout,
                      params={"fields": "display_phone_number,verified_name,quality_rating"})
        return self._check(r)

    def upload_pdf(self, pdf: bytes, filename: str) -> str:
        r = httpx.post(f"{self.base}/{self.phone_id}/media", headers=self._auth, timeout=self.timeout,
                       data={"messaging_product": "whatsapp", "type": "application/pdf"},
                       files={"file": (filename, pdf, "application/pdf")})
        media_id = self._check(r).get("id")
        if not media_id:
            raise CloudError("Upload ohne Medien-ID beantwortet")
        return media_id

    def send_template(self, to: str, template: str, lang: str, params: list[str],
                      media_id: Optional[str] = None, filename: str = "") -> str:
        components = []
        if media_id:
            components.append({"type": "header", "parameters": [
                {"type": "document", "document": {"id": media_id, "filename": filename}}]})
        if params:
            components.append({"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]})
        body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {"name": template, "language": {"code": lang}, "components": components}}
        r = httpx.post(f"{self.base}/{self.phone_id}/messages", headers=self._auth, json=body, timeout=self.timeout)
        msgs = self._check(r).get("messages") or [{}]
        return msgs[0].get("id", "")

    def send_document(self, to: str, pdf: bytes, filename: str, template: str, lang: str,
                      params: list[str]) -> str:
        return self.send_template(to, template, lang, params, self.upload_pdf(pdf, filename), filename)


def check_signature(body: bytes, header: str, secret: str = "") -> bool:
    secret = secret or config.wa_app_secret
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def statuses(payload: dict) -> list[dict]:
    """Statusmeldungen aus einem Webhook-Aufruf: [{id, status, error, recipient}]."""
    out = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            for st in (change.get("value") or {}).get("statuses", []) or []:
                errs = st.get("errors") or []
                err = ""
                if errs:
                    e = errs[0]
                    err = f"{e.get('title') or e.get('message', '')} [Code {e.get('code')}]"
                    if (e.get("error_data") or {}).get("details"):
                        err += f": {e['error_data']['details']}"
                out.append({"id": st.get("id", ""), "status": st.get("status", ""), "error": err,
                            "recipient": st.get("recipient_id", "")})
    return out


def newer(old: str, new: str) -> bool:
    """Statusmeldungen kommen nicht immer in Reihenfolge – „gelesen“ nicht durch „zugestellt“ überschreiben."""
    return _RANK.get(new, 0) >= _RANK.get(old, 0)
