"""Verbindet Datenbank, Home Assistant, Berechnung und Versand."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from . import billing as calc
from . import mailer, render
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
    out: list[tuple[str, str]] = [(st[k], role) for k, role in HOUSE_ENTITIES if st[k]]
    parties = active_parties(s)
    names = {p.id: p.name for p in parties}
    for p in parties:
        for m in p.meters or []:
            out.append((m, f"Zähler {p.name}"))
    for a in s.query(Allocation).filter(Allocation.active.is_(True)).all():
        if a.source_type == "energy" and a.source_entity:
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
    for a in s.query(Allocation).filter(Allocation.active.is_(True)).order_by(Allocation.id).all():
        amount = (b.amounts or {}).get(str(a.id), a.default_amount)
        allocs.append(
            calc.AllocationCfg(
                id=a.id, name=a.name, source_type=a.source_type, source_entity=a.source_entity,
                amount=float(amount or 0), key_type=a.key_type, key_unit=a.key_unit,
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
        pv_rate_on_battery=bool(b.pv_rate_on_battery),
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
