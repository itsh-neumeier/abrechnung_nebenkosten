"""Webinterface (FastAPI + Jinja2)."""

from __future__ import annotations

import base64
import io
import re
import secrets
import zipfile
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import service
from .config import config
from .db import Allocation, Billing, FixedCost, Party, get_session, get_settings, init_db, save_settings
from .ha import HAClient, HAError

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Stromabrechnung Hausparteien", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# --------------------------------------------------------------------------- Helfer
def parse_float(v, default: Optional[float] = 0.0) -> Optional[float]:
    """Akzeptiert deutsche Schreibweise (1.234,56) und Punkt-Dezimal."""
    if v is None:
        return default
    v = str(v).strip().replace(" ", "").replace("€", "")
    if not v:
        return default
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return default


def fmt_num(v, digits: int = 2) -> str:
    if v is None:
        return ""
    s = f"{float(v):,.{digits}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_eur(v) -> str:
    return "" if v is None else f"{fmt_num(v, 2)} €"


def fmt_date(d) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%d.%m.%Y") if d else ""


templates.env.filters["num"] = fmt_num
templates.env.filters["eur"] = fmt_eur
templates.env.filters["de_date"] = fmt_date


def split_entities(text: str) -> list[str]:
    return [e for e in re.split(r"[\s,;]+", text or "") if e]


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + msg
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
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Stromabrechnung"'})
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
                  ha_configured=bool(config.ha_url and config.ha_token))


# --------------------------------------------------------------------------- Einstellungen
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(get_session)):
    return render(request, "settings.html", st=get_settings(s), ha_url=config.ha_url,
                  ha_token_set=bool(config.ha_token))


@app.post("/settings")
async def settings_save(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    save_settings(s, {k: str(v).strip() for k, v in form.items()})
    return redirect("/settings", "Gespeichert")


@app.post("/settings/test")
async def settings_test(request: Request):
    try:
        msg = await HAClient(config.ha_url, config.ha_token).check()
        return redirect("/settings", f"Verbindung OK: {msg}")
    except Exception as e:  # noqa: BLE001
        return redirect("/settings", f"Verbindung fehlgeschlagen: {e}")


@app.get("/api/entities")
async def api_entities():
    """Energie-/Wasser-Sensoren aus HA für die Auswahllisten."""
    try:
        states = await HAClient(config.ha_url, config.ha_token).states()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for st in states:
        attrs = st.get("attributes", {})
        if attrs.get("state_class") in ("total", "total_increasing"):
            out.append({
                "entity_id": st["entity_id"],
                "name": attrs.get("friendly_name", ""),
                "unit": attrs.get("unit_of_measurement", ""),
            })
    return sorted(out, key=lambda x: x["entity_id"])


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
    p.address = str(form.get("address", "")).strip()
    p.email = str(form.get("email", "")).strip()
    p.meters = split_entities(str(form.get("meters", "")))
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
    a = (Allocation(name="", source_type="energy", source_entity="", default_amount=0.0, key_type="entity",
                    key_unit="m³", key={}, active=True)
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
    a.source_entity = str(form.get("source_entity", "")).strip()
    a.default_amount = parse_float(form.get("default_amount")) or 0.0
    a.key_type = str(form.get("key_type", "entity"))
    a.key_unit = str(form.get("key_unit", "")).strip()
    key = {}
    for p in service.active_parties(s):
        raw = str(form.get(f"key_{p.id}", "")).strip()
        if not raw:
            continue
        key[str(p.id)] = parse_float(raw) if a.key_type == "percent" else raw
    a.key = key
    a.active = bool(form.get("active"))
    if aid == 0:
        s.add(a)
    s.commit()
    return redirect("/costs", "Gespeichert")


# --------------------------------------------------------------------------- Abrechnungen
@app.get("/billings/new", response_class=HTMLResponse)
def billing_new(request: Request, s: Session = Depends(get_session)):
    st = get_settings(s)
    first_this = date.today().replace(day=1)
    end = first_this - timedelta(days=1)
    start = end.replace(day=1)
    return render(request, "billing_new.html", start=start, end=end, st=st)


@app.post("/billings")
async def billing_create(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    st = get_settings(s)
    b = Billing(
        title=str(form.get("title", "")).strip(),
        invoice_no=str(form.get("invoice_no", "")).strip(),
        period_start=date.fromisoformat(str(form["period_start"])),
        period_end=date.fromisoformat(str(form["period_end"])),
        grid_kwh=parse_float(form.get("grid_kwh"), None),
        energy_cost_net=parse_float(form.get("energy_cost_net")) or 0.0,
        fixed_cost_net=parse_float(form.get("fixed_cost_net")) or 0.0,
        vat_rate=(parse_float(form.get("vat_rate"), parse_float(st["vat_rate"])) or 0.0) / 100,
        battery_rate_ct=parse_float(form.get("battery_rate_ct"), parse_float(st["battery_rate_ct"])) or 0.0,
        values={}, amounts={}, result={},
    )
    if not b.title:
        b.title = f"Strom {b.period_start.strftime('%m/%Y')}"
    s.add(b)
    s.commit()
    msg = "Angelegt"
    if form.get("fetch") and config.ha_url and config.ha_token:
        try:
            missing = await service.fetch_values(s, b)
            msg = "Werte aus Home Assistant geladen" + (f" (ohne Statistik: {', '.join(missing)})" if missing else "")
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
    allocs = s.query(Allocation).filter(Allocation.active.is_(True), Allocation.source_type == "amount").all()
    return render(request, "billing.html", b=b, r=b.result or {}, entities=service.required_entities(s),
                  amount_allocs=allocs, ha_configured=bool(config.ha_url and config.ha_token))


@app.post("/billings/{bid}")
async def billing_save(request: Request, bid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    form = await request.form()
    action = form.get("action", "save")

    if action == "delete":
        s.delete(b)
        s.commit()
        return redirect("/", "Abrechnung gelöscht")
    if action == "reopen":
        b.status = "draft"
        s.commit()
        return redirect(f"/billings/{bid}", "Wieder zur Bearbeitung geöffnet")
    if b.status == "final":
        return redirect(f"/billings/{bid}", "Abrechnung ist abgeschlossen")

    b.title = str(form.get("title", b.title)).strip()
    b.invoice_no = str(form.get("invoice_no", b.invoice_no)).strip()
    b.period_start = date.fromisoformat(str(form.get("period_start", b.period_start.isoformat())))
    b.period_end = date.fromisoformat(str(form.get("period_end", b.period_end.isoformat())))
    b.grid_kwh = parse_float(form.get("grid_kwh"), None)
    b.energy_cost_net = parse_float(form.get("energy_cost_net")) or 0.0
    b.fixed_cost_net = parse_float(form.get("fixed_cost_net")) or 0.0
    b.vat_rate = (parse_float(form.get("vat_rate")) or 0.0) / 100
    b.battery_rate_ct = parse_float(form.get("battery_rate_ct")) or 0.0
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
            missing = await service.fetch_values(s, b)
            msg = "Werte aus Home Assistant geladen" + (f" (ohne Statistik: {', '.join(missing)})" if missing else "")
        except (HAError, Exception) as e:  # noqa: BLE001
            msg = f"Home Assistant: {e}"
    service.recompute(s, b)
    if action == "finalize":
        b.status = "final"
        msg = "Abrechnung abgeschlossen"
    s.commit()
    return redirect(f"/billings/{bid}", msg)


def _party_result(b: Billing, pid: int) -> dict:
    for p in (b.result or {}).get("parties", []):
        if p["id"] == pid:
            return p
    raise HTTPException(404, "Partei nicht in dieser Abrechnung")


def _invoice_html(request: Request, b: Billing, pid: int, pdf: bool = False) -> str:
    p = _party_result(b, pid)
    ctx = {"b": b, "r": b.result, "p": p, "ll": b.result.get("landlord", {}), "pdf": pdf,
           "due": b.created_at.date() + timedelta(days=int(parse_float(b.result.get("landlord", {}).get("payment_days"), 14) or 14))}
    return templates.get_template("invoice.html").render(request=request, **ctx)


def _pdf_name(b: Billing, p: dict) -> str:
    safe = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß_-]+", "_", p["name"]).strip("_")
    return f"Stromabrechnung_{b.period_start:%Y-%m}_{safe}.pdf"


def _render_pdf(html: str) -> bytes:
    from weasyprint import HTML  # lazy: braucht System-Libs (pango)

    return HTML(string=html, base_url=str(BASE)).write_pdf()


@app.get("/billings/{bid}/invoice/{pid}.pdf")
def invoice_pdf(request: Request, bid: int, pid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    p = _party_result(b, pid)
    pdf = _render_pdf(_invoice_html(request, b, pid, pdf=True))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{_pdf_name(b, p)}"'})


@app.get("/billings/{bid}/invoice/{pid}", response_class=HTMLResponse)
def invoice_view(request: Request, bid: int, pid: int, s: Session = Depends(get_session)):
    return HTMLResponse(_invoice_html(request, _get_billing(s, bid), pid))


@app.get("/billings/{bid}/all.zip")
def invoices_zip(request: Request, bid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in b.result.get("parties", []):
            z.writestr(_pdf_name(b, p), _render_pdf(_invoice_html(request, b, p["id"], pdf=True)))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="Stromabrechnung_{b.period_start:%Y-%m}.zip"'})
