"""Templates, Formatierung und PDF-Erzeugung der Abrechnungen."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from fastapi.templating import Jinja2Templates

from .db import Billing

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")


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
    if v is None or v == "":
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


def party_result(b: Billing, pid: int) -> Optional[dict]:
    for p in (b.result or {}).get("parties", []):
        if p["id"] == pid:
            return p
    return None


def period_text(b: Billing) -> str:
    return f"{fmt_date(b.period_start)} – {fmt_date(b.period_end)}"


def invoice_html(b: Billing, p: dict, pdf: bool = False) -> str:
    r = b.result or {}
    ll = r.get("landlord", {})
    days = int(parse_float(ll.get("payment_days"), 14) or 14)
    return templates.get_template("invoice.html").render(
        b=b, r=r, p=p, ll=ll, bld=r.get("building", {}), pdf=pdf,
        due=b.created_at.date() + timedelta(days=days),
    )


def render_pdf(html: str) -> bytes:
    from weasyprint import HTML  # lazy: braucht System-Libs (pango)

    return HTML(string=html, base_url=str(BASE)).write_pdf()


def invoice_pdf(b: Billing, p: dict) -> bytes:
    return render_pdf(invoice_html(b, p, pdf=True))


def pdf_name(b: Billing, p: dict) -> str:
    safe = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß_-]+", "_", p.get("unit_id") or p["name"]).strip("_")
    return f"Nebenkostenabrechnung_Strom_{b.period_start:%Y-%m}_{safe}.pdf"
