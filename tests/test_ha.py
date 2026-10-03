from datetime import date

from app.ha import period_bounds, sum_changes


def test_sum_changes():
    assert sum_changes([{"change": 1.5}, {"change": 2.0}, {"change": None}]) == 3.5
    assert sum_changes([{"sum": 10.0}, {"sum": 14.0}]) == 4.0
    assert sum_changes([]) is None


def test_period_bounds_inclusive_end_local_time():
    t0, t1 = period_bounds(date(2026, 3, 1), date(2026, 3, 31), "Europe/Berlin")
    assert t0.isoformat() == "2026-03-01T00:00:00+01:00"
    assert t1.isoformat() == "2026-04-01T00:00:00+02:00"


def test_entities_ws_skips_rest_500(monkeypatch):
    """/api/states liefert 500 -> Auswahl kommt aus WebSocket get_states + Recorder-Statistiken."""
    import asyncio

    from app.ha import HAClient, HAError

    async def ws_calls(self, payloads):
        assert [p["type"] for p in payloads] == ["get_states", "recorder/list_statistic_ids"]
        return [
            [{"entity_id": "sensor.shelly_energy", "state": "12.5",
              "attributes": {"friendly_name": "Shelly", "unit_of_measurement": "kWh",
                             "state_class": "total_increasing"}},
             {"entity_id": "light.x", "state": "on", "attributes": {}}],
            [{"statistic_id": "sensor.shelly_energy", "name": "Shelly"},
             {"statistic_id": "sensor.wasser_m3", "name": "Wasserzähler", "display_unit_of_measurement": "m³"}],
        ]

    async def rest_states(self):
        raise HAError("500 Internal Server Error")

    monkeypatch.setattr(HAClient, "_ws_calls", ws_calls)
    monkeypatch.setattr(HAClient, "states", rest_states)
    ents = asyncio.run(HAClient("http://ha", "t").entities())
    assert [e["entity_id"] for e in ents] == ["sensor.shelly_energy", "sensor.wasser_m3"]
    assert ents[1]["unit"] == "m³" and ents[1]["statistics"]


def test_entities_falls_back_to_rest(monkeypatch):
    import asyncio

    from app.ha import HAClient

    async def ws_fail(self, payloads):
        raise OSError("WebSocket blockiert")

    async def rest_states(self):
        return [{"entity_id": "sensor.a", "state": "1", "attributes": {"unit_of_measurement": "kWh"}}]

    monkeypatch.setattr(HAClient, "_ws_calls", ws_fail)
    monkeypatch.setattr(HAClient, "states", rest_states)
    assert [e["entity_id"] for e in asyncio.run(HAClient("http://ha", "t").entities())] == ["sensor.a"]


def test_power_sensor_integrates_hourly_means():
    from app.ha import evaluate_rows

    # 3 Stunden: 0,5 kW, 1,0 kW, -0,2 kW (Rückspeisung -> 0) => 1,5 kWh; 3 von 4 Stunden mit Daten
    rows = [{"mean": 0.5}, {"mean": 1.0}, {"mean": -0.2}]
    kwh, meta = evaluate_rows(rows, expected_hours=4)
    assert kwh == 1.5
    assert meta == {"method": "power", "hours": 3, "expected_hours": 4, "coverage": 0.75}


def test_counter_rows_preferred_over_mean():
    from app.ha import evaluate_rows

    kwh, meta = evaluate_rows([{"change": 2.0, "mean": None}, {"change": 1.0}], 2)
    assert kwh == 3.0 and meta["method"] == "counter"


def test_consumption_detail_mixed(monkeypatch):
    """Ein Zähler und ein Leistungssensor in einer Abfrage, Einheit kW angefordert, Monat mit Zeitumstellung."""
    import asyncio
    from datetime import date

    from app.ha import HAClient

    seen = {}

    async def ws_call(self, payload):
        seen.update(payload)
        return {"sensor.zaehler": [{"change": 10.0}, {"change": 5.0}],
                "sensor.leistung": [{"mean": 0.25}] * 744}

    monkeypatch.setattr(HAClient, "_ws_call", ws_call)
    vals, meta = asyncio.run(HAClient("http://ha", "t").consumption_detail(
        ["sensor.zaehler", "sensor.leistung"], date(2026, 10, 1), date(2026, 10, 31), "Europe/Berlin"))
    assert seen["units"]["power"] == "kW" and "mean" in seen["types"]
    assert vals == {"sensor.leistung": 186.0, "sensor.zaehler": 15.0}
    assert meta["sensor.leistung"]["expected_hours"] == 745  # Oktober mit Zeitumstellung
    assert meta["sensor.zaehler"] == {"method": "counter"}
