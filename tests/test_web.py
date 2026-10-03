import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ.pop("HA_URL", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_full_flow():
    with TestClient(app) as c:
        c.post("/settings", data={"entity_grid": "sensor.grid", "entity_total": "sensor.total",
                                  "entity_battery": "sensor.batt", "battery_rate_ct": "10", "vat_rate": "19",
                                  "landlord_name": "Max Vermieter", "landlord_iban": "DE00 1234"})
        c.post("/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1"})
        c.post("/parties/0", data={"name": "Mieter EG", "meters": "sensor.eg", "active": "1",
                                   "address": "Hauptstr. 1\n12345 Ort"})
        c.post("/costs/fixed/0", data={"name": "IPTV", "amount_gross": "9,99", "party_ids": ["2"], "active": "1"})
        c.post("/costs/alloc/0", data={"name": "Warmwasser", "source_type": "energy", "source_entity": "sensor.ww",
                                       "key_type": "entity", "key_unit": "m³", "key_2": "sensor.w_eg", "active": "1"})

        r = c.post("/billings", data={"period_start": "2026-09-01", "period_end": "2026-09-30",
                                      "grid_kwh": "500", "energy_cost_net": "125,00", "fixed_cost_net": "20",
                                      "vat_rate": "19", "battery_rate_ct": "10"}, follow_redirects=False)
        assert r.status_code == 303
        url = r.headers["location"].split("?")[0]

        page = c.get(url).text
        for e in ("sensor.grid", "sensor.total", "sensor.batt", "sensor.eg", "sensor.ww", "sensor.w_eg"):
            assert f"val__{e}" in page

        r = c.post(url, data={"action": "save", "title": "Strom 09/2026", "period_start": "2026-09-01",
                              "period_end": "2026-09-30", "grid_kwh": "500", "energy_cost_net": "125",
                              "fixed_cost_net": "20", "vat_rate": "19", "battery_rate_ct": "10",
                              "val__sensor.grid": "500", "val__sensor.total": "1000", "val__sensor.batt": "250",
                              "val__sensor.eg": "200", "val__sensor.ww": "100", "val__sensor.w_eg": "4"})
        page = r.text
        assert "29,75 ct/kWh" in page
        assert "25,0 %" in page

        inv = c.get(f"{url}/invoice/2").text
        assert "IPTV" in inv and "Warmwasser" in inv and "DE00 1234" in inv

        pdf = c.get(f"{url}/invoice/2.pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        z = c.get(f"{url}/all.zip")
        assert z.status_code == 200 and z.content[:2] == b"PK"

        c.post(url, data={"action": "finalize", "period_start": "2026-09-01", "period_end": "2026-09-30",
                          "energy_cost_net": "125"})
        assert "abgeschlossen" in c.get("/").text
