"""Home-Assistant-Anbindung.

Verbrauchswerte werden aus der *Langzeitstatistik* (recorder) gelesen, nicht aus
der History: die History wird standardmäßig nach 10 Tagen gelöscht, die
Statistik bleibt dauerhaft erhalten. Voraussetzung: die Entität hat eine
``state_class`` (``total``/``total_increasing``) – bei Shelly-, Victron- und
Zähler-Energiesensoren ist das in der Regel der Fall.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
import websockets


class HAError(Exception):
    pass


def period_bounds(start: date, end: date, tz: str) -> tuple[datetime, datetime]:
    """Abrechnungszeitraum (Enddatum inklusive) -> lokale Start-/Endzeit."""
    zone = ZoneInfo(tz)
    return (
        datetime.combine(start, time.min, tzinfo=zone),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone),
    )


def sum_changes(rows: list[dict]) -> Optional[float]:
    """Summiert die stündlichen Änderungen einer Statistik."""
    if not rows:
        return None
    if any("change" in r for r in rows):
        return sum(float(r["change"]) for r in rows if r.get("change") is not None)
    # Fallback für ältere HA-Versionen ohne "change": Differenz der "sum"-Werte.
    sums = [float(r["sum"]) for r in rows if r.get("sum") is not None]
    if len(sums) < 2:
        return None
    return sums[-1] - sums[0]


class HAClient:
    def __init__(self, url: str, token: str, timeout: float = 30.0):
        if not url or not token:
            raise HAError("HA_URL oder HA_TOKEN ist nicht gesetzt (siehe .env).")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def check(self) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.url}/api/", headers=self._headers)
        if r.status_code == 401:
            raise HAError("Token ungültig (401).")
        r.raise_for_status()
        return r.json().get("message", "OK")

    async def states(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.url}/api/states", headers=self._headers)
        r.raise_for_status()
        return r.json()

    async def _ws_calls(self, payloads: list[dict]) -> list:
        """Mehrere WebSocket-Befehle über eine Verbindung. Ergebnis je Befehl oder HAError."""
        ws_url = self.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket"
        async with websockets.connect(ws_url, max_size=None, open_timeout=self.timeout) as ws:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise HAError(f"Unerwartete Antwort: {msg}")
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                raise HAError("Anmeldung an Home Assistant fehlgeschlagen (Token prüfen).")
            results: dict[int, object] = {}
            for i, payload in enumerate(payloads, start=1):
                await ws.send(json.dumps({"id": i, **payload}))
            while len(results) < len(payloads):
                msg = json.loads(await ws.recv())
                if msg.get("type") == "result" and msg.get("id") in range(1, len(payloads) + 1):
                    results[msg["id"]] = (msg.get("result") if msg.get("success")
                                          else HAError(f"HA-Fehler: {msg.get('error')}"))
            return [results[i] for i in range(1, len(payloads) + 1)]

    async def _ws_call(self, payload: dict) -> dict:
        (result,) = await self._ws_calls([payload])
        if isinstance(result, HAError):
            raise result
        return result or {}

    async def entities(self) -> list[dict]:
        """Sensoren für die Entitäten-Auswahl.

        Nutzt die WebSocket-API (``get_states`` überspringt Entitäten mit fehlerhaften
        Attributen einzeln, während ``/api/states`` dann komplett mit 500 abbricht)
        und ergänzt alle Statistik-IDs aus dem Recorder. REST dient nur als Rückfall.
        """
        errors = []
        try:
            states, stats = await self._ws_calls([
                {"type": "get_states"},
                {"type": "recorder/list_statistic_ids", "statistic_type": "sum"},
            ])
        except Exception as e:  # noqa: BLE001  (z. B. WebSocket durch Proxy blockiert)
            errors.append(f"WebSocket: {e}")
            states, stats = None, None
            try:
                states = await self.states()
            except Exception as e2:  # noqa: BLE001
                errors.append(f"REST: {e2}")
        if isinstance(states, HAError):
            errors.append(f"Zustände: {states}")
            states = None
        if isinstance(stats, HAError):
            errors.append(f"Statistiken: {stats}")
            stats = None
        if states is None and stats is None:
            raise HAError("; ".join(errors))
        return merge_entities(states or [], stats or [])
    async def consumption(
        self, entity_ids: list[str], start: date, end: date, tz: str
    ) -> dict[str, Optional[float]]:
        """Verbrauch je Entität im Zeitraum (Energie in kWh, Volumen in m³)."""
        ids = sorted({e for e in entity_ids if e})
        if not ids:
            return {}
        t0, t1 = period_bounds(start, end, tz)
        result = await self._ws_call(
            {
                "type": "recorder/statistics_during_period",
                "start_time": t0.isoformat(),
                "end_time": t1.isoformat(),
                "statistic_ids": ids,
                "period": "hour",
                "types": ["change", "sum"],
                "units": {"energy": "kWh", "volume": "m³"},
            }
        )
        return {e: sum_changes(result.get(e, [])) for e in ids}


def merge_entities(states: list[dict], stats: list[dict]) -> list[dict]:
    """Führt Zustände (Sensoren) und Recorder-Statistik-IDs zu einer Auswahlliste zusammen."""
    stat_by_id = {st.get("statistic_id"): st for st in stats if st.get("statistic_id")}
    out: dict[str, dict] = {}
    for st in states:
        eid = st.get("entity_id", "")
        if not eid.startswith("sensor."):
            continue
        attrs = st.get("attributes") or {}
        out[eid] = {
            "entity_id": eid,
            "name": attrs.get("friendly_name", "") or "",
            "unit": attrs.get("unit_of_measurement", "") or "",
            "state": st.get("state"),
            "device_class": attrs.get("device_class", "") or "",
            "statistics": attrs.get("state_class") in ("total", "total_increasing") or eid in stat_by_id,
        }
    for sid, st in stat_by_id.items():
        if sid in out:
            continue
        out[sid] = {  # Statistik ohne aktuellen Zustand, z. B. externe Statistik eines Adapters
            "entity_id": sid,
            "name": st.get("name") or "",
            "unit": st.get("display_unit_of_measurement") or st.get("statistics_unit_of_measurement") or "",
            "state": "",
            "device_class": "",
            "statistics": True,
        }
    return sorted(out.values(), key=lambda x: x["entity_id"])
