"""Webinterface (FastAPI + Jinja2)."""

from __future__ import annotations

import asyncio
import base64
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

from . import invoice_import, mailbox, mailer, service, victron, vrm
from .config import config
from .db import Allocation, Billing, FixedCost, Party, get_session, get_settings, init_db, save_settings
from .ha import HAClient
from .render import BASE, invoice_html, invoice_pdf, parse_float, party_result, pdf_name, templates


@asynccontextmanager
async def lifespan(_app):
    init_db()
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
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.app_user and config.app_password and not request.url.path.startswith("/healthz"):
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, config.app_user) and secrets.compare_digest(pw, config.app_password)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Nebenkostenabrechnung"'})
    return await call_next(request)


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
                  imap_ok=mailbox.configured(), mbox=mailbox.status, imap=config)


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
    for flag in ("mail_auto_send", "victron_enabled", "owner_free_own_energy"):  # Checkboxen
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
    to = str(form.get("to", "")).strip()
    try:
        mailer.send_mail([to], "Test Nebenkostenabrechnung", "Der E-Mail-Versand funktioniert.", [])
        return redirect("/settings", f"Test-E-Mail an {to} verschickt")
    except Exception as e:  # noqa: BLE001
        return redirect("/settings", f"E-Mail fehlgeschlagen: {e}")


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
    emails = {p.id: p.email for p in s.query(Party).all()}
    return render(request, "billing.html", b=b, r=b.result or {}, entities=service.required_entities(s),
                  amount_allocs=allocs, st=get_settings(s), emails=emails, mail_ok=mailer.configured(),
                  compare=service.victron_comparison(s, b))


def _report_msg(report: list[tuple[str, bool, str]]) -> str:
    if not report:
        return "Keine E-Mails verschickt (keine offenen Parteien mit E-Mail-Adresse)."
    return "E-Mail: " + "; ".join(f"{n} {'✔' if ok else '✘ ' + m}" for n, ok, m in report)


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
        if st["mail_auto_send"] and mailer.configured():
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
