"""VRM (Victron Remote Management, Cloud) als optionale dritte Datenquelle.

VRM liefert für beliebige vergangene Zeiträume die Energieflüsse der Anlage nach Quelle
(``/v2/installations/{id}/stats?type=kwh``). Die Codes:

    Gc Netz → Verbraucher      Gb Netz → Batterie
    Pc PV → Verbraucher        Pb PV → Batterie        Pg PV → Netz
    Bc Batterie → Verbraucher  Bg Batterie → Netz

Daraus werden zusätzlich zusammengesetzte Werte gebildet (z. B. Netzbezug = Gc + Gb).
In den Entitätsfeldern wählbar als ``vrm:<schlüssel>``. Token und Zugang kommen aus der
Umgebung (VRM_TOKEN), die Anlagen-ID aus den Einstellungen.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import httpx

from .config import config
from .ha import period_bounds

PREFIX = "vrm:"
API = "https://vrmapi.victronenergy.com/v2"

CODES = {
    "Gc": "Netz → Verbraucher",
    "Gb": "Netz → Batterie",
    "Pc": "PV → Verbraucher",
    "Pb": "PV → Batterie",
    "Pg": "PV → Netz",
    "Bc": "Batterie → Verbraucher",
    "Bg": "Batterie → Netz",
}
# zusammengesetzte Werte
DERIVED = {
    "grid_import": (["Gc", "Gb"], "Netzbezug (Gc + Gb)"),
    "grid_export": (["Pg", "Bg"], "Einspeisung (Pg + Bg)"),
    "consumption": (["Gc", "Pc", "Bc"], "Verbrauch (Gc + Pc + Bc)"),
    "battery_charged": (["Gb", "Pb"], "Batterie geladen (Gb + Pb)"),
    "battery_discharged": (["Bc", "Bg"], "Batterie entladen (Bc + Bg)"),
    "pv": (["Pc", "Pb", "Pg"], "PV-Erzeugung (Pc + Pb + Pg)"),
}
# Vergleich mit den Haus-Feldern
COMPARE = [
    ("entity_grid", "Netzbezug", ["grid_import"]),
    ("entity_total", "Gesamtverbrauch", ["consumption"]),
    ("entity_battery", "Batterie entladen", ["battery_discharged"]),
    ("entity_battery_charge", "Batterie geladen", ["battery_charged"]),
    ("entity_battery_charge_grid", "Netz → Batterie", ["Gb"]),
]


class VRMError(Exception):
    pass


def configured() -> bool:
    return bool(config.vrm_token)


def is_vrm(entity: str) -> bool:
    return entity.startswith(PREFIX)


def label(key: str) -> str:
    if key in CODES:
        return f"{CODES[key]} ({key})"
    return DERIVED[key][1] if key in DERIVED else key


class VRMClient:
    def __init__(self, token: str, timeout: float = 30.0):
        if not token:
            raise VRMError("VRM_TOKEN ist nicht gesetzt (siehe .env / Portainer).")
        self.token = token
        self.timeout = timeout

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(API + path, params=params, headers={"X-Authorization": f"Token {self.token}"})
        if r.status_code in (401, 403):
            raise VRMError("VRM: Token ungültig oder ohne Berechtigung.")
        r.raise_for_status()
        data = r.json()
        if not data.get("success", True):
            raise VRMError(f"VRM: {data.get('errors') or data}")
        return data

    async def installations(self) -> list[dict]:
        me = await self._get("/users/me")
        uid = me.get("user", {}).get("id")
        data = await self._get(f"/users/{uid}/installations")
        return [{"id": i.get("idSite"), "name": i.get("name", "")} for i in data.get("records", [])]

    async def totals(self, site: int, t0: datetime, t1: datetime) -> dict[str, float]:
        data = await self._get(f"/installations/{site}/stats", {
            "type": "kwh", "interval": "hours",
            "start": int(t0.timestamp()), "end": int(t1.timestamp()),
        })
        tot = data.get("totals") or {}
        return {k: float(v) for k, v in tot.items() if isinstance(v, (int, float)) and k in CODES}


def resolve(totals: dict[str, float], key: str) -> Optional[float]:
    if key in CODES:
        return totals.get(key, 0.0) if totals else None
    if key in DERIVED:
        parts = DERIVED[key][0]
        return sum(totals.get(p, 0.0) for p in parts) if totals else None
    return None


async def consumption(site: str, entity_ids: list[str], start: Optional[date], end: Optional[date], tz: str,
                      bounds: Optional[tuple[datetime, datetime]] = None,
                      ) -> tuple[dict[str, Optional[float]], dict[str, dict]]:
    """Werte je ``vrm:<schlüssel>`` für den Zeitraum (eine API-Abfrage)."""
    if not entity_ids:
        return {}, {}
    if not site:
        raise VRMError("VRM-Anlagen-ID fehlt (Einstellungen → VRM).")
    t0, t1 = bounds or period_bounds(start, end, tz)
    totals = await VRMClient(config.vrm_token).totals(int(site), t0, t1)
    values = {e: resolve(totals, e[len(PREFIX):]) for e in entity_ids}
    meta = {e: {"method": "vrm"} for e in entity_ids if values[e] is not None}
    return values, meta


def virtual_entities() -> list[dict]:
    keys = list(DERIVED) + list(CODES)
    return [{"entity_id": PREFIX + k, "name": f"VRM (Cloud) – {label(k)}", "unit": "kWh", "state": "",
             "device_class": "energy", "statistics": True} for k in keys]
