"""Verbindet Datenbank, Home Assistant, Berechnung und Versand."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from . import billing as calc
from . import invoice_import, mailer, render
from .config import config
from .db import Allocation, Billing, FixedCost, Party, get_settings
from .ha import HAClient

# Einstellungs-Schlüssel der Haus-Entitäten und ihre Rolle
HOUSE_ENTITIES = [
    ("entity_grid", "Netzbezug (Stromzähler)"),
    ("entity_total", "Gesamtverbrauch Haus (Victron)"),
    ("entity_battery", "Batterie entladen (Victron)"),
    ("entity_battery_charge", "Batterie geladen gesamt (Victron)"),
    ("entity_battery_charge_grid", "Batterie aus Netz geladen – dyn. ESS (Victron)"),
    ("entity_pv_direct", "PV-Direktverbrauch (optional)"),
]


def _num(v: str) -> float:
    try:
        return float(str(v).replace(",", ".")) if str(v).strip() else 0.0
    except ValueError:
        return 0.0


def water_price_parts(st: dict[str, str]) -> list[tuple[str, float]]:
    """Wasser- und Abwasserpreis je m³ aus den Einstellungen."""
    return [("Wasser", _num(st["water_price_m3"])), ("Abwasser", _num(st["sewage_price_m3"]))]


def active_parties(s: Session) -> list[Party]:
    return s.query(Party).filter(Party.active.is_(True)).order_by(Party.sort, Party.id).all()


def energy_entities(st: dict[str, str]) -> calc.EnergyEntities:
    return calc.EnergyEntities(
        total=st["entity_total"], grid=st["entity_grid"], battery_discharge=st["entity_battery"],
        battery_charge_total=st["entity_battery_charge"], battery_charge_grid=st["entity_battery_charge_grid"],
        pv_direct=st["entity_pv_direct"],
    )


def required_entities(s: Session) -> list[tuple[str, str]]:
    """Alle Entitäten, deren Verbrauch für eine Abrechnung gebraucht wird: (entity_id, Rolle)."""
    st = get_settings(s)
    out: list[tuple[str, str]] = [(e, role) for k, role in HOUSE_ENTITIES for e in calc.split_ids(st[k])]
    parties = active_parties(s)
    names = {p.id: p.name for p in parties}
    for p in parties:
        for m in p.meters or []:
            out.append((m, f"Zähler {p.name}"))
    for a in s.query(Allocation).filter(Allocation.active.is_(True)).all():
        if a.source_type in ("energy", "quantity") and a.source_entity:
            out.append((a.source_entity, f"Umlage {a.name} (Quelle)"))
        if a.key_type == "entity":
            for pid, ent in (a.key or {}).items():
                if ent and int(pid) in names:
                    out.append((str(ent), f"Umlage {a.name}: {names[int(pid)]}"))
    seen: set[str] = set()
    uniq = []
    for e, role in out:
        if e not in seen:
            seen.add(e)
            uniq.append((e, role))
    return uniq


async def fetch_values(s: Session, b: Billing) -> list[str]:
    """Holt Verbrauchswerte aus HA. Gibt Entitäten ohne Statistik zurück."""
    ents = [e for e, _ in required_entities(s)]
    client = HAClient(config.ha_url, config.ha_token)
    fetched = await client.consumption(ents, b.period_start, b.period_end, config.timezone)
    values = dict(b.values or {})
    missing = []
    for e, v in fetched.items():
        if v is None:
            missing.append(e)
        else:
            values[e] = round(v, 4)
    b.values = values
    st = get_settings(s)
    if not b.grid_kwh and st["entity_grid"] and values.get(st["entity_grid"]) is not None:
        b.grid_kwh = values[st["entity_grid"]]
    b.fetched_at = datetime.now()
    return missing


def recompute(s: Session, b: Billing) -> dict:
    st = get_settings(s)
    db_parties = active_parties(s)
    parties = [
        calc.PartyCfg(id=p.id, name=p.name, meters=list(p.meters or []), is_owner=p.is_owner,
                      address=p.address, unit=p.unit)
        for p in db_parties
    ]
    fixed = [
        calc.FixedCostCfg(name=f.name, amount_gross=f.amount_gross, party_ids=[int(x) for x in f.party_ids or []])
        for f in s.query(FixedCost).filter(FixedCost.active.is_(True)).order_by(FixedCost.id).all()
    ]
    allocs = []
    water_parts = water_price_parts(st)
    for a in s.query(Allocation).filter(Allocation.active.is_(True)).order_by(Allocation.id).all():
        amount = (b.amounts or {}).get(str(a.id), a.default_amount)
        parts = water_parts if a.source_type == "quantity" and a.price_source == "water" else []
        allocs.append(
            calc.AllocationCfg(
                id=a.id, name=a.name, source_type=a.source_type, source_entity=a.source_entity,
                amount=float(amount or 0), source_unit=a.source_unit or "", price_parts=parts,
                key_type=a.key_type,
                key_unit=a.key_unit,
                key={int(k): v for k, v in (a.key or {}).items() if v not in ("", None)},
            )
        )
    bill = calc.BillCfg(
        grid_kwh=b.grid_kwh or 0.0,
        energy_cost_net=b.energy_cost_net,
        fixed_cost_net=b.fixed_cost_net,
        vat_rate=b.vat_rate,
        wear_rate_ct=b.battery_rate_ct,
        spot_price_ct=b.spot_price_ct or 0.0,
        pv_rate_ct=b.pv_rate_ct or 0.0,
    )
    result = calc.compute(bill, parties, fixed, allocs, b.values or {}, energy_entities(st))
    missing = calc.missing_entities([e for e, _ in required_entities(s)], b.values or {})
    if missing:
        result["warnings"].insert(0, f"Fehlende Messwerte (als 0 gerechnet): {', '.join(missing)}")
    extra = {p.id: p for p in db_parties}
    for rp in result["parties"]:
        rp["unit_id"] = extra[rp["id"]].unit_id or ""
    result["landlord"] = {
        k: st[k] for k in ("landlord_name", "landlord_address", "landlord_contact", "landlord_iban",
                           "payment_days", "invoice_text")
    }
    result["building"] = {k: st[k] for k in ("building_title", "building_address", "building_id")}
    result["computed_at"] = datetime.now().isoformat(timespec="seconds")
    b.result = result
    return result


# --------------------------------------------------------------------------- E-Mail
def _split_addr(text: str) -> list[str]:
    return [a.strip() for a in (text or "").replace(";", ",").split(",") if a.strip()]


def send_invoices(s: Session, b: Billing, party_ids: list[int] | None = None,
                  only_unsent: bool = False) -> list[tuple[str, bool, str]]:
    """Verschickt die PDFs per E-Mail. Ergebnis: (Partei, ok, Meldung)."""
    st = get_settings(s)
    parties = {p.id: p for p in s.query(Party).all()}
    sent = dict(b.sent or {})
    report = []
    for rp in (b.result or {}).get("parties", []):
        pid = rp["id"]
        if party_ids is not None and pid not in party_ids:
            continue
        if only_unsent and sent.get(str(pid), {}).get("ok"):
            continue
        party = parties.get(pid)
        to = _split_addr(party.email if party else "")
        if not to:
            if party_ids is not None:
                report.append((rp["name"], False, "keine E-Mail-Adresse hinterlegt"))
            continue
        fields = defaultdict(str, {
            "name": rp["name"],
            "wohneinheit": rp["name"],
            "we_id": rp.get("unit_id", ""),
            "zeitraum": render.period_text(b),
            "betrag": render.fmt_eur(rp["total"]),
            "absender": st["landlord_name"],
            "gebaeude": st["building_address"],
        })
        try:
            mailer.send_mail(
                to=to,
                subject=st["mail_subject"].format_map(fields),
                body=st["mail_body"].format_map(fields),
                attachments=[(render.pdf_name(b, rp), render.invoice_pdf(b, rp))],
                bcc=_split_addr(st["mail_bcc"]),
            )
            sent[str(pid)] = {"ok": True, "at": datetime.now().isoformat(timespec="seconds"), "to": ", ".join(to)}
            report.append((rp["name"], True, ", ".join(to)))
        except Exception as e:  # noqa: BLE001
            sent[str(pid)] = {"ok": False, "at": datetime.now().isoformat(timespec="seconds"), "error": str(e)}
            report.append((rp["name"], False, str(e)))
    b.sent = sent
    return report


# --------------------------------------------------------------------------- Rechnungsimport
def invoice_dir() -> Path:
    base = Path(config.data_dir) if config.data_dir else (
        Path(config.database_url.removeprefix("sqlite:///")).parent
        if config.database_url.startswith("sqlite:///") else Path("data"))
    d = base / "invoices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_duplicate(s: Session, inv: invoice_import.ParsedInvoice, message_id: str = "") -> Billing | None:
    q = s.query(Billing)
    if inv.invoice_no:
        hit = q.filter(Billing.invoice_no == inv.invoice_no).first()
        if hit:
            return hit
    if message_id:
        return q.filter(Billing.mail_message_id == message_id).first()
    return None


def create_from_invoice(s: Session, inv: invoice_import.ParsedInvoice, pdf: bytes, filename: str,
                        message_id: str = "") -> Billing:
    """Legt einen Abrechnungs-Entwurf aus einer importierten Rechnung an."""
    st = get_settings(s)
    b = Billing(
        title=f"Strom {inv.period_start:%m/%Y}" if inv.period_start else "Strom",
        invoice_no=inv.invoice_no,
        period_start=inv.period_start,
        period_end=inv.period_end,
        grid_kwh=inv.grid_kwh,
        energy_cost_net=inv.energy_cost_net,
        fixed_cost_net=inv.fixed_cost_net,
        spot_price_ct=inv.spot_price_ct or 0.0,
        vat_rate=inv.vat_rate if inv.vat_rate is not None else (float(st["vat_rate"] or 19) / 100),
        battery_rate_ct=float(st["battery_rate_ct"].replace(",", ".") or 0),
        pv_rate_ct=float(st["pv_rate_ct"].replace(",", ".") or 0),
        values={}, amounts={}, result={}, sent={},
        import_info={**inv.info(), "filename": filename},
        mail_message_id=message_id,
        notes=f"Automatisch importiert aus {filename}",
    )
    s.add(b)
    s.flush()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    path = invoice_dir() / f"{b.id}_{safe}"
    path.write_bytes(pdf)
    b.source_file = str(path)
    return b


async def import_invoice(s: Session, pdf: bytes, filename: str, message_id: str = "",
                         source: str = "Upload") -> tuple[Billing | None, str]:
    """Kompletter Ablauf: PDF lesen, Entwurf anlegen, HA-Werte laden, berechnen,
    je nach Einstellung „import_mode“ als Entwurf belassen oder automatisch abschließen
    und versenden, anschließend benachrichtigen."""
    inv = invoice_import.parse_pdf(pdf)
    dup = find_duplicate(s, inv, message_id)
    if dup:
        return dup, f"Rechnung {inv.invoice_no or filename} ist bereits erfasst."
    b = create_from_invoice(s, inv, pdf, filename, message_id)
    s.commit()
    msgs = [f"Rechnung {inv.invoice_no} ({inv.period_start:%d.%m.%Y} – {inv.period_end:%d.%m.%Y}) importiert"]
    if config.ha_url and config.ha_token:
        try:
            missing = await fetch_values(s, b)
            msgs.append("Werte aus Home Assistant geladen" + (f" (ohne Statistik: {', '.join(missing)})" if missing else ""))
        except Exception as e:  # noqa: BLE001
            msgs.append(f"Home Assistant nicht erreichbar: {e}")
    result = recompute(s, b)
    warnings = list(inv.warnings) + list(result.get("warnings", []))
    st = get_settings(s)
    mode = st["import_mode"]
    if mode == "auto_always" or (mode == "auto_if_clean" and not warnings):
        # Vollautomatisch: ohne manuelle Prüfung abschließen und an alle Parteien mit Adresse senden
        b.status = "final"
        msgs.append("automatisch abgeschlossen" + (f" trotz {len(warnings)} Hinweis(en)" if warnings else ""))
        if mailer.configured():
            s.commit()
            report = send_invoices(s, b, only_unsent=True)
            ok = sum(1 for _, good, _ in report if good)
            msgs.append(f"{ok} Abrechnung(en) versendet" + (f", {len(report) - ok} fehlgeschlagen" if len(report) > ok else ""))
        else:
            msgs.append("nicht versendet: SMTP nicht konfiguriert")
    else:
        msgs.append(f"Entwurf – bitte prüfen ({len(warnings)} Hinweis(e))" if warnings else "Entwurf – bitte prüfen")
    s.commit()
    text_msg = " · ".join(msgs)
    if st["notify_email"] and mailer.configured():
        link = f"{config.app_base_url}/billings/{b.id}" if config.app_base_url else f"/billings/{b.id}"
        body = (f"Neue Stromrechnung über {source} eingegangen.\n\n{text_msg}\n\n"
                + ("Hinweise:\n- " + "\n- ".join(warnings) + "\n\n" if warnings else "")
                + f"Abrechnung: {link}\n")
        try:
            mailer.send_mail(_split_addr(st["notify_email"]), f"Stromrechnung importiert: {b.title}", body, [])
        except Exception:  # noqa: BLE001
            pass
    return b, text_msg
