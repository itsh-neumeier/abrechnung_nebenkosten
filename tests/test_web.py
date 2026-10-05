import re
import smtplib

from fastapi.testclient import TestClient

from app import ha
from app.main import app

SENT = []


class FakeSMTP:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **kw):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg, to_addrs=None):
        SENT.append((msg, to_addrs))


BILL = {"period_start": "2026-09-01", "period_end": "2026-09-30", "grid_kwh": "500", "energy_cost_net": "125,00",
        "fixed_cost_net": "20", "spot_price_ct": "10", "vat_rate": "19", "battery_rate_ct": "8", "pv_rate_ct": "5"}
VALUES = {"val__sensor.grid": "500", "val__sensor.total": "1000", "val__sensor.dis": "250", "val__sensor.chg": "200",
          "val__sensor.chg_grid": "100", "val__sensor.eg": "200", "val__sensor.ww": "100", "val__sensor.w_eg": "4", "val__sensor.wasser": "10"}


def test_full_flow(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    async def fake_states(self):
        return [
            {"entity_id": "sensor.shelly_eg_energy", "state": "123.4",
             "attributes": {"friendly_name": "Shelly EG", "unit_of_measurement": "kWh", "state_class": "total_increasing"}},
            {"entity_id": "sensor.temp", "state": "21", "attributes": {"unit_of_measurement": "°C"}},
            {"entity_id": "switch.x", "state": "on", "attributes": {}},
        ]
    monkeypatch.setattr(ha.HAClient, "states", fake_states)

    async def no_ws(self, payloads):
        raise OSError("kein WebSocket im Test")
    monkeypatch.setattr(ha.HAClient, "_ws_calls", no_ws)

    with TestClient(app) as c:
        ents = c.get("/api/entities?refresh=1").json()
        assert [e["entity_id"] for e in ents] == ["sensor.shelly_eg_energy", "sensor.temp"]
        assert ents[0]["statistics"] and not ents[1]["statistics"]

        c.post("/admin/settings", data={
            "entity_grid": "sensor.grid", "entity_total": "sensor.total", "entity_battery": "sensor.dis",
            "entity_battery_charge": "sensor.chg", "entity_battery_charge_grid": "sensor.chg_grid",
            "battery_rate_ct": "8", "pv_rate_ct": "5", "vat_rate": "19",
            "building_title": "Nebenkostenabrechnung",
            "building_address": "Musterstraße 1, 12345 Musterstadt", "building_id": "GID-01",
            "landlord_name": "Max Vermieter", "landlord_iban": "DE00 1234",
            "mail_auto_send": "1", "water_price_m3": "2,15", "sewage_price_m3": "2,60", "mail_subject": "Abrechnung {zeitraum} – {wohneinheit}",
            "mail_body": "Hallo {name}, Betrag {betrag}", "mail_bcc": "ich@test.de"})
        assert "data-phases" in c.get("/admin/settings").text

        c.post("/admin/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1"})
        c.post("/admin/parties/0", data={"name": "Familie Muster", "unit_id": "WE-001", "meters": "sensor.eg",
                                   "active": "1", "email": "zweig@test.de"})
        c.post("/admin/costs/fixed/0", data={"name": "IPTV", "amount_gross": "9,99", "party_ids": ["2"], "active": "1"})
        c.post("/admin/costs/alloc/0", data={"name": "Warmwasser", "source_type": "energy", "source_entity": "sensor.ww",
                                       "key_type": "entity", "key_unit": "m³", "key_2": "sensor.w_eg", "active": "1"})

        c.post("/admin/costs/alloc/0", data={"name": "Trinkwasser", "source_type": "quantity", "source_entity": "sensor.wasser",
                                       "source_unit": "m³", "price_source": "water", "key_type": "percent",
                                       "key_1": "50", "key_2": "50", "active": "1"})
        r = c.post("/admin/billings", data=BILL, follow_redirects=False)
        assert r.status_code == 303
        url = r.headers["location"].split("?")[0]

        page = c.get(url).text
        for e in ("sensor.grid", "sensor.total", "sensor.dis", "sensor.chg", "sensor.chg_grid", "sensor.eg",
                  "sensor.ww", "sensor.w_eg"):
            assert f"val__{e}" in page

        page = c.post(url, data={"action": "save", **BILL, **VALUES}).text
        assert "29,75 ct/kWh" in page
        assert "Batteriestrom aus Netz (Graustrom)" in page

        inv = c.get(f"{url}/invoice/2").text
        assert "Nebenkostenabrechnung" in inv
        assert "(Musterstraße 1, 12345 Musterstadt)" in inv
        assert "Gebäude ID: GID-01 – Wohneinheiten ID: WE-001" in inv
        assert "PV-Strom direkt" in inv and "IPTV" in inv and "Warmwasser" in inv
        assert "(Wasser 2,15 € + Abwasser 2,60 €)/m³ = 47,50 €" in inv

        pdf = c.get(f"{url}/invoice/2.pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        assert c.get(f"{url}/all.zip").content[:2] == b"PK"

        # Abschließen -> automatischer Versand an Parteien mit E-Mail
        r = c.post(url, data={"action": "finalize", **BILL, **VALUES})
        assert "abgeschlossen" in r.text
        assert len(SENT) == 1
        msg, rcpt = SENT[0]
        assert rcpt == ["zweig@test.de", "ich@test.de"]
        assert msg["Subject"] == "Abrechnung 01.09.2026 – 30.09.2026 – Familie Muster"
        att = [p for p in msg.iter_attachments()]
        assert att[0].get_filename() == "Nebenkostenabrechnung_2026-09_WE-001.pdf"
        assert att[0].get_content()[:4] == b"%PDF"
        html = msg.get_body(("html",)).get_content()
        assert "Ihre Nebenkostenabrechnung 09/2026" in html and "Zahlbar bis" in html and "DE00 1234" in html
        assert "max-width:600px" in html and "@media only screen" in html  # responsive
        cid = re.search(r'src="cid:([^"]+)"', html).group(1)
        logo = [p for p in msg.walk() if p.get("Content-ID") == f"<{cid}>"]
        assert logo and logo[0].get_content_type() == "image/png"
        assert "Hallo Familie Muster" in msg.get_body(("plain",)).get_content()
        assert "versendet" in c.get(url).text

        # manuell erneut senden
        c.post(url, data={"action": "send:2"})
        assert len(SENT) == 2


def test_migration_adds_columns(tmp_path):
    import sqlalchemy as sa

    from app import db

    eng = sa.create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE TABLE parties (id INTEGER PRIMARY KEY, name VARCHAR(200))"))
        conn.execute(sa.text("INSERT INTO parties (name) VALUES ('Alt')"))
    old = db.engine
    db.engine = eng
    try:
        db.init_db()
    finally:
        db.engine = old
    cols = {c["name"] for c in sa.inspect(eng).get_columns("parties")}
    assert {"unit_id", "email", "meters", "is_owner"} <= cols
    with eng.connect() as conn:
        assert conn.execute(sa.text("SELECT unit_id, is_owner FROM parties")).one() == ("", 0)



def test_three_phase_meters():
    """Shelly 3EM o. Ä.: Zähler je Phase L1/L2/L3 werden addiert, Haus-Entitäten ebenso."""
    with TestClient(app) as c:
        c.post("/admin/settings", data={"entity_total": "sensor.tot_l1+sensor.tot_l2 , sensor.tot_l3", "vat_rate": "19"})
        c.post("/admin/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1"})
        c.post("/admin/parties/0", data={"name": "3EM", "active": "1",
                                   "meters": ["sensor.p_l1 + sensor.p_l2 + sensor.p_l3", "sensor.extra", ""]})
        page = c.get("/admin/parties/2").text
        assert 'value="sensor.p_l1 + sensor.p_l2 + sensor.p_l3"' in page and 'value="sensor.extra"' in page
        c.post("/admin/costs/alloc/0", data={"name": "WW", "source_type": "energy", "source_entity": "sensor.ww_l1 sensor.ww_l2 sensor.ww_l3",
                                       "key_type": "entity", "keyent_2": "sensor.k1+sensor.k2+sensor.k3", "active": "1"})
        r = c.post("/admin/billings", data={"period_start": "2026-09-01", "period_end": "2026-09-30", "grid_kwh": "100",
                                      "energy_cost_net": "25", "vat_rate": "19"}, follow_redirects=False)
        url = r.headers["location"].split("?")[0]
        page = c.get(url).text
        assert "Gesamtverbrauch Haus – L3" in page
        assert "Zähler 3EM #1 – L2" in page and "Zähler 3EM #2" in page
        vals = {"val__sensor.tot_l1": "100", "val__sensor.tot_l2": "100", "val__sensor.tot_l3": "100",
                "val__sensor.p_l1": "10", "val__sensor.p_l2": "20", "val__sensor.p_l3": "30", "val__sensor.extra": "5",
                "val__sensor.ww_l1": "1", "val__sensor.ww_l2": "1", "val__sensor.ww_l3": "1",
                "val__sensor.k1": "1", "val__sensor.k2": "1", "val__sensor.k3": "1"}
        c.post(url, data={"action": "save", "period_start": "2026-09-01", "period_end": "2026-09-30",
                          "grid_kwh": "100", "energy_cost_net": "25", "vat_rate": "19", **vals})
        from app.db import Billing, SessionLocal
        with SessionLocal() as s:
            r = s.get(Billing, int(url.rsplit("/", 1)[1])).result
        kwh = {p["name"]: p["kwh"] for p in r["parties"]}
        assert kwh["3EM"] == 65 and kwh["Eigentümer"] == 300 - 65 - 3


def test_power_sensor_coverage_warning(monkeypatch):
    import asyncio

    from app import service
    from app.db import Billing, Party, SessionLocal

    async def detail(self, ids, start, end, tz):
        return ({i: 50.0 for i in ids},
                {i: {"method": "power", "hours": 600, "expected_hours": 720, "coverage": 600 / 720} for i in ids})

    monkeypatch.setattr(ha.HAClient, "consumption_detail", detail)
    from datetime import date
    with TestClient(app):
        with SessionLocal() as s:
            s.add(Party(name="P", meters=["sensor.p_power"], active=True, is_owner=False, sort=0))
            b = Billing(period_start=date(2026, 9, 1), period_end=date(2026, 9, 30), grid_kwh=100,
                        energy_cost_net=25, values={}, amounts={}, result={}, sent={})
            s.add(b)
            s.commit()
            asyncio.run(service.fetch_values(s, b))
            r = service.recompute(s, b)
            assert b.values["sensor.p_power"] == 50.0
            assert any("600 von 720 Stunden" in w for w in r["warnings"])


def test_fetch_values_reasons_and_no_overwrite(monkeypatch):
    """VRM- und HA-Werte gemeinsam: VRM darf nicht überschrieben werden; fehlende Werte mit Grund."""
    import asyncio
    from datetime import date

    from app import service, vrm
    from app.db import Billing, Party, SessionLocal, save_settings

    async def ha_detail(self, ids, start, end, tz, bounds=None):
        return ({i: (5.0 if i == "sensor.da" else None) for i in ids}, {i: {"method": "counter"} for i in ids})

    async def ha_entities(self):
        return [{"entity_id": "sensor.da", "state": "1"},
                {"entity_id": "sensor.kg_bad_dryer_energie", "state": "unavailable"}]

    async def vrm_cons(site, ids, start, end, tz, bounds=None):
        return {i: 42.0 for i in ids}, {i: {"method": "vrm"} for i in ids}

    monkeypatch.setattr(ha.HAClient, "consumption_detail", ha_detail)
    monkeypatch.setattr(ha.HAClient, "entities", ha_entities)
    monkeypatch.setattr(vrm, "consumption", vrm_cons)
    monkeypatch.setattr(vrm, "configured", lambda: True)
    with TestClient(app):
        with SessionLocal() as s:
            save_settings(s, {"entity_total": "vrm:consumption", "vrm_site_id": "1"})
            s.add(Party(name="P", meters=["sensor.da", "sensor.dryer_energie", "sensor.leer"], active=True,
                        is_owner=False, sort=0))
            b = Billing(period_start=date(2026, 8, 1), period_end=date(2026, 8, 31), grid_kwh=10,
                        energy_cost_net=3, values={}, amounts={}, result={}, sent={})
            s.add(b)
            s.commit()
            missing = asyncio.run(service.fetch_values(s, b))
            assert b.values["vrm:consumption"] == 42.0 and b.values["sensor.da"] == 5.0
            assert "umbenannt" in missing["sensor.dryer_energie"]
            assert "sensor.kg_bad_dryer_energie" in missing["sensor.dryer_energie"]
            assert "umbenannt" in missing["sensor.leer"]
            r = service.recompute(s, b)
            assert any("umbenannt" in w for w in r["warnings"])


def test_legacy_titles_renamed():
    from datetime import date

    from app import db

    with db.SessionLocal() as s:
        s.add(db.Billing(title="Strom 08/2026", period_start=date(2026, 8, 1), period_end=date(2026, 8, 31),
                         energy_cost_net=1, values={}, amounts={}, result={}, sent={}))
        s.add(db.Billing(title="Mein Titel", period_start=date(2026, 9, 1), period_end=date(2026, 9, 30),
                         energy_cost_net=1, values={}, amounts={}, result={}, sent={}))
        s.add(db.Setting(key="mail_subject", value="Nebenkostenabrechnung Strom {zeitraum} – {wohneinheit}"))
        s.commit()
    db.init_db()
    with db.SessionLocal() as s:
        assert sorted(b.title for b in s.query(db.Billing).all()) == ["Mein Titel", "Nebenkosten 08/2026"]
        assert db.get_settings(s)["mail_subject"] == "Nebenkostenabrechnung {zeitraum} – {wohneinheit}"
