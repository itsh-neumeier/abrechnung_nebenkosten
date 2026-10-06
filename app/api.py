"""Webhook-Schnittstelle mit API-Schlüssel: Benachrichtigungen aus n8n, Home Assistant & Co. auslösen.

``POST /api/v1/notify`` (Header ``X-Api-Key: …`` oder ``Authorization: Bearer …``)::

    {"title": "Wasser wird abgestellt", "body": "Morgen 9–12 Uhr", "priority": "high", "category": "abschaltung",
     "parties": ["WE-001", 3], "building": "GID-01", "audience": "tenants", "mail": false, "persist": true}

* ``audience``: ``tenants`` (Standard, Mieter der gewählten Parteien), ``admins`` (Verwalter) oder ``all`` (beide).
* ``parties``: Partei-IDs, Wohnungsnummern oder Namen; leer = alle Parteien (Broadcast).
* ``building``: Gebäude-ID (Zahl) oder Gebäude-Code; schränkt die Parteien ein.
* ``persist``: Mitteilung zusätzlich in „Mein Zuhause“ anzeigen (Standard ja); ``false`` = nur Push.

Schlüssel werden vom Super-Admin unter /admin/api-keys angelegt (gespeichert wird nur der SHA-256-Hash) und können
auf Gebäude beschränkt werden.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from . import auth, notify
from .accounts import _base_url, _page, _redirect, require_admin
from .db import ApiKey, Building, Message, Party, User, get_session

router = APIRouter()
PREFIX = "imv_"
key_throttle = auth.Throttle(limit=60, window=60)  # je Schlüssel 60 Aufrufe pro Minute
bad_throttle = auth.Throttle(limit=10, window=300)  # falsche Schlüssel je IP
AUDIENCES = ("tenants", "admins", "all")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(s: Session, name: str, building_ids: list[int], allow_admins: bool = True) -> tuple[ApiKey, str]:
    raw = PREFIX + secrets.token_urlsafe(32)
    k = ApiKey(name=name, prefix=raw[:12], key_hash=_hash(raw), building_ids=sorted(set(building_ids)),
               allow_admins=allow_admins, active=True)
    s.add(k)
    s.commit()
    return k, raw


def _key_from_request(request: Request) -> str:
    key = request.headers.get("x-api-key", "").strip()
    if not key and request.headers.get("authorization", "").lower().startswith("bearer "):
        key = request.headers["authorization"][7:].strip()
    return key


def authenticate(request: Request, s: Session) -> ApiKey:
    ip = request.client.host if request.client else "?"
    if bad_throttle.blocked(ip):
        raise HTTPException(429, "Zu viele ungültige Schlüssel – bitte später erneut versuchen.")
    raw = _key_from_request(request)
    k = s.query(ApiKey).filter(ApiKey.key_hash == _hash(raw)).first() if raw else None
    if k is None or not k.active or not hmac.compare_digest(k.key_hash, _hash(raw)):
        bad_throttle.hit(ip)
        raise HTTPException(401, "Ungültiger oder fehlender API-Schlüssel (Header X-Api-Key).")
    if key_throttle.blocked(str(k.id)):
        raise HTTPException(429, "Zu viele Aufrufe – höchstens 60 pro Minute.")
    key_throttle.hit(str(k.id))
    k.last_used, k.uses = datetime.now(), (k.uses or 0) + 1
    s.commit()
    return k


def _buildings_for(s: Session, k: ApiKey, value) -> Optional[set]:
    """Erlaubte Gebäude für diesen Aufruf (None = alle)."""
    allowed = set(k.building_ids or []) or None
    if value in (None, "", []):
        return allowed
    b = None
    if str(value).isdigit():
        b = s.get(Building, int(value))
    if b is None:
        b = s.query(Building).filter(Building.code == str(value)).first()
    if b is None:
        raise HTTPException(404, f"Gebäude „{value}“ nicht gefunden.")
    if allowed is not None and b.id not in allowed:
        raise HTTPException(403, "Dieser Schlüssel darf das Gebäude nicht benachrichtigen.")
    return {b.id}


def _parties_for(s: Session, buildings: Optional[set], wanted: list) -> list[Party]:
    q = s.query(Party).filter(Party.active.is_(True))
    if buildings is not None:
        q = q.filter(Party.building_id.in_(buildings or [-1]))
    pool = q.order_by(Party.sort, Party.id).all()
    if not wanted:
        return pool
    found, unknown = [], []
    for w in wanted:
        w_s = str(w).strip().lower()
        hit = next((p for p in pool if (isinstance(w, int) or w_s.isdigit()) and p.id == int(w_s)), None) \
            or next((p for p in pool if (p.unit_id or "").lower() == w_s), None) \
            or next((p for p in pool if p.name.lower() == w_s), None)
        if hit is None:
            unknown.append(str(w))
        elif hit not in found:
            found.append(hit)
    if unknown:
        raise HTTPException(404, "Partei(en) nicht gefunden: " + ", ".join(unknown))
    return found


def _admin_targets(s: Session, buildings: Optional[set]) -> list[int]:
    out = []
    for u in s.query(User).filter(User.role.in_(auth.ADMIN_ROLES), User.active.is_(True)):
        if u.role == "superadmin" or buildings is None or set(int(x) for x in (u.building_ids or [])) & buildings:
            out.append(u.id)
    return out


def _dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(422, f"Ungültiges Datum: {value} (ISO-Format, z. B. 2026-10-07T09:00)")


@router.get("/api/v1/ping")
def ping(request: Request, s: Session = Depends(get_session)):
    k = authenticate(request, s)
    return {"ok": True, "key": k.name, "buildings": k.building_ids or "alle"}


@router.get("/api/v1/parties")
def parties(request: Request, building: str = "", s: Session = Depends(get_session)):
    """Parteien und Gebäude, die dieser Schlüssel benachrichtigen darf (für Auswahlfelder in n8n/HA)."""
    k = authenticate(request, s)
    blds = _buildings_for(s, k, building)
    bq = s.query(Building)
    if blds is not None:
        bq = bq.filter(Building.id.in_(blds or [-1]))
    return {"buildings": [{"id": b.id, "code": b.code, "name": b.name} for b in bq.order_by(Building.sort, Building.id)],
            "parties": [{"id": p.id, "unit_id": p.unit_id, "name": p.name, "building_id": p.building_id}
                        for p in _parties_for(s, blds, [])]}


@router.post("/api/v1/notify")
async def notify_endpoint(request: Request, s: Session = Depends(get_session)):
    k = authenticate(request, s)
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON-Body erwartet.")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON-Objekt erwartet.")
    title = str(data.get("title") or "").strip()[:200]
    if not title:
        raise HTTPException(422, "„title“ fehlt.")
    body = str(data.get("body") or data.get("message") or "").strip()[:4000]
    audience = str(data.get("audience") or "tenants").lower()
    if audience not in AUDIENCES:
        raise HTTPException(422, "„audience“ muss tenants, admins oder all sein.")
    if audience != "tenants" and not k.allow_admins:
        raise HTTPException(403, "Dieser Schlüssel darf keine Verwalter benachrichtigen.")
    priority = str(data.get("priority") or "normal").lower()
    if priority not in notify.PRIORITIES:
        raise HTTPException(422, "„priority“ muss low, normal, high oder urgent sein.")
    category = str(data.get("category") or "info").lower()
    if category not in notify.CATEGORIES:
        raise HTTPException(422, "„category“ muss eines von " + ", ".join(notify.CATEGORIES) + " sein.")
    wanted = data.get("parties") or data.get("party") or []
    if not isinstance(wanted, list):
        wanted = [wanted]
    blds = _buildings_for(s, k, data.get("building"))
    result: dict = {"ok": True, "audience": audience}

    if audience in ("tenants", "all"):
        targets = _parties_for(s, blds, wanted)
        is_broadcast = not wanted and blds is None
        m = Message(title=title, body=body, category=category, priority=priority, sender=f"API: {k.name}",
                    party_ids=[] if is_broadcast else ([p.id for p in targets] or [-1]),
                    pinned=bool(data.get("pinned")), event_start=_dt(data.get("event_start")),
                    event_end=_dt(data.get("event_end")), show_until=_dt(data.get("show_until")))
        m.remind = bool(data.get("remind")) and m.event_start is not None
        persist = data.get("persist", True) not in (False, "false", "0", 0)
        if persist:
            s.add(m)
            s.commit()
        st = notify.send_message(s, m, via_mail=bool(data.get("mail")), persist=persist)
        result.update(message_id=m.id if persist else None, parties=st["parties"], push_devices=st["push_devices"],
                      push_ok=st["push_ok"], mail_ok=st["mail_ok"], errors=st["push_errors"] + st["mail_errors"])
    if audience in ("admins", "all"):
        subs = notify.subs_for_users(s, _admin_targets(s, blds))
        ok, errors = notify.send_to(s, subs, {"title": title, "body": body[:240], "url": str(data.get("url") or "/admin"),
                                              "tag": str(data.get("tag") or f"api-{k.id}")}, priority) if subs else (0, [])
        result.update(admin_devices=len(subs), admin_push_ok=ok)
        result.setdefault("errors", [])
        result["errors"] += errors[:3]
    return JSONResponse(result)


# --------------------------------------------------------------------------- Beispiele zum Kopieren
def examples(base: str) -> dict:
    """curl, Home Assistant (rest_command + Automation) und n8n-Flow mit Platzhalter-Schlüssel."""
    url = f"{base}/api/v1/notify"
    payload = {"title": "Wasser wird abgestellt", "body": "Morgen von 9 bis 12 Uhr wegen Wartung.",
               "category": "abschaltung", "priority": "high", "audience": "tenants"}
    curl = (f"curl -X POST {url} \\\n  -H 'X-Api-Key: DEIN_API_KEY' -H 'Content-Type: application/json' \\\n"
            f"  -d '{json.dumps(payload, ensure_ascii=False)}'")
    ha = f"""# configuration.yaml – Schlüssel in secrets.yaml: immo_api_key: "Bearer DEIN_API_KEY"
rest_command:
  immo_notify:
    url: "{url}"
    method: POST
    headers:
      Authorization: !secret immo_api_key
    content_type: "application/json; charset=utf-8"
    payload: >
      {{{{ {{"title": title, "body": body | default(""), "priority": priority | default("normal"),
          "category": category | default("info"), "audience": audience | default("tenants"),
          "parties": parties | default([]), "persist": persist | default(true)}} | to_json }}}}

# Automation: Waschmaschine fertig → nur Partei WE-001, nur Push
automation:
  - alias: "Waschmaschine fertig"
    trigger:
      - platform: state
        entity_id: sensor.waschmaschine_status
        to: "fertig"
    action:
      - service: rest_command.immo_notify
        data:
          title: "Waschmaschine ist fertig"
          body: "Bitte Wäsche aus dem Keller holen."
          parties: ["WE-001"]
          persist: false"""
    node = {"parameters": {"method": "POST", "url": url, "sendHeaders": True,
                           "headerParameters": {"parameters": [{"name": "X-Api-Key", "value": "DEIN_API_KEY"}]},
                           "sendBody": True, "specifyBody": "json",
                           "jsonBody": "=" + json.dumps({**payload, "title": "{{ $json.title }}", "body": "{{ $json.body }}"},
                                                        ensure_ascii=False, indent=2),
                           "options": {}},
            "name": "ImmoVerwaltung: Benachrichtigung", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
            "position": [460, 300]}
    manual = {"parameters": {"assignments": {"assignments": [
                {"id": "t", "name": "title", "value": payload["title"], "type": "string"},
                {"id": "b", "name": "body", "value": payload["body"], "type": "string"}]}, "options": {}},
              "name": "Inhalt", "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [240, 300]}
    flow = {"nodes": [manual, node], "connections": {"Inhalt": {"main": [[{"node": node["name"], "type": "main",
                                                                            "index": 0}]]}}}
    return {"curl": curl, "ha": ha, "n8n": json.dumps(flow, ensure_ascii=False, indent=2),
            "payload": json.dumps({**payload, "parties": ["WE-001", 3], "building": "GID-01", "mail": False,
                                   "persist": True, "pinned": False, "event_start": "2026-10-07T09:00",
                                   "event_end": "2026-10-07T12:00", "remind": True}, ensure_ascii=False, indent=2)}


# --------------------------------------------------------------------------- Verwaltung (Super-Admin)
@router.get("/admin/api-keys", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def keys_page(request: Request, new: str = "", s: Session = Depends(get_session)):
    keys = s.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    buildings = s.query(Building).order_by(Building.sort, Building.id).all()
    parties_ = s.query(Party).filter(Party.active.is_(True)).order_by(Party.sort, Party.id).all()
    resp = _page(request, "api_keys.html", keys=keys, buildings=buildings, bnames={b.id: b.label for b in buildings},
                 parties=parties_, base=_base_url(request), new_key=request.cookies.get("nk_newkey", "") if new else "",
                 cats=notify.CATEGORIES, prios=notify.PRIORITIES, ex=examples(_base_url(request)))
    resp.delete_cookie("nk_newkey", path="/admin/api-keys")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/admin/api-keys", dependencies=[Depends(require_admin)])
async def key_create(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    name = str(form.get("name", "")).strip()[:100] or "Webhook"
    blds = [int(x) for x in form.getlist("building_ids") if str(x).isdigit()]
    _, raw = new_key(s, name, blds, allow_admins=bool(form.get("allow_admins")))
    # Schlüssel nur einmal anzeigen: kurzlebiges Cookie statt URL (landet nicht in Logs/Verlauf)
    resp = _redirect("/admin/api-keys?new=1", f"Schlüssel „{name}“ angelegt – jetzt kopieren, er wird nur einmal angezeigt.")
    resp.set_cookie("nk_newkey", raw, max_age=120, httponly=True, samesite="strict", path="/admin/api-keys",
                    secure=request.url.scheme == "https")
    return resp


@router.post("/admin/api-keys/{kid}/toggle", dependencies=[Depends(require_admin)])
def key_toggle(kid: int, s: Session = Depends(get_session)):
    k = s.get(ApiKey, kid)
    if k is None:
        raise HTTPException(404)
    k.active = not k.active
    s.commit()
    return _redirect("/admin/api-keys", "Aktiviert" if k.active else "Gesperrt")


@router.post("/admin/api-keys/{kid}/delete", dependencies=[Depends(require_admin)])
def key_delete(kid: int, s: Session = Depends(get_session)):
    k = s.get(ApiKey, kid)
    if k is not None:
        s.delete(k)
        s.commit()
    return _redirect("/admin/api-keys", "Schlüssel gelöscht")
