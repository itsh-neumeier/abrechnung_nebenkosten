"""Victron-Logger gegen einen nachgebauten GX (Modbus-TCP-Server mit Victron-Registern)."""

import asyncio
import json
import struct
from datetime import date, datetime, timedelta, timezone

import pytest

from app import victron
from app.db import SessionLocal, VictronBucket, save_settings


class FakeGX:
    """Antwortet auf FC 3 wie ein GX; unbekannte Unit/Register -> Modbus-Ausnahme 0x0B; FC != 3 -> 0x01."""

    def __init__(self):
        self.regs = {}  # (unit, addr) -> wert
        self.requests = []
        self.server = None

    def set_u32(self, unit, addr, value):
        self.regs[(unit, addr)] = (value >> 16) & 0xFFFF
        self.regs[(unit, addr + 1)] = value & 0xFFFF

    def set(self, unit, addr, value):
        self.regs[(unit, addr)] = value & 0xFFFF

    async def handle(self, reader, writer):
        try:
            while True:
                head = await reader.readexactly(7)
                tid, proto, length, unit = struct.unpack(">HHHB", head)
                pdu = await reader.readexactly(length - 1)
                fc = pdu[0]
                self.requests.append((unit, fc))
                if fc != 3:
                    resp = bytes([fc | 0x80, 1])
                else:
                    addr, count = struct.unpack(">HH", pdu[1:5])
                    vals = [self.regs.get((unit, a)) for a in range(addr, addr + count)]
                    if any(v is None for v in vals):
                        resp = bytes([0x83, 0x0B])
                    else:
                        resp = bytes([3, 2 * count]) + struct.pack(f">{count}H", *vals)
                writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


def gx_with_devices():
    gx = FakeGX()
    for a, v in zip(range(817, 823), [800, 300, 400, 0xFFFF - 999, 600, 450]):  # Netz L1 = -1000 W
        gx.set(100, a, v)
    gx.set(100, 842, 0xFFFF - 499)  # Batterie -500 W (entlädt)
    gx.set(100, 850, 2000)
    for a in range(74, 94):
        gx.set(229, a, 0)
    gx.set_u32(229, 76, 1000)    # acin1toinverter 10,00 kWh
    gx.set_u32(229, 74, 2000)    # acin1toacout 20,00 kWh
    gx.set_u32(229, 90, 3000)    # invertertoacout 30,00 kWh
    gx.set(225, 301, 65000)      # entladen 6500,0 kWh (kurz vor 16-Bit-Überlauf)
    gx.set(225, 302, 1234)       # geladen 123,4 kWh
    gx.set_u32(31, 2634, 7479)   # Energiezähler Bezug 74,79 kWh
    gx.set_u32(31, 2636, 100)
    return gx


def test_counter_delta_reset_and_overflow():
    assert victron.counter_delta("vebus_acin1toacout", 100.0, 105.5) == pytest.approx(5.5)
    assert victron.counter_delta("vebus_acin1toacout", 641.7, 10.4) == pytest.approx(10.4)  # Rücksetzung
    assert victron.counter_delta("battery_charged", 6550.0, 2.0) == pytest.approx(5.5)       # 16-Bit-Überlauf
    assert victron.counter_delta("x", None, 5.0) == 0.0


def test_discover_and_read_only():
    async def run():
        gx = gx_with_devices()
        port = await gx.start()
        r = victron.ModbusReader("127.0.0.1", port)
        units = await victron.discover(r)
        power = await victron.read_power(r, units)
        counters = await victron.read_counters(r, units)
        await r.close()
        gx.server.close()
        return gx, units, power, counters
    gx, units, power, counters = asyncio.run(run())
    assert units == {"system": 100, "vebus": 229, "battery": 225, "grid": 31}
    assert power["consumption"] == 1500
    assert power["grid_import_saldo"] == 50 and power["grid_export_saldo"] == 0  # -1000 + 600 + 450 = 50 W
    assert power["grid_import_phases"] == 1050 and power["grid_export_phases"] == 1000  # je Phase einzeln
    assert power["battery_discharge_power"] == 500 and power["battery_charge_power"] == 0
    assert counters == {"grid_import": 74.79, "grid_export": 1.0, "battery_discharged": 6500.0,
                        "battery_charged": 123.4, "vebus_acin1toacout": 20.0, "vebus_acin1toinverter": 10.0,
                        "vebus_invertertoacin1": 0.0, "vebus_invertertoacout": 30.0}
    assert {fc for _, fc in gx.requests} == {3}  # ausschließlich lesende Zugriffe


def test_logger_buckets_and_billing_query():
    async def run():
        gx = gx_with_devices()
        port = await gx.start()
        with SessionLocal() as s:
            save_settings(s, {"victron_enabled": "1", "victron_host": "127.0.0.1", "victron_port": str(port)})
        lg = victron.Logger()
        t = datetime(2026, 9, 30, 21, 50, tzinfo=timezone.utc)  # 23:50 Ortszeit
        mono = 1000.0
        for i in range(13):  # 2 Minuten, alle 10 s
            if i == 7:  # Zählerstände steigen
                gx.set_u32(31, 2634, 7479 + 50)          # +0,5 kWh Bezug
                gx.set_u32(229, 74, 2000 + 0)            # unverändert
                gx.set(225, 301, 25)                     # Überlauf: 6500,0 -> 2,5 => 6553,5 - 6500 + 2,5 = 56,0 kWh
            await lg.step(now=t + timedelta(seconds=10 * i), mono=mono + 10 * i)
            lg.last_counter_read = lg.last_counter_read if i != 6 else 0  # Zähler bei i=7 erneut lesen
        await lg.reader.close()
        gx.server.close()
    asyncio.run(run())
    with SessionLocal() as s:
        keys = {r.key for r in s.query(VictronBucket).all()}
        assert {"consumption", "grid_import", "battery_discharged"} <= keys
        vals, meta = victron.consumption(s, ["victron:consumption", "victron:grid_import",
                                             "victron:battery_discharged"],
                                         date(2026, 9, 1), date(2026, 9, 30), "Europe/Berlin")
    # 1500 W über 10 s-Schritte bis 23:59:59 Ortszeit (Block 21:45–22:00 UTC); 12 Intervalle à 10 s = 120 s
    assert vals["victron:consumption"] == pytest.approx(1500 * 120 / 3_600_000)
    assert vals["victron:grid_import"] == pytest.approx(0.5)
    assert vals["victron:battery_discharged"] == pytest.approx(56.0)
    assert meta["victron:consumption"]["method"] == "victron"
    assert meta["victron:consumption"]["coverage"] < 0.01


def test_fetch_values_uses_logger_for_victron_entities():
    from app import service
    from app.db import Billing, Party

    with SessionLocal() as s:
        save_settings(s, {"victron_enabled": "1", "entity_total": "victron:consumption",
                          "entity_grid": "victron:grid_import"})
        s.add(Party(name="Eigentümer", meters=[], active=True, is_owner=True, sort=0))
        for i in range(4 * 24 * 30):  # September komplett in 15-Minuten-Blöcken
            start = datetime(2026, 8, 31, 22, 0) + timedelta(minutes=15 * i)
            s.add(VictronBucket(start=start, key="consumption", kwh=0.5, seconds=900))
            s.add(VictronBucket(start=start, key="grid_import", kwh=0.01, seconds=900))
        b = Billing(period_start=date(2026, 9, 1), period_end=date(2026, 9, 30), energy_cost_net=10,
                    values={}, amounts={}, result={}, sent={})
        s.add(b)
        s.commit()
        missing = asyncio.run(service.fetch_values(s, b))
        assert missing == {}
        assert b.values["victron:consumption"] == pytest.approx(1440.0)
        assert b.grid_kwh == pytest.approx(28.8)  # Netzbezug ohne Rechnungswert aus dem Logger übernommen
        assert b.values_meta["victron:consumption"]["coverage"] == pytest.approx(1.0)
        rows = service.victron_comparison(s, b)
        assert any(r["label"] == "Gesamtverbrauch" and r["deviation"] == pytest.approx(0) for r in rows)


def test_compare_page(monkeypatch):
    from fastapi.testclient import TestClient

    from app import ha
    from app.main import app

    async def detail(self, ids, start, end, tz, bounds=None):
        assert bounds is not None
        return {i: 10.0 for i in ids}, {i: {"method": "counter"} for i in ids}

    monkeypatch.setattr(ha.HAClient, "consumption_detail", detail)
    with SessionLocal() as s:
        save_settings(s, {"victron_enabled": "1", "entity_grid": "sensor.easymeter_bezug"})
        now = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
        for i in range(1, 9):
            s.add(VictronBucket(start=now - timedelta(minutes=15 * i), key="grid_import", kwh=1.25, seconds=900))
        s.commit()
    with TestClient(app) as c:
        page = c.get("/victron/compare?hours=2").text
    assert "sensor.easymeter_bezug" in page
    assert "+0,0 %" in page or "+0.0 %" in page  # 8 × 1,25 = 10 kWh = HA-Wert
