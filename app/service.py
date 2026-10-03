"""Verbindet Datenbank, Home Assistant und Berechnungslogik."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from . import billing as calc
from .config import config
from .db import Allocation, Billing, FixedCost, Party, get_settings
from .ha import HAClient


def active_parties(s: Session) -> list[Party]:
    return s.query(Party).filter(Party.active.is_(True)).order_by(Party.sort, Party.id).all()


def required_entities(s: Session) -> list[tuple[str, str]]:
    """Alle Entitäten, deren Verbrauch für eine Abrechnung gebraucht wird: (entity_id, Rolle)."""
    st = get_settings(s)
    out: list[tuple[str, str]] = []
    if st["entity_grid"]:
        out.append((st["entity_grid"], "Netzbezug (Stromzähler)"))
    if st["entity_total"]:
        out.append((st["entity_total"], "Gesamtverbrauch (Victron)"))
    if st["entity_battery"]:
        out.append((st["entity_battery"], "Batterie-Entladung (Victron)"))
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
    parties = [
        calc.PartyCfg(id=p.id, name=p.name, meters=list(p.meters or []), is_owner=p.is_owner,
                      address=p.address, unit=p.unit)
        for p in active_parties(s)
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
        battery_rate_ct=b.battery_rate_ct,
    )
    result = calc.compute(
        bill, parties, fixed, allocs, b.values or {},
        total_entity=st["entity_total"], battery_entity=st["entity_battery"], grid_entity=st["entity_grid"],
    )
    missing = calc.missing_entities([e for e, _ in required_entities(s)], b.values or {})
    if missing:
        result["warnings"].insert(0, f"Fehlende Messwerte (als 0 gerechnet): {', '.join(missing)}")
    result["landlord"] = {
        k: st[k] for k in ("landlord_name", "landlord_address", "landlord_contact", "landlord_iban",
                           "payment_days", "invoice_text")
    }
    result["computed_at"] = datetime.now().isoformat(timespec="seconds")
    b.result = result
    return result
