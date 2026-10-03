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

        c.post("/settings", data={
            "entity_grid": "sensor.grid", "entity_total": "sensor.total", "entity_battery": "sensor.dis",
            "entity_battery_charge": "sensor.chg", "entity_battery_charge_grid": "sensor.chg_grid",
            "battery_rate_ct": "8", "pv_rate_ct": "5", "vat_rate": "19",
            "building_title": "Nebenkostenabrechnung",
            "building_address": "Musterstraße 1, 12345 Musterstadt", "building_id": "GID-01",
            "landlord_name": "Max Vermieter", "landlord_iban": "DE00 1234",
            "mail_auto_send": "1", "water_price_m3": "2,15", "sewage_price_m3": "2,60", "mail_subject": "Abrechnung {zeitraum} – {wohneinheit}",
            "mail_body": "Hallo {name}, Betrag {betrag}", "mail_bcc": "ich@test.de"})
        assert "data-entity" in c.get("/settings").text

        c.post("/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1"})
        c.post("/parties/0", data={"name": "Familie Muster", "unit_id": "WE-001", "meters": "sensor.eg",
                                   "active": "1", "email": "zweig@test.de"})
        c.post("/costs/fixed/0", data={"name": "IPTV", "amount_gross": "9,99", "party_ids": ["2"], "active": "1"})
        c.post("/costs/alloc/0", data={"name": "Warmwasser", "source_type": "energy", "source_entity": "sensor.ww",
                                       "key_type": "entity", "key_unit": "m³", "key_2": "sensor.w_eg", "active": "1"})

        c.post("/costs/alloc/0", data={"name": "Trinkwasser", "source_type": "quantity", "source_entity": "sensor.wasser",
                                       "source_unit": "m³", "price_source": "water", "key_type": "percent",
                                       "key_1": "50", "key_2": "50", "active": "1"})
        r = c.post("/billings", data=BILL, follow_redirects=False)
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
        assert att[0].get_filename() == "Nebenkostenabrechnung_Strom_2026-09_WE-001.pdf"
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
