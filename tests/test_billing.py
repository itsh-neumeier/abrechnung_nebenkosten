import pytest

from app.billing import AllocationCfg, BillCfg, EnergyEntities, FixedCostCfg, PartyCfg, compute

ENT = EnergyEntities(total="sensor.total", grid="sensor.grid", battery_discharge="sensor.dis",
                     battery_charge_total="sensor.chg", battery_charge_grid="sensor.chg_grid")


def amounts(party):
    return {l["label"]: l["amount"] for l in party["lines"]}


@pytest.fixture
def setup():
    # Ø Arbeitspreis 25 ct netto / 29,75 ct brutto, Börse 10 ct, PV 5 ct, Verschleiß 8 ct
    bill = BillCfg(grid_kwh=500, energy_cost_net=125.0, fixed_cost_net=20.0, vat_rate=0.19,
                   wear_rate_ct=8, spot_price_ct=10, pv_rate_ct=5)
    parties = [
        PartyCfg(1, "Eigentümer", is_owner=True),
        PartyCfg(2, "Mieter EG", meters=["sensor.eg"]),
        PartyCfg(3, "Mieter OG", meters=["sensor.og_a", "sensor.og_b"]),
    ]
    values = {
        "sensor.total": 1000.0,
        "sensor.grid": 500.0,     # davon 100 kWh in die Batterie -> 400 kWh Netz direkt
        "sensor.chg": 200.0,      # Batterie geladen gesamt
        "sensor.chg_grid": 100.0,  # davon aus dem Netz -> 50 % Graustrom
        "sensor.dis": 250.0,      # Batterie entladen -> 125 Grau + 125 PV
        "sensor.eg": 200.0,       # PV direkt = 1000 - 400 - 250 = 350
        "sensor.og_a": 100.0,
        "sensor.og_b": 100.0,
    }
    return bill, parties, values


def run(bill, parties, values, fixed=(), allocs=(), ent=ENT):
    return compute(bill, parties, list(fixed), list(allocs), values, ent)


def test_energy_mix(setup):
    r = run(*setup)
    src = {s["key"]: s for s in r["sources"]}
    assert src["grid"]["kwh"] == pytest.approx(400)
    assert src["pv"]["kwh"] == pytest.approx(350)
    assert src["bat_pv"]["kwh"] == pytest.approx(125)
    assert src["bat_grid"]["kwh"] == pytest.approx(125)
    assert r["grey_frac"] == pytest.approx(0.5)
    assert r["warnings"] == []


def test_party_prices_per_source(setup):
    r = run(*setup)
    a = amounts(r["parties"][1])  # 200 kWh: 40 % / 35 % / 12,5 % / 12,5 %
    assert a["Netzstrom"] == pytest.approx(80 * 0.2975)            # brutto
    assert a["PV-Strom direkt"] == pytest.approx(70 * 0.15)        # Börse + PV
    assert a["Batteriestrom aus PV"] == pytest.approx(25 * 0.23)   # Börse + PV + Verschleiß
    assert a["Batteriestrom aus Netz (Graustrom)"] == pytest.approx(25 * 0.18)  # Börse + Verschleiß
    vat = {l["label"]: l["vat_included"] for l in r["parties"][1]["lines"]}
    assert vat["Netzstrom"] and not vat["PV-Strom direkt"]


def test_pv_direct_entity_and_normalisation(setup):
    bill, parties, values = setup
    values["sensor.pv"] = 700.0  # passt nicht zum Gesamtverbrauch -> Warnung + Normierung
    r = run(bill, parties, values, ent=EnergyEntities(**{**ENT.__dict__, "pv_direct": "sensor.pv"}))
    assert sum(s["share"] for s in r["sources"]) == pytest.approx(1.0)
    assert any("normiert" in w for w in r["warnings"])


def test_owner_gets_rest_and_fixed_costs_split_equally(setup):
    r = run(*setup)
    owner, eg, og = r["parties"]
    assert owner["kwh"] == pytest.approx(600)
    assert og["kwh"] == pytest.approx(200)
    fixed = [amounts(p)["Fixkosten Stromanbieter (Grundpreis/Messstelle) anteilig"] for p in r["parties"]]
    assert sum(fixed) == pytest.approx(23.80)
    assert max(fixed) - min(fixed) == pytest.approx(0.01)


def test_extra_fixed_cost_only_selected_parties(setup):
    r = run(*setup, fixed=[FixedCostCfg("IPTV", 9.99, party_ids=[2, 3])])
    owner, eg, og = r["parties"]
    assert "IPTV" not in amounts(owner)
    assert amounts(eg)["IPTV"] + amounts(og)["IPTV"] == pytest.approx(9.99)


def test_energy_allocation_by_water_meters(setup):
    bill, parties, values = setup
    values.update({"sensor.ww": 100.0, "sensor.w_eg": 3.0, "sensor.w_og": 1.0})
    alloc = AllocationCfg(1, "Warmwasser", "energy", source_entity="sensor.ww", key_type="entity",
                          key={2: "sensor.w_eg", 3: "sensor.w_og"}, key_unit="m³")
    r = run(bill, parties, values, allocs=[alloc])
    owner, eg, og = r["parties"]
    assert owner["kwh"] == pytest.approx(500)  # Warmwasser-Strom vom Rest abgezogen
    pot = 40 * 0.2975 + 35 * 0.15 + 12.5 * 0.23 + 12.5 * 0.18
    assert amounts(eg)["Warmwasser"] == pytest.approx(round(pot * 0.75, 2))
    assert amounts(og)["Warmwasser"] == pytest.approx(round(pot * 0.25, 2))
    assert "Warmwasser" not in amounts(owner)


def test_amount_allocation_percent(setup):
    alloc = AllocationCfg(2, "Wasser", "amount", amount=100.0, key_type="percent", key={1: 50, 2: 30, 3: 20})
    r = run(*setup, allocs=[alloc])
    assert [amounts(p)["Wasser"] for p in r["parties"]] == [50.0, 30.0, 20.0]


def test_grid_only_house_matches_bill():
    bill = BillCfg(grid_kwh=400, energy_cost_net=100.0, fixed_cost_net=30.0, vat_rate=0.19)
    parties = [PartyCfg(1, "A", is_owner=True), PartyCfg(2, "B", meters=["m"])]
    values = {"t": 400.0, "g": 400.0, "m": 150.0}
    r = compute(bill, parties, [], [], values, EnergyEntities(total="t", grid="g"))
    assert r["difference"] == pytest.approx(0.0, abs=0.02)


def test_warnings_negative_rest_and_grid_deviation(setup):
    bill, parties, values = setup
    values["sensor.eg"] = 900.0
    values["sensor.grid"] = 600.0
    r = run(bill, parties, values)
    assert r["parties"][0]["kwh"] == 0
    assert any("negativ" in w for w in r["warnings"])
    assert any("weicht" in w for w in r["warnings"])


def test_drinking_water_and_hot_water_by_percent(setup):
    """Trinkwasser: m³ aus HA x Preis, Warmwasserbereitung: Shelly-kWh zum Mix – beide nach Prozent."""
    bill, parties, values = setup
    values.update({"sensor.wasser": 12.0, "sensor.shelly_ww": 100.0})
    water = AllocationCfg(1, "Trinkwasser", "quantity", source_entity="sensor.wasser", amount=4.5,
                          source_unit="m³", key_type="percent", key={1: 20, 2: 50, 3: 30})
    hot = AllocationCfg(2, "Warmwasserbereitung", "energy", source_entity="sensor.shelly_ww",
                        key_type="percent", key={2: 60, 3: 40})
    r = run(bill, parties, values, allocs=[water, hot])
    owner, eg, og = r["parties"]
    assert [amounts(p)["Trinkwasser"] for p in r["parties"]] == [10.80, 27.00, 16.20]  # 54 € gesamt
    assert "12,00 m³ × 4,50 €/m³ = 54,00 €" in next(l["note"] for l in eg["lines"] if l["label"] == "Trinkwasser")
    pot = 40 * 0.2975 + 35 * 0.15 + 12.5 * 0.23 + 12.5 * 0.18
    assert amounts(eg)["Warmwasserbereitung"] == pytest.approx(round(pot * 0.6, 2))
    assert "Warmwasserbereitung" not in amounts(owner)
    assert owner["kwh"] == pytest.approx(500)  # Warmwasser-Strom nicht doppelt beim Eigentümer


def test_water_price_from_settings_parts(setup):
    bill, parties, values = setup
    values["sensor.wasser"] = 10.0
    water = AllocationCfg(1, "Trinkwasser", "quantity", source_entity="sensor.wasser", source_unit="m³",
                          price_parts=[("Wasser", 2.15), ("Abwasser", 2.60)], key_type="percent", key={2: 60, 3: 40})
    r = run(bill, parties, values, allocs=[water])
    eg = r["parties"][1]
    assert amounts(eg)["Trinkwasser"] == pytest.approx(28.50)  # 10 m³ x 4,75 € x 60 %
    note = next(l["note"] for l in eg["lines"] if l["label"] == "Trinkwasser")
    assert "10,00 m³ × (Wasser 2,15 € + Abwasser 2,60 €)/m³ = 47,50 €" in note
    water.price_parts = [("Wasser", 0.0), ("Abwasser", 0.0)]
    assert any("kein Preis" in w for w in run(bill, parties, values, allocs=[water])["warnings"])
