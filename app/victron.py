"""Victron direkt: eigener Logger, der den GX per Modbus TCP liest (ohne Home Assistant).

* Der Modbus-Client kann ausschließlich **lesen** (Funktionscode 3) – eine Schreibfunktion
  gibt es nicht, der GX kann darüber nicht verändert werden.
* Zähler (Energiezähler, Batteriewächter, VE.Bus) werden jede Minute gelesen. Die Differenz
  zum letzten Stand wird verbucht – auch über Ausfälle des Containers hinweg, weil die Zähler
  im Gerät weiterlaufen. Rücksetzungen und 16-Bit-Überläufe werden erkannt.
* Leistungen (Verbrauch L1–L3, Netz L1–L3, Batterie, PV) werden alle 10 s gelesen und
  integriert. Der Netzbezug wird dabei je Abtastung über die Phasen saldiert (wie ein
  saldierender Zähler) – nicht über Stundenmittel wie in HA.
* Ergebnis: 15-Minuten-Blöcke (kWh + abgedeckte Sekunden) in der Datenbank. In der
  Abrechnung sind sie als virtuelle Entitäten ``victron:<schlüssel>`` wählbar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .db import SessionLocal, VictronBucket, VictronState, get_settings, save_settings
from .ha import period_bounds

log = logging.getLogger("victron")

PREFIX = "victron:"
BUCKET_MINUTES = 15
POWER_INTERVAL = 10  # s
COUNTER_INTERVAL = 60  # s
MAX_POWER_GAP = 30  # s – größere Lücken werden nicht integriert (Abdeckung sinkt)


class ModbusError(Exception):
    pass


# --------------------------------------------------------------------------- Modbus (nur lesen)
class ModbusReader:
    """Minimaler Modbus-TCP-Client, ausschließlich „Read Holding Registers“ (FC 3)."""

    def __init__(self, host: str, port: int = 502, timeout: float = 3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._tid = 0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._reader = self._writer = None

    async def _connect(self) -> None:
        if self._writer is None:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)

    async def read(self, unit: int, address: int, count: int) -> list[int]:
        async with self._lock:
            try:
                await self._connect()
                self._tid = (self._tid + 1) & 0xFFFF
                pdu = struct.pack(">BHH", 3, address, count)
                self._writer.write(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, unit) + pdu)
                await self._writer.drain()
                while True:
                    head = await asyncio.wait_for(self._reader.readexactly(7), self.timeout)
                    tid, _proto, length, _unit = struct.unpack(">HHHB", head)
                    body = await asyncio.wait_for(self._reader.readexactly(length - 1), self.timeout)
                    if tid == self._tid:
                        break
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
                await self.close()
                raise ModbusError(f"Verbindung zu {self.host}:{self.port}: {e or type(e).__name__}") from e
        if body[0] & 0x80:
            raise ModbusError(f"Unit {unit}, Register {address}: Modbus-Ausnahme {body[1]}")
        n = body[1]
        return list(struct.unpack(f">{n // 2}H", body[2:2 + n]))


def u16(regs, i=0):
    return regs[i]


def s16(regs, i=0):
    v = regs[i]
    return v - 0x10000 if v & 0x8000 else v


def u32(regs, i=0):
    return (regs[i] << 16) | regs[i + 1]


# --------------------------------------------------------------------------- Datenpunkte
@dataclass(frozen=True)
class Key:
    key: str
    label: str
    kind: str  # counter | power


KEYS = [
    Key("grid_import", "Energiezähler: Netzbezug (Zähler)", "counter"),
    Key("grid_export", "Energiezähler: Einspeisung (Zähler)", "counter"),
    Key("grid_import_saldo", "Netzbezug saldiert aus Netzleistung L1–L3 (10 s)", "power"),
    Key("grid_export_saldo", "Einspeisung saldiert aus Netzleistung L1–L3 (10 s)", "power"),
    Key("grid_import_phases", "Netzbezug je Phase einzeln (nicht saldiert, 10 s)", "power"),
    Key("grid_export_phases", "Einspeisung je Phase einzeln (nicht saldiert, 10 s)", "power"),
    Key("consumption", "Verbrauch AC-Lasten L1–L3 (10 s)", "power"),
    Key("battery_charged", "Batterie geladen (Batteriewächter-Zähler)", "counter"),
    Key("battery_discharged", "Batterie entladen (Batteriewächter-Zähler)", "counter"),
    Key("battery_charge_power", "Batterie geladen (aus Batterieleistung, 10 s)", "power"),
    Key("battery_discharge_power", "Batterie entladen (aus Batterieleistung, 10 s)", "power"),
    Key("vebus_acin1toinverter", "VE.Bus: Netz → Batterie (Zähler)", "counter"),
    Key("vebus_acin1toacout", "VE.Bus: Netz → AC-out (Zähler)", "counter"),
    Key("vebus_invertertoacout", "VE.Bus: Wechselrichter → AC-out (Zähler)", "counter"),
    Key("vebus_invertertoacin1", "VE.Bus: Wechselrichter → Netz (Zähler)", "counter"),
    Key("pv_dc", "PV-Erzeugung DC/MPPT (10 s)", "power"),
]
KEY_BY_NAME = {k.key: k for k in KEYS}
# bekannte Überläufe: Batteriewächter-Historie ist 16 Bit mit 0,1 kWh
OVERFLOW = {"battery_charged": 6553.5, "battery_discharged": 6553.5}

SYSTEM_UNIT = 100


async def discover(reader: ModbusReader, units=range(0, 248)) -> dict:
    """Sucht VE.Bus, Batteriewächter und Energiezähler; System ist immer Unit 100."""
    found: dict = {"system": None, "vebus": None, "battery": None, "grid": None}
    try:
        await reader.read(SYSTEM_UNIT, 817, 6)
        found["system"] = SYSTEM_UNIT
    except ModbusError as e:
        if "Verbindung" in str(e):
            raise
    probes = [("vebus", 74, 20), ("battery", 301, 2), ("grid", 2634, 4)]
    for unit in units:
        if unit == SYSTEM_UNIT:
            continue
        for name, addr, count in probes:
            if found[name] is not None:
                continue
            try:
                await reader.read(unit, addr, count)
                found[name] = unit
            except ModbusError as e:
                if "Verbindung" in str(e):
                    raise
        if all(v is not None for v in found.values()):
            break
    return found


async def read_power(reader: ModbusReader, units: dict) -> dict[str, float]:
    """Leistungen in W."""
    out: dict[str, float] = {}
    if units.get("system") is not None:
        r = await reader.read(units["system"], 817, 6)  # 817–819 Verbrauch, 820–822 Netz
        out["consumption"] = u16(r, 0) + u16(r, 1) + u16(r, 2)
        phases = [s16(r, 3), s16(r, 4), s16(r, 5)]
        grid = sum(phases)  # je Abtastung über die Phasen saldiert (wie ein saldierender Zähler)
        out["grid_import_saldo"] = max(0, grid)
        out["grid_export_saldo"] = max(0, -grid)
        out["grid_import_phases"] = sum(max(0, p) for p in phases)  # Vergleich: ohne Saldierung
        out["grid_export_phases"] = sum(max(0, -p) for p in phases)
        b = s16(await reader.read(units["system"], 842, 1))  # + = Laden
        out["battery_charge_power"] = max(0, b)
        out["battery_discharge_power"] = max(0, -b)
        out["pv_dc"] = u16(await reader.read(units["system"], 850, 1))
    return out


async def read_counters(reader: ModbusReader, units: dict) -> dict[str, float]:
    """Zählerstände in kWh."""
    out: dict[str, float] = {}
    if units.get("grid") is not None:
        r = await reader.read(units["grid"], 2634, 4)
        out["grid_import"] = u32(r, 0) / 100
        out["grid_export"] = u32(r, 2) / 100
    if units.get("battery") is not None:
        r = await reader.read(units["battery"], 301, 2)
        out["battery_discharged"] = u16(r, 0) / 10
        out["battery_charged"] = u16(r, 1) / 10
    if units.get("vebus") is not None:
        r = await reader.read(units["vebus"], 74, 20)  # 74 … 93
        out["vebus_acin1toacout"] = u32(r, 0) / 100
        out["vebus_acin1toinverter"] = u32(r, 2) / 100
        out["vebus_invertertoacin1"] = u32(r, 12) / 100
        out["vebus_invertertoacout"] = u32(r, 16) / 100
    return out


def counter_delta(key: str, prev: Optional[float], new: float) -> float:
    if prev is None:
        return 0.0
    if new >= prev:
        return new - prev
    top = OVERFLOW.get(key)
    if top and prev > top * 0.8 and new < top * 0.2:
        return top - prev + new  # Überlauf
    return new  # Rücksetzung: Zähler beginnt bei 0 (wie Home Assistant)


def bucket_start(ts: datetime) -> datetime:
    ts = ts.astimezone(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
    return ts - timedelta(minutes=ts.minute % BUCKET_MINUTES)


# --------------------------------------------------------------------------- Logger
class Logger:
    def __init__(self):
        self.reader: Optional[ModbusReader] = None
        self.cfg: tuple = ()
        self.units: dict = {}
        self.last_power: dict[str, tuple[float, float]] = {}  # key -> (mono, watt)
        self.last_counter_read = 0.0
        self.buckets: dict[tuple[datetime, str], list[float]] = {}  # (start, key) -> [kwh, seconds]
        self.status: dict = {"running": False, "last_ok": None, "last_error": "", "live": {}}

    def _add(self, now: datetime, key: str, kwh: float, seconds: float) -> None:
        b = self.buckets.setdefault((bucket_start(now), key), [0.0, 0.0])
        b[0] += kwh
        b[1] += seconds

    def integrate(self, now: datetime, mono: float, power: dict[str, float]) -> None:
        for key, watt in power.items():
            prev = self.last_power.get(key)
            if prev is not None:
                dt = mono - prev[0]
                if 0 < dt <= MAX_POWER_GAP:
                    self._add(now, key, (prev[1] + watt) / 2 * dt / 3_600_000, dt)  # Trapez
            self.last_power[key] = (mono, watt)

    def book_counters(self, s: Session, now: datetime, counters: dict[str, float]) -> None:
        for key, raw in counters.items():
            st = s.get(VictronState, key)
            if st is None:
                s.add(VictronState(key=key, raw=raw, ts=now.replace(tzinfo=None)))
                continue
            delta = counter_delta(key, st.raw, raw)
            seconds = max(0.0, (now.replace(tzinfo=None) - st.ts).total_seconds())
            self._add(now, key, delta, seconds)
            st.raw, st.ts = raw, now.replace(tzinfo=None)

    def flush(self, s: Session) -> None:
        for (start, key), (kwh, seconds) in self.buckets.items():
            row = s.query(VictronBucket).filter(VictronBucket.start == start, VictronBucket.key == key).first()
            if row is None:
                s.add(VictronBucket(start=start, key=key, kwh=kwh, seconds=seconds))
            else:
                row.kwh += kwh
                row.seconds += seconds
        self.buckets.clear()
        s.commit()

    async def step(self, now: Optional[datetime] = None, mono: Optional[float] = None) -> None:
        """Ein Durchlauf: Leistungen lesen, ggf. Zähler lesen, Blöcke speichern."""
        now = now or datetime.now(timezone.utc)
        mono = _time.monotonic() if mono is None else mono
        with SessionLocal() as s:
            st = get_settings(s)
            cfg = (st["victron_host"], int(st["victron_port"] or 502))
            if not st["victron_enabled"] or not cfg[0]:
                self.status["running"] = False
                return
            if cfg != self.cfg or self.reader is None:
                if self.reader:
                    await self.reader.close()
                self.reader, self.cfg, self.last_power = ModbusReader(*cfg), cfg, {}
            self.units = json.loads(st["victron_units"] or "{}")
            if not self.units:
                self.units = await discover(self.reader)
                save_settings(s, {"victron_units": json.dumps(self.units)})
            power = await read_power(self.reader, self.units)
            self.integrate(now, mono, power)
            live = {k: v for k, v in power.items()}
            if mono - self.last_counter_read >= COUNTER_INTERVAL or not self.last_counter_read:
                counters = await read_counters(self.reader, self.units)
                self.book_counters(s, now, counters)
                self.last_counter_read = mono
                live.update(counters)
            self.flush(s)  # nach jedem Abruf speichern – bei Neustart geht nichts verloren
            self.status.update(running=True, last_ok=now.isoformat(timespec="seconds"), last_error="",
                               live={**self.status.get("live", {}), **live})

    async def run_forever(self) -> None:
        while True:
            try:
                await self.step()
            except Exception as e:  # noqa: BLE001
                self.status.update(last_error=str(e))
                log.warning("Victron-Logger: %s", e)
                if self.reader:
                    await self.reader.close()
                await asyncio.sleep(20)
            await asyncio.sleep(POWER_INTERVAL)


logger = Logger()


# --------------------------------------------------------------------------- Abfrage
def is_victron(entity: str) -> bool:
    return entity.startswith(PREFIX)


def consumption(s: Session, entity_ids: list[str], start: Optional[date], end: Optional[date], tz: str,
                bounds: Optional[tuple[datetime, datetime]] = None,
                ) -> tuple[dict[str, Optional[float]], dict[str, dict]]:
    """Verbrauch je virtueller Entität im Zeitraum + Abdeckung (Anteil der Zeit mit Daten)."""
    t0, t1 = bounds or period_bounds(start, end, tz)
    u0 = t0.astimezone(timezone.utc).replace(tzinfo=None)
    u1 = t1.astimezone(timezone.utc).replace(tzinfo=None)
    total_s = (u1 - u0).total_seconds()
    values, meta = {}, {}
    for e in entity_ids:
        key = e[len(PREFIX):]
        rows = s.query(VictronBucket).filter(VictronBucket.key == key, VictronBucket.start >= u0,
                                             VictronBucket.start < u1).all()
        if not rows:
            values[e], meta[e] = None, {}
            continue
        secs = sum(r.seconds for r in rows)
        first = min(r.start for r in rows)
        values[e] = sum(r.kwh for r in rows)
        zone = t0.tzinfo
        daily: dict[str, float] = {}
        for r in rows:
            d = r.start.replace(tzinfo=timezone.utc).astimezone(zone).date().isoformat()
            daily[d] = daily.get(d, 0.0) + r.kwh
        meta[e] = {"method": "victron", "coverage": min(1.0, secs / total_s) if total_s else 1.0,
                   "since": first.isoformat(timespec="minutes"),
                   "daily": {d: round(v, 4) for d, v in sorted(daily.items())}}
    return values, meta


def virtual_entities() -> list[dict]:
    live = logger.status.get("live", {})
    return [{"entity_id": PREFIX + k.key, "name": f"Victron direkt – {k.label}", "unit": "kWh",
             "state": round(live[k.key], 2) if k.key in live else "", "device_class": "energy",
             "statistics": True} for k in KEYS]


# Welche virtuelle Entität zum jeweiligen Haus-Feld passt (für den Vergleich in der Abrechnung)
COMPARE = [
    ("entity_grid", "Netzbezug", ["grid_import", "grid_import_saldo", "grid_import_phases"]),
    ("entity_total", "Gesamtverbrauch", ["consumption"]),
    ("entity_battery", "Batterie entladen", ["battery_discharged", "battery_discharge_power"]),
    ("entity_battery_charge", "Batterie geladen", ["battery_charged", "battery_charge_power"]),
    ("entity_battery_charge_grid", "Netz → Batterie", ["vebus_acin1toinverter"]),
]
