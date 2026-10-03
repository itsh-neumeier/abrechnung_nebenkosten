"""Webinterface (FastAPI + Jinja2)."""

from __future__ import annotations

import asyncio
import json
import io
import os
import re
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import accounts, auth, invoice_import, mailbox, mailer, service, victron, vrm, wa_cloud, whatsapp
from .config import config
from .db import (Allocation, Billing, FixedCost, Party, SessionLocal, get_session, get_settings, init_db,
                 save_settings)
from .ha import HAClient
from .render import BASE, invoice_html, invoice_pdf, parse_float, party_result, pdf_name, templates


@asynccontextmanager
async def lifespan(_app):
    init_db()
    with SessionLocal() as s:
        if name := auth.bootstrap(s):
            print(f"Login aktiv: Verwalter „{name}“ aus APP_USER/APP_PASSWORD angelegt.")
    tasks = []
    if mailbox.configured():
        tasks.append(asyncio.create_task(mailbox.poll_forever()))
    if config.victron_logger:
        tasks.append(asyncio.create_task(victron.logger.run_forever()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Nebenkostenabrechnung Hausparteien", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(accounts.router)


# --------------------------------------------------------------------------- Helfer
def normalize_spec(text: str) -> str:
    """Entitäten-Angabe vereinheitlichen: „a+b , c“ -> „a + b + c“."""
    return " + ".join(e for e in re.split(r"[\s,;+]+", text or "") if e)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("ha_configured", bool(config.ha_url and config.ha_token))
    ctx.setdefault("victron_status", victron.logger.status)
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url, hash_, frag = url.partition("#")  # Meldung vor den Anker, sonst kommt sie nicht an
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg) + hash_ + frag
    return RedirectResponse(url, status_code=303)


PUBLIC = ("/login", "/logout", "/setup", "/password/", "/static/", "/healthz", "/favicon", "/apple-touch-icon",
          "/api/n8n/", "/api/whatsapp/", "/manifest.webmanifest", "/sw.js", "/offline", "/app")
TENANT_OK = ("/portal", "/account")


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """Anmeldung prüfen. Ohne angelegte Benutzer ist die App offen (Hinweis zur Einrichtung im Menü)."""
    path = request.url.path
    with SessionLocal() as s:
        enabled = auth.has_users(s)
        u = None
        if enabled:
            if cookie := request.cookies.get(auth.COOKIE):
                u = auth.user_from_cookie(s, cookie)
            if u is None and request.headers.get("authorization", "").startswith("Basic "):
                u = auth.user_from_basic(s, request.headers["authorization"])
        request.state.user = auth.snapshot(u) if u else None
        request.state.auth_enabled = enabled
    if not enabled or path.startswith(PUBLIC) or path == "/":
        if enabled and path == "/" and request.state.user is None:
            return RedirectResponse("/login", status_code=303)
        if enabled and path == "/" and not request.state.user.is_admin:
            return RedirectResponse("/portal", status_code=303)
        return await call_next(request)
    user = request.state.user
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Anmeldung erforderlich"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
    if not user.is_admin and not path.startswith(TENANT_OK):
        if request.method == "GET":
            return RedirectResponse("/portal", status_code=303)
        return Response("Nur für Verwalter", status_code=403)
    return await call_next(request)


@app.get("/favicon.ico")
def favicon():
    return FileResponse(BASE / "static" / "favicon.ico", media_type="image/x-icon")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    js = (BASE / "static" / "sw.js").read_text().replace("__VERSION__", templates.env.globals["app_version"])
    return Response(js, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/offline", response_class=HTMLResponse)
def offline(request: Request):
    return templates.TemplateResponse(request, "offline.html", {})


@app.get("/app", response_class=HTMLResponse)
def app_info(request: Request):
    return templates.TemplateResponse(request, "app.html", {"https": request.url.scheme == "https",
                                                            "base": config.app_base_url})


@app.get("/apple-touch-icon.png")
def apple_icon():
    return FileResponse(BASE / "static" / "apple-touch-icon.png", media_type="image/png")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --------------------------------------------------------------------------- Übersicht
@app.get("/", response_class=HTMLResponse)
def index(request: Request, s: Session = Depends(get_session)):
    billings = s.query(Billing).order_by(Billing.period_start.desc()).all()
    st = get_settings(s)
    setup_missing = [
        label for key, label in (("entity_total", "Gesamtverbrauch-Entität"), ("entity_grid", "Zähler-Entität"))
        if not st[key]
    ]
    if not service.active_parties(s):
        setup_missing.append("Parteien")
    return render(request, "index.html", billings=billings, setup_missing=setup_missing,
                  imap_ok=mailbox.configured(), mbox=mailbox.status, imap=config,
                  senders=mailbox.sender_patterns(st), forwarded=bool(st["imap_forwarded"]))


@app.post("/mailbox/check")
async def mailbox_check():
    return redirect("/", await mailbox.check_mailbox())


@app.post("/billings/import")
async def billing_import(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return redirect("/billings/new", "Bitte eine PDF- oder .eml-Datei auswählen.")
    data = await upload.read()
    try:
        files = invoice_import.load_upload(upload.filename, data)
        last = None
        msgs = []
        for name, pdf, mid in files:
            last, msg = await service.import_invoice(s, pdf, name, mid, source="Upload")
            msgs.append(msg)
    except invoice_import.ImportError_ as e:
        return redirect("/billings/new", f"Import fehlgeschlagen: {e}")
    return redirect(f"/billings/{last.id}" if last else "/", " · ".join(msgs))


# --------------------------------------------------------------------------- Einstellungen
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(get_session)):
    return render(request, "settings.html", st=get_settings(s), ha_url=config.ha_url,
                  ha_token_set=bool(config.ha_token), house_entities=service.HOUSE_ENTITIES,
                  victron=service.VICTRON_HINTS,
                  smtp=config, mail_ok=mailer.configured(), imap_ok=mailbox.configured(),
                  vrm_ok=vrm.configured(), vrm_mix=vrm.MIX_FIELDS)


@app.post("/settings")
async def settings_save(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    data = {k: (normalize_spec(str(v)) if k.startswith("entity_") else str(v).strip()) for k, v in form.items()}
    for flag in ("mail_auto_send", "victron_enabled", "owner_free_own_energy", "imap_forwarded"):  # Checkboxen
        data[flag] = "1" if form.get(flag) else ""
    save_settings(s, data)
    return redirect("/settings", "Gespeichert")


@app.post("/settings/test")
async def settings_test():
    try:
        msg = await HAClient(config.ha_url, config.ha_token).check()
        return redirect("/settings", f"Verbindung OK: {msg}")
    except Exception as e:  # noqa: BLE001
        return redirect("/settings", f"Verbindung fehlgeschlagen: {e}")


@app.post("/settings/testmail")
async def settings_testmail(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    to = str(form.get("test_to", "") or form.get("to", "")).strip()
    if not mailer.configured():
        return redirect("/settings#mail", "SMTP ist nicht eingerichtet (SMTP_HOST / SMTP_FROM in Portainer).")
    if not to:
        return redirect("/settings#mail", "Bitte eine Empfängeradresse für die Test-E-Mail eintragen.")
    try:
        mailer.send_mail([to], "Test Nebenkostenabrechnung",
                         f"Der E-Mail-Versand funktioniert.\n\nServer: {config.smtp_host}:{config.smtp_port} "
                         f"({config.smtp_security})\nAbsender: {config.smtp_from}\n", [])
        return redirect("/settings#mail", f"Test-E-Mail an {to} verschickt – bitte Posteingang (und Spam) prüfen.")
    except Exception as e:  # noqa: BLE001
        return redirect("/settings#mail", f"E-Mail fehlgeschlagen: {mailer.explain(e)}")


@app.post("/settings/testimap")
async def settings_testimap(request: Request):
    """Postfach prüfen mit den Absender-Angaben aus dem Formular (ohne Speichern, ohne Import)."""
    form = await request.form()
    if not mailbox.configured():
        return redirect("/settings#eingang", "IMAP ist nicht eingerichtet (IMAP_HOST / IMAP_USER / IMAP_PASSWORD).")
    patterns = mailbox.sender_patterns({"imap_senders": str(form.get("imap_senders", ""))})
    try:
        msg = await asyncio.to_thread(mailbox.test_connection, patterns, bool(form.get("imap_forwarded")))
    except Exception as e:  # noqa: BLE001
        msg = f"Postfach-Test fehlgeschlagen: {e}"
    return redirect("/settings#eingang", msg)


# --------------------------------------------------------------------------- WhatsApp über n8n
WA_KEYS = ("wa_mode", "n8n_webhook_url", "n8n_app_url", "wa_provider", "wa_message", "wa_template_name", "wa_template_lang")


@app.get("/whatsapp", response_class=HTMLResponse)
def whatsapp_page(request: Request, s: Session = Depends(get_session)):
    st = get_settings(s)
    secret = whatsapp.ensure_secret(s, st)
    flows = {k: whatsapp.flow_json(k, st, secret) for k in whatsapp.PROVIDERS}
    last = json.loads(st["n8n_last_test"]) if st["n8n_last_test"] else None
    verify = _wa_verify_token(s, st)
    base = config.app_base_url
    return render(request, "whatsapp.html", st=st, secret=secret, flows=flows, providers=whatsapp.PROVIDERS,
                  modes=whatsapp.MODES, native_ok=wa_cloud.configured(), phone_id=config.wa_phone_number_id,
                  app_secret_set=bool(config.wa_app_secret), verify_token=verify,
                  meta_webhook=(base + "/api/whatsapp/webhook") if base else "",
                  app_url=whatsapp.app_url(st), path=whatsapp.WEBHOOK_PATH, header=whatsapp.HEADER, last=last,
                  parties=s.query(Party).order_by(Party.sort, Party.id).all())


@app.post("/whatsapp")
async def whatsapp_save(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    data = {k: str(form.get(k, "")).strip() for k in WA_KEYS}
    data["n8n_app_url"] = data["n8n_app_url"].rstrip("/")
    if data["wa_mode"] not in whatsapp.MODES:
        data["wa_mode"] = "n8n"
    data["n8n_pdf_base64"] = "1" if form.get("n8n_pdf_base64") else ""
    if form.get("new_secret"):
        data["n8n_secret"] = secrets.token_urlsafe(24)
    save_settings(s, data)
    s.commit()
    return redirect("/whatsapp", "Neues Token erzeugt – Flow in n8n neu kopieren!" if form.get("new_secret") else "Gespeichert")


def _wa_verify_token(s: Session, st: dict) -> str:
    if not st.get("wa_verify_token"):
        st["wa_verify_token"] = secrets.token_urlsafe(18)
        save_settings(s, {"wa_verify_token": st["wa_verify_token"]})
        s.commit()
    return st["wa_verify_token"]


@app.post("/whatsapp/check")
def whatsapp_check():
    try:
        info = wa_cloud.CloudClient().info()
        return redirect("/whatsapp", f"Cloud API OK: {info.get('verified_name', '?')} · {info.get('display_phone_number', '?')}"
                                     f" · Qualität {info.get('quality_rating', '?')}")
    except Exception as e:  # noqa: BLE001
        return redirect("/whatsapp", f"Cloud API: {e}")


@app.post("/whatsapp/test")
async def whatsapp_test(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    st = get_settings(s)
    secret = whatsapp.ensure_secret(s, st)
    phone = str(form.get("phone", "")).strip()
    if whatsapp.native(st):
        to = whatsapp.normalize_phone(phone)
        try:
            wamid = wa_cloud.CloudClient().send_document(
                to, service.test_pdf(), "Test.pdf", st["wa_template_name"], st["wa_template_lang"],
                ["Test", "Testzeitraum", "0,00 €"])
            save_settings(s, {"n8n_last_test": json.dumps({
                "ok": True, "at": datetime.now().isoformat(timespec="seconds"), "status": "gesendet",
                "raw_status": "sent", "message_id": wamid, "provider": "Cloud API direkt", "error": ""})})
            s.commit()
            return redirect("/whatsapp", f"Test an +{to} gesendet – Zustellstatus erscheint unten, sobald Meta ihn meldet.")
        except Exception as e:  # noqa: BLE001
            return redirect("/whatsapp", f"Test fehlgeschlagen: {e}")
    try:
        payload = whatsapp.build_payload(
            st, secret, bid=0, pid=0, name="Test", unit_id="TEST", phone=phone, email="", period="Test",
            total=0.0, total_text="0,00 €", due=date.today().isoformat(),
            message="Test der WhatsApp-Anbindung der Nebenkostenabrechnung ✅", filename="Test.pdf",
            pdf=service.test_pdf(), test=True)
        save_settings(s, {"n8n_last_test": ""})
        s.commit()
        msg = whatsapp.post(st, secret, payload)
        return redirect("/whatsapp", f"Test an +{payload['phone']} {msg} – Rückmeldung erscheint unten (Seite neu laden).")
    except Exception as e:  # noqa: BLE001
        return redirect("/whatsapp", f"Test fehlgeschlagen: {e}")


@app.get("/whatsapp/flow/{provider}.json")
def whatsapp_flow(provider: str, s: Session = Depends(get_session)):
    if provider not in whatsapp.PROVIDERS:
        raise HTTPException(404)
    st = get_settings(s)
    return Response(whatsapp.flow_json(provider, st, whatsapp.ensure_secret(s, st)), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="n8n-nebenkosten-whatsapp-{provider}.json"'})


def _n8n_secret(s: Session) -> str:
    return get_settings(s)["n8n_secret"]


@app.get("/api/n8n/invoice/{bid}/{pid}.pdf")
def n8n_invoice_pdf(bid: int, pid: int, exp: int = 0, sig: str = "", s: Session = Depends(get_session)):
    if not whatsapp.verify(_n8n_secret(s), bid, pid, exp, sig):
        raise HTTPException(403, "Link ungültig oder abgelaufen")
    b = _get_billing(s, bid)
    p = _party_or_404(b, pid)
    return Response(invoice_pdf(b, p), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_name(b, p)}"', "Cache-Control": "no-store"})


@app.get("/api/n8n/test.pdf")
def n8n_test_pdf(exp: int = 0, sig: str = "", s: Session = Depends(get_session)):
    if not whatsapp.verify(_n8n_secret(s), 0, 0, exp, sig):
        raise HTTPException(403, "Link ungültig oder abgelaufen")
    return Response(service.test_pdf(), media_type="application/pdf")


@app.post("/api/n8n/status")
async def n8n_status(request: Request, s: Session = Depends(get_session)):
    secret = _n8n_secret(s)
    if not secret or not secrets.compare_digest(request.headers.get(whatsapp.HEADER, ""), secret):
        raise HTTPException(403, "Token falsch")
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON erwartet")
    try:
        return {"ok": True, "result": service.wa_status(s, data)}
    except KeyError:
        raise HTTPException(404, "Abrechnung unbekannt")


@app.get("/api/whatsapp/webhook")
def meta_webhook_verify(request: Request, s: Session = Depends(get_session)):
    """Einrichtung des Webhooks in der Meta-App (hub.challenge zurückgeben)."""
    q = request.query_params
    token = get_settings(s)["wa_verify_token"]
    if q.get("hub.mode") == "subscribe" and token and secrets.compare_digest(q.get("hub.verify_token", ""), token):
        return Response(q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403, "Verify-Token falsch")


@app.post("/api/whatsapp/webhook")
async def meta_webhook(request: Request, s: Session = Depends(get_session)):
    """Zustellstatus von Meta (sent / delivered / read / failed)."""
    body = await request.body()
    if not wa_cloud.check_signature(body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(403, "Signatur ungültig (WA_APP_SECRET prüfen)")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "JSON erwartet")
    return {"ok": True, "matched": service.wa_cloud_status(s, wa_cloud.statuses(payload))}


_entity_cache: dict = {"at": 0.0, "data": None}


@app.get("/api/entities")
async def api_entities(refresh: bool = False, s: Session = Depends(get_session)):
    """Zähler-/Leistungssensoren aus Home Assistant + virtuelle Victron-Entitäten des Loggers."""
    st_ = get_settings(s)
    virtual = victron.virtual_entities() if st_["victron_enabled"] else []
    if vrm.configured() and st_["vrm_site_id"]:
        virtual += vrm.virtual_entities()
    if not (config.ha_url and config.ha_token):
        if virtual:
            return virtual
        return JSONResponse({"error": "Home Assistant ist nicht konfiguriert (HA_URL / HA_TOKEN)."}, 503)
    if refresh or _entity_cache["data"] is None or time.time() - _entity_cache["at"] > 60:
        try:
            out = await HAClient(config.ha_url, config.ha_token).entities()
        except Exception as e:  # noqa: BLE001
            if virtual:
                return virtual
            return JSONResponse({"error": f"Home Assistant nicht erreichbar: {e}"}, 502)
        _entity_cache.update(at=time.time(), data=out)
    return virtual + _entity_cache["data"]


@app.get("/api/sources")
async def api_sources(refresh: bool = False, s: Session = Depends(get_session)):
    """Auswahl-Dialog: Entitäten je Datenquelle mit Status (eingerichtet / erreichbar / Hinweis)."""
    st_ = get_settings(s)
    out = {}
    if not (config.ha_url and config.ha_token):
        out["ha"] = {"ok": False, "hint": "HA_URL / HA_TOKEN in der .env bzw. in Portainer setzen.", "entities": []}
    else:
        try:
            if refresh or _entity_cache["data"] is None or time.time() - _entity_cache["at"] > 60:
                _entity_cache.update(at=time.time(), data=await HAClient(config.ha_url, config.ha_token).entities())
            out["ha"] = {"ok": True, "hint": "", "entities": _entity_cache["data"]}
        except Exception as e:  # noqa: BLE001
            out["ha"] = {"ok": False, "hint": f"Home Assistant nicht erreichbar: {e}", "entities": []}
    if st_["victron_enabled"]:
        err = victron.logger.status.get("last_error")
        out["victron"] = {"ok": True, "hint": f"Logger-Fehler: {err}" if err else "",
                          "entities": victron.virtual_entities()}
    else:
        out["victron"] = {"ok": False, "hint": "Unter Einstellungen → „Victron direkt“ den Logger aktivieren.",
                          "entities": []}
    if vrm.configured() and st_["vrm_site_id"]:
        out["vrm"] = {"ok": True, "hint": "", "entities": vrm.virtual_entities()}
    else:
        out["vrm"] = {"ok": False, "hint": "VRM_TOKEN setzen und unter Einstellungen → VRM die Anlage suchen.",
                      "entities": []}
    return out


# --------------------------------------------------------------------------- Victron direkt
@app.post("/victron/discover")
async def victron_discover(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    if "victron_host" in form:  # Knopf sitzt im Einstellungsformular: Eingaben zuerst übernehmen
        save_settings(s, {"victron_host": str(form.get("victron_host", "")).strip(),
                          "victron_port": str(form.get("victron_port", "502")).strip() or "502",
                          "victron_enabled": "1" if form.get("victron_enabled") else ""})
    st = get_settings(s)
    if not st["victron_host"]:
        return redirect("/settings", "Bitte zuerst die IP des GX eintragen und speichern.")
    reader = victron.ModbusReader(st["victron_host"], int(st["victron_port"] or 502))
    try:
        units = await victron.discover(reader)
    except Exception as e:  # noqa: BLE001
        return redirect("/settings", f"Victron: {e}")
    finally:
        await reader.close()
    save_settings(s, {"victron_units": json.dumps(units)})
    victron.logger.units = units
    names = {"system": "System", "vebus": "VE.Bus", "battery": "Batteriewächter", "grid": "Energiezähler"}
    found = ", ".join(f"{names[k]} = Unit {v}" for k, v in units.items() if v is not None) or "nichts"
    missing = ", ".join(names[k] for k, v in units.items() if v is None)
    return redirect("/settings", f"Victron gefunden: {found}" + (f" · nicht gefunden: {missing}" if missing else ""))


@app.get("/victron/compare", response_class=HTMLResponse)
async def victron_compare(request: Request, hours: int = 24, s: Session = Depends(get_session)):
    """Abgleich HA ↔ Victron-Logger für die letzten N vollen Stunden."""
    from zoneinfo import ZoneInfo

    hours = max(1, min(hours, 24 * 62))
    t1 = datetime.now(ZoneInfo(config.timezone)).replace(minute=0, second=0, microsecond=0)
    t0 = t1 - timedelta(hours=hours)
    rows = await service.compare_period(s, t0, t1)
    return render(request, "victron_compare.html", rows=rows, hours=hours, t0=t0, t1=t1, st=get_settings(s))


@app.post("/vrm/sites")
async def vrm_sites(request: Request, s: Session = Depends(get_session)):
    """VRM-Anlagen des Tokens suchen; bei genau einer Anlage wird sie direkt übernommen."""
    form = await request.form()
    if form.get("vrm_site_id"):
        save_settings(s, {"vrm_site_id": str(form.get("vrm_site_id")).strip()})
    try:
        sites = await vrm.VRMClient(config.vrm_token).installations()
    except Exception as e:  # noqa: BLE001
        return redirect("/settings", f"VRM: {e}")
    if len(sites) == 1 and not get_settings(s)["vrm_site_id"]:
        save_settings(s, {"vrm_site_id": str(sites[0]["id"])})
    listing = ", ".join(f"{x['name']} = {x['id']}" for x in sites) or "keine"
    return redirect("/settings", f"VRM-Anlagen: {listing}")


@app.get("/api/victron/status")
def victron_status():
    return victron.logger.status


# --------------------------------------------------------------------------- Parteien
@app.get("/parties", response_class=HTMLResponse)
def parties_page(request: Request, s: Session = Depends(get_session)):
    parties = s.query(Party).order_by(Party.active.desc(), Party.sort, Party.id).all()
    return render(request, "parties.html", parties=parties)


@app.get("/parties/{pid}", response_class=HTMLResponse)
def party_edit(request: Request, pid: int, s: Session = Depends(get_session)):
    p = Party(name="", meters=[], active=True, is_owner=False, sort=0) if pid == 0 else s.get(Party, pid)
    if p is None:
        raise HTTPException(404)
    return render(request, "party_edit.html", p=p, pid=pid)


@app.post("/parties/{pid}")
async def party_save(request: Request, pid: int, s: Session = Depends(get_session)):
    form = await request.form()
    if form.get("delete"):
        p = s.get(Party, pid)
        if p:
            s.delete(p)
            s.commit()
        return redirect("/parties", "Gelöscht")
    p = Party() if pid == 0 else s.get(Party, pid)
    if p is None:
        raise HTTPException(404)
    p.name = str(form.get("name", "")).strip()
    p.unit = str(form.get("unit", "")).strip()
    p.unit_id = str(form.get("unit_id", "")).strip()
    p.address = str(form.get("address", "")).strip()
    p.email = str(form.get("email", "")).strip()
    p.phone = str(form.get("phone", "")).strip()
    p.portal = bool(form.get("portal"))
    p.channel = str(form.get("channel", "email")) if form.get("channel") in ("email", "whatsapp", "both") else "email"
    p.meters = [m for m in (normalize_spec(str(v)) for v in form.getlist("meters")) if m]
    p.is_owner = bool(form.get("is_owner"))
    p.active = bool(form.get("active"))
    p.sort = int(parse_float(form.get("sort"), 0) or 0)
    if p.is_owner:
        for other in s.query(Party).filter(Party.is_owner.is_(True)).all():
            if other is not p:
                other.is_owner = False
    if pid == 0:
        s.add(p)
    s.commit()
    return redirect("/parties", "Gespeichert")


# --------------------------------------------------------------------------- Fixkosten
@app.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, s: Session = Depends(get_session)):
    return render(
        request, "costs.html",
        costs=s.query(FixedCost).order_by(FixedCost.id).all(),
        allocations=s.query(Allocation).order_by(Allocation.id).all(),
        parties={p.id: p.name for p in s.query(Party).all()},
    )


@app.get("/costs/fixed/{cid}", response_class=HTMLResponse)
def fixed_edit(request: Request, cid: int, s: Session = Depends(get_session)):
    c = FixedCost(name="", amount_gross=0.0, party_ids=[], active=True) if cid == 0 else s.get(FixedCost, cid)
    if c is None:
        raise HTTPException(404)
    return render(request, "fixed_edit.html", c=c, cid=cid, parties=service.active_parties(s))


@app.post("/costs/fixed/{cid}")
async def fixed_save(request: Request, cid: int, s: Session = Depends(get_session)):
    form = await request.form()
    if form.get("delete"):
        c = s.get(FixedCost, cid)
        if c:
            s.delete(c)
            s.commit()
        return redirect("/costs", "Gelöscht")
    c = FixedCost() if cid == 0 else s.get(FixedCost, cid)
    if c is None:
        raise HTTPException(404)
    c.name = str(form.get("name", "")).strip()
    c.amount_gross = parse_float(form.get("amount_gross")) or 0.0
    c.party_ids = [int(x) for x in form.getlist("party_ids")]
    c.active = bool(form.get("active"))
    if cid == 0:
        s.add(c)
    s.commit()
    return redirect("/costs", "Gespeichert")


@app.get("/costs/alloc/{aid}", response_class=HTMLResponse)
def alloc_edit(request: Request, aid: int, s: Session = Depends(get_session)):
    a = (Allocation(name="", source_type="energy", source_entity="", source_unit="m³", price_source="custom", default_amount=0.0,
                    key_type="percent", key_unit="", key={}, active=True)
         if aid == 0 else s.get(Allocation, aid))
    if a is None:
        raise HTTPException(404)
    return render(request, "alloc_edit.html", a=a, aid=aid, parties=service.active_parties(s))


@app.post("/costs/alloc/{aid}")
async def alloc_save(request: Request, aid: int, s: Session = Depends(get_session)):
    form = await request.form()
    if form.get("delete"):
        a = s.get(Allocation, aid)
        if a:
            s.delete(a)
            s.commit()
        return redirect("/costs", "Gelöscht")
    a = Allocation() if aid == 0 else s.get(Allocation, aid)
    if a is None:
        raise HTTPException(404)
    a.name = str(form.get("name", "")).strip()
    a.source_type = str(form.get("source_type", "energy"))
    a.source_entity = normalize_spec(str(form.get("source_entity", "")))
    a.source_unit = str(form.get("source_unit", "")).strip() or "m³"
    a.price_source = str(form.get("price_source", "custom"))
    a.default_amount = parse_float(form.get("default_amount")) or 0.0
    a.key_type = str(form.get("key_type", "entity"))
    a.key_unit = str(form.get("key_unit", "")).strip()
    key = {}
    for p in service.active_parties(s):
        if a.key_type == "percent":
            raw = str(form.get(f"keypct_{p.id}", form.get(f"key_{p.id}", ""))).strip()
            if raw:
                key[str(p.id)] = parse_float(raw)
        else:
            raw = normalize_spec(str(form.get(f"keyent_{p.id}", form.get(f"key_{p.id}", ""))))
            if raw:
                key[str(p.id)] = raw
    a.key = key
    a.active = bool(form.get("active"))
    if aid == 0:
        s.add(a)
    s.commit()
    return redirect("/costs", "Gespeichert")


# --------------------------------------------------------------------------- Abrechnungen
def _apply_bill_form(b: Billing, form, st: dict) -> None:
    """Übernimmt die Rechnungsfelder aus dem Formular."""
    b.title = str(form.get("title", b.title or "")).strip()
    b.invoice_no = str(form.get("invoice_no", b.invoice_no or "")).strip()
    b.period_start = date.fromisoformat(str(form.get("period_start")))
    b.period_end = date.fromisoformat(str(form.get("period_end")))
    b.grid_kwh = parse_float(form.get("grid_kwh"), None)
    b.energy_cost_net = parse_float(form.get("energy_cost_net")) or 0.0
    b.fixed_cost_net = parse_float(form.get("fixed_cost_net")) or 0.0
    b.spot_price_ct = parse_float(form.get("spot_price_ct")) or 0.0
    b.vat_rate = (parse_float(form.get("vat_rate"), parse_float(st["vat_rate"])) or 0.0) / 100
    b.battery_rate_ct = parse_float(form.get("battery_rate_ct"), parse_float(st["battery_rate_ct"])) or 0.0
    b.pv_rate_ct = parse_float(form.get("pv_rate_ct"), parse_float(st["pv_rate_ct"])) or 0.0


@app.get("/billings/new", response_class=HTMLResponse)
def billing_new(request: Request, s: Session = Depends(get_session)):
    st = get_settings(s)
    end = date.today().replace(day=1) - timedelta(days=1)
    return render(request, "billing_new.html", start=end.replace(day=1), end=end, st=st)


@app.post("/billings")
async def billing_create(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    st = get_settings(s)
    b = Billing(values={}, amounts={}, result={}, sent={})
    _apply_bill_form(b, form, st)
    if not b.title:
        b.title = f"Nebenkosten {b.period_start.strftime('%m/%Y')}"
    s.add(b)
    s.commit()
    msg = "Angelegt"
    if form.get("fetch") and ((config.ha_url and config.ha_token) or st["victron_enabled"]):
        try:
            msg = service.fetch_message(await service.fetch_values(s, b))
        except Exception as e:  # noqa: BLE001
            msg = f"Home Assistant nicht erreichbar: {e}"
    service.recompute(s, b)
    s.commit()
    return redirect(f"/billings/{b.id}", msg)


def _get_billing(s: Session, bid: int) -> Billing:
    b = s.get(Billing, bid)
    if b is None:
        raise HTTPException(404)
    return b


@app.get("/billings/{bid}", response_class=HTMLResponse)
def billing_view(request: Request, bid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    allocs = [a for a in s.query(Allocation).filter(Allocation.active.is_(True)).all()
              if a.source_type == "amount" or (a.source_type == "quantity" and a.price_source != "water")]
    st = get_settings(s)
    contacts = {}
    for p in s.query(Party).all():
        by_mail, by_wa = service.channels(p)
        contacts[p.id] = {"mail": by_mail and bool(p.email), "wa": by_wa and bool(p.phone),
                          "want_mail": by_mail, "want_wa": by_wa}
    return render(request, "billing.html", b=b, r=b.result or {}, entities=service.required_entities(s),
                  amount_allocs=allocs, st=st, contacts=contacts, mail_ok=mailer.configured(),
                  wa_ok=whatsapp.configured(st), send_ok=service.can_send(st),
                  compare=service.victron_comparison(s, b))


def _report_msg(report: list[tuple[str, bool, str]]) -> str:
    if not report:
        return "Nichts verschickt (keine offenen Parteien mit E-Mail-Adresse bzw. WhatsApp-Nummer)."
    return "Versand: " + "; ".join(f"{n} {'✔' if ok else '✘ ' + m}" for n, ok, m in report)


@app.post("/billings/{bid}")
async def billing_save(request: Request, bid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    form = await request.form()
    action = form.get("action", "save")
    st = get_settings(s)

    if action == "delete":
        s.delete(b)
        s.commit()
        return redirect("/", "Abrechnung gelöscht")
    if action == "reopen":
        b.status = "draft"
        s.commit()
        return redirect(f"/billings/{bid}", "Wieder zur Bearbeitung geöffnet")
    if action in ("publish", "unpublish"):
        b.published = action == "publish"
        s.commit()
        return redirect(f"/billings/{bid}", "Im Mieterportal veröffentlicht" if b.published
                        else "Aus dem Mieterportal zurückgezogen")
    if action.startswith("send"):
        if b.status != "final":
            return redirect(f"/billings/{bid}", "Bitte zuerst abschließen, dann versenden.")
        ids = None if action == "send_all" else [int(action.split(":")[1])]
        report = service.send_invoices(s, b, ids)
        s.commit()
        return redirect(f"/billings/{bid}", _report_msg(report))
    if b.status == "final":
        return redirect(f"/billings/{bid}", "Abrechnung ist abgeschlossen")

    _apply_bill_form(b, form, st)
    b.notes = str(form.get("notes", "")).strip()
    values = {}
    for k, v in form.items():
        if k.startswith("val__"):
            fv = parse_float(v, None)
            if fv is not None:
                values[k[5:]] = fv
    b.values = values
    b.amounts = {k[8:]: parse_float(v) for k, v in form.items() if k.startswith("amount__")}

    msg = "Gespeichert"
    if action == "fetch":
        try:
            msg = service.fetch_message(await service.fetch_values(s, b))
        except Exception as e:  # noqa: BLE001
            msg = f"Home Assistant: {e}"
    service.recompute(s, b)
    if action == "finalize":
        b.status = "final"
        msg = "Abrechnung abgeschlossen"
        if st["portal_auto_publish"]:
            b.published = True
            msg += " · im Mieterportal veröffentlicht"
        if st["mail_auto_send"] and service.can_send(st):
            s.commit()
            msg += " · " + _report_msg(service.send_invoices(s, b, only_unsent=True))
    s.commit()
    return redirect(f"/billings/{bid}", msg)


def _party_or_404(b: Billing, pid: int) -> dict:
    p = party_result(b, pid)
    if p is None:
        raise HTTPException(404, "Partei nicht in dieser Abrechnung")
    return p


@app.get("/billings/{bid}/source.pdf")
def billing_source(bid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    if not b.source_file or not os.path.exists(b.source_file):
        raise HTTPException(404, "Keine Original-Rechnung gespeichert")
    return FileResponse(b.source_file, media_type="application/pdf")


@app.get("/billings/{bid}/invoice/{pid}.pdf")
def invoice_pdf_view(bid: int, pid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    p = _party_or_404(b, pid)
    return Response(invoice_pdf(b, p), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_name(b, p)}"',
                             "Cache-Control": "no-store"})


@app.get("/billings/{bid}/invoice/{pid}", response_class=HTMLResponse)
def invoice_view(bid: int, pid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    return HTMLResponse(invoice_html(b, _party_or_404(b, pid)))


@app.get("/billings/{bid}/all.zip")
def invoices_zip(bid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in b.result.get("parties", []):
            z.writestr(pdf_name(b, p), invoice_pdf(b, p))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="Nebenkostenabrechnung_{b.period_start:%Y-%m}.zip"'})
