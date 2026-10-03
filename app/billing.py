"""Reine Berechnungslogik der Stromabrechnung (ohne Datenbank / Home Assistant).

Grundidee
---------
* Aus der Rechnung des Stromanbieters ergibt sich ein Durchschnittspreis je kWh
  (Arbeitspreis netto / bezogene kWh), netto und brutto (inkl. MwSt.).
* Der Gesamtverbrauch des Hauses kommt vom Victron-Zähler, die Batterie-Entladung
  ebenfalls aus der Victron-Anlage. Daraus ergibt sich der Batterieanteil
  ``batterie_kwh / gesamt_kwh`` für den Abrechnungszeitraum.
* Der Verbrauch jeder Partei (Summe ihrer Shelly-Zähler) wird anteilig aufgeteilt:
    - Netzanteil   -> kWh x Durchschnittspreis brutto
    - Batterieanteil -> kWh x Durchschnittspreis netto (ohne MwSt.) + Batterienutzungssatz
* Die Partei mit ``is_owner`` (Eigentümer/Hauptpartei) bekommt den Restverbrauch
  (Gesamt - alle anderen Parteien - über Umlagen verteilte Energie).
* Fixkosten des Anbieters (Grundpreis, Messstelle ...) werden brutto gleichmäßig
  auf alle Parteien verteilt; weitere Fixkosten (z. B. IPTV) gleichmäßig auf die
  ausgewählten Parteien.
* Umlagen (z. B. Warmwasser, Wasser) verteilen eine Energiemenge oder einen
  Betrag nach Verbrauchswerten je Partei (HA-Entitäten), gleich oder nach Prozent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional


@dataclass
class PartyCfg:
    id: int
    name: str
    meters: list[str] = field(default_factory=list)
    is_owner: bool = False
    address: str = ""
    unit: str = ""


@dataclass
class FixedCostCfg:
    name: str
    amount_gross: float
    party_ids: list[int] = field(default_factory=list)  # leer = alle Parteien


@dataclass
class AllocationCfg:
    id: int
    name: str
    source_type: str  # "energy" (kWh aus HA-Entität) | "amount" (Betrag in EUR)
    source_entity: str = ""
    amount: float = 0.0  # bei source_type == "amount"
    key_type: str = "equal"  # "entity" | "percent" | "equal"
    key: dict[int, str | float] = field(default_factory=dict)
    key_unit: str = ""


@dataclass
class BillCfg:
    grid_kwh: float
    energy_cost_net: float
    fixed_cost_net: float
    vat_rate: float  # z. B. 0.19
    battery_rate_ct: float  # Batterienutzungssatz ct/kWh (ohne MwSt.)


@dataclass
class Line:
    label: str
    amount: float
    quantity: Optional[float] = None
    unit: str = ""
    unit_price: Optional[float] = None
    note: str = ""
    vat_included: bool = True

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "amount": self.amount,
            "quantity": self.quantity,
            "unit": self.unit,
            "unit_price": self.unit_price,
            "note": self.note,
            "vat_included": self.vat_included,
        }


def _r(x: float, digits: int = 2) -> float:
    """Kaufmännisch runden (0,005 -> 0,01)."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP)) + 0.0


def _de(x: float, digits: int = 2) -> str:
    """Zahl im deutschen Format (1.234,56)."""
    return f"{x:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(x: float) -> str:
    return _de(x * 100, 1) + " %"


def _val(values: dict[str, Optional[float]], entity: str) -> float:
    if not entity:
        return 0.0
    v = values.get(entity)
    return float(v) if v is not None else 0.0


def _split_equal(amount: float, n: int) -> list[float]:
    """Teilt einen Betrag centgenau auf n Teile (Rundungsrest auf die ersten)."""
    if n <= 0:
        return []
    cents = round(amount * 100)
    base, rest = divmod(cents, n)
    return [(base + (1 if i < rest else 0)) / 100 for i in range(n)]


def missing_entities(required: list[str], values: dict[str, Optional[float]]) -> list[str]:
    return [e for e in required if e and values.get(e) is None]


def compute(
    bill: BillCfg,
    parties: list[PartyCfg],
    fixed_costs: list[FixedCostCfg],
    allocations: list[AllocationCfg],
    values: dict[str, Optional[float]],
    total_entity: str = "",
    battery_entity: str = "",
    grid_entity: str = "",
) -> dict:
    warnings: list[str] = []

    # --- Preise aus der Rechnung -------------------------------------------------
    if bill.grid_kwh > 0:
        price_net = bill.energy_cost_net / bill.grid_kwh
    else:
        price_net = 0.0
        warnings.append("Bezogene kWh laut Rechnung fehlen – Durchschnittspreis = 0.")
    price_gross = price_net * (1 + bill.vat_rate)
    battery_rate = bill.battery_rate_ct / 100.0
    bill_gross = (bill.energy_cost_net + bill.fixed_cost_net) * (1 + bill.vat_rate)

    # --- Messwerte ---------------------------------------------------------------
    total_kwh = _val(values, total_entity) if total_entity else None
    battery_kwh = _val(values, battery_entity) if battery_entity else 0.0
    grid_meter_kwh = values.get(grid_entity) if grid_entity else None

    if grid_meter_kwh is not None and bill.grid_kwh > 0:
        dev = abs(grid_meter_kwh - bill.grid_kwh) / bill.grid_kwh
        if dev > 0.05:
            warnings.append(
                f"Netzbezug laut Zähler ({grid_meter_kwh:.1f} kWh) weicht um {dev:.0%} "
                f"von der Rechnung ({bill.grid_kwh:.1f} kWh) ab."
            )

    if total_kwh and total_kwh > 0:
        battery_share = min(1.0, max(0.0, battery_kwh / total_kwh))
        if battery_kwh > total_kwh:
            warnings.append("Batterie-Entladung ist größer als der Gesamtverbrauch – Anteil auf 100 % begrenzt.")
    else:
        battery_share = 0.0
        if battery_kwh:
            warnings.append("Kein Gesamtverbrauch vorhanden – Batterieanteil kann nicht berechnet werden.")

    def energy_cost(kwh: float) -> tuple[float, float, float, float, float]:
        """-> (netz_kwh, netz_eur, batt_kwh, batt_energie_eur, batt_nutzung_eur)"""
        g = kwh * (1 - battery_share)
        b = kwh * battery_share
        return g, g * price_gross, b, b * price_net, b * battery_rate

    # --- Verbrauch je Partei -----------------------------------------------------
    party_kwh: dict[int, float] = {}
    for p in parties:
        party_kwh[p.id] = sum(_val(values, m) for m in p.meters)

    energy_alloc_kwh = sum(
        _val(values, a.source_entity) for a in allocations if a.source_type == "energy"
    )

    owners = [p for p in parties if p.is_owner]
    if len(owners) > 1:
        warnings.append("Mehr als eine Partei ist als Eigentümer markiert – Rest geht an die erste.")
    owner = owners[0] if owners else None
    rest_kwh = None
    if owner is not None and total_kwh is not None:
        others = sum(v for pid, v in party_kwh.items() if pid != owner.id)
        rest_kwh = total_kwh - others - energy_alloc_kwh
        if rest_kwh < 0:
            warnings.append(
                f"Restverbrauch für {owner.name} wäre negativ ({rest_kwh:.1f} kWh) – auf 0 gesetzt. "
                "Zählerzuordnung prüfen."
            )
            rest_kwh = 0.0
        party_kwh[owner.id] = rest_kwh
    elif owner is None and total_kwh is not None:
        unassigned = total_kwh - sum(party_kwh.values()) - energy_alloc_kwh
        if unassigned > 0.5:
            warnings.append(
                f"{unassigned:.1f} kWh sind keiner Partei zugeordnet (keine Eigentümer-Partei festgelegt)."
            )

    lines: dict[int, list[Line]] = {p.id: [] for p in parties}

    for p in parties:
        kwh = party_kwh[p.id]
        g, g_eur, b, b_eur, b_use = energy_cost(kwh)
        label_suffix = " (Restverbrauch Haus)" if owner is not None and p.id == owner.id and rest_kwh is not None else ""
        lines[p.id].append(
            Line(f"Netzstrom{label_suffix}", _r(g_eur), _r(g, 3), "kWh", price_gross,
                 note="Durchschnittspreis lt. Rechnung inkl. MwSt.")
        )
        if b > 0 or battery_share > 0:
            lines[p.id].append(
                Line("Batteriestrom – Energie netto", _r(b_eur), _r(b, 3), "kWh", price_net,
                     note="Durchschnittspreis ohne MwSt.", vat_included=False)
            )
            lines[p.id].append(
                Line("Batteriestrom – Nutzungssatz", _r(b_use), _r(b, 3), "kWh", battery_rate,
                     note="ohne MwSt.", vat_included=False)
            )

    # --- Fixkosten Stromanbieter (Rechnung) --------------------------------------
    if parties and bill.fixed_cost_net:
        fixed_gross = bill.fixed_cost_net * (1 + bill.vat_rate)
        for p, share in zip(parties, _split_equal(fixed_gross, len(parties))):
            lines[p.id].append(
                Line("Fixkosten Stromanbieter (Grundpreis/Messstelle) anteilig", share,
                     note=f"{_de(fixed_gross)} € / {len(parties)} Parteien")
            )

    # --- weitere Fixkosten -------------------------------------------------------
    for fc in fixed_costs:
        targets = [p for p in parties if not fc.party_ids or p.id in fc.party_ids]
        if not targets or not fc.amount_gross:
            continue
        for p, share in zip(targets, _split_equal(fc.amount_gross, len(targets))):
            note = f"{_de(fc.amount_gross)} € / {len(targets)} Parteien" if len(targets) > 1 else ""
            lines[p.id].append(Line(fc.name, share, note=note))

    # --- Umlagen -----------------------------------------------------------------
    for a in allocations:
        if a.source_type == "energy":
            src_kwh = _val(values, a.source_entity)
            g, g_eur, b, b_eur, b_use = energy_cost(src_kwh)
            pot = g_eur + b_eur + b_use
            pot_desc = f"{_de(src_kwh, 1)} kWh = {_de(pot)} €"
        else:
            pot = a.amount
            pot_desc = f"{_de(pot)} €"
        if not pot:
            continue

        targets = [p for p in parties]
        weights: dict[int, float] = {}
        if a.key_type == "entity":
            targets = [p for p in parties if a.key.get(p.id)]
            weights = {p.id: _val(values, str(a.key[p.id])) for p in targets}
        elif a.key_type == "percent":
            targets = [p for p in parties if a.key.get(p.id)]
            weights = {p.id: float(a.key[p.id]) for p in targets}
        else:
            weights = {p.id: 1.0 for p in targets}

        wsum = sum(weights.values())
        if not targets:
            warnings.append(f"Umlage „{a.name}“ hat keine Parteien zugeordnet.")
            continue
        if wsum <= 0:
            warnings.append(f"Umlage „{a.name}“: keine Verbrauchswerte – gleichmäßig verteilt.")
            weights = {p.id: 1.0 for p in targets}
            wsum = float(len(targets))

        for p in targets:
            share = weights[p.id] / wsum
            if a.key_type == "entity":
                qty = f"{_de(weights[p.id])} {a.key_unit} von {_de(wsum)} {a.key_unit}"
                note = f"{qty} ({_pct(share)}) von {pot_desc}"
            else:
                note = f"{_pct(share)} von {pot_desc}"
            lines[p.id].append(Line(a.name, _r(pot * share), note=note))

    # --- Ergebnis ----------------------------------------------------------------
    result_parties = []
    for p in parties:
        pl = lines[p.id]
        total = _r(sum(l.amount for l in pl))
        result_parties.append(
            {
                "id": p.id,
                "name": p.name,
                "address": p.address,
                "unit": p.unit,
                "is_owner": p.is_owner,
                "kwh": _r(party_kwh[p.id], 3),
                "lines": [l.as_dict() for l in pl],
                "total": total,
            }
        )

    charged = _r(sum(rp["total"] for rp in result_parties))
    return {
        "price_net": price_net,
        "price_gross": price_gross,
        "battery_rate": battery_rate,
        "battery_share": battery_share,
        "total_kwh": total_kwh,
        "battery_kwh": battery_kwh,
        "grid_meter_kwh": grid_meter_kwh,
        "bill_gross": _r(bill_gross),
        "charged_total": charged,
        "difference": _r(charged - bill_gross),
        "parties": result_parties,
        "warnings": warnings,
    }
