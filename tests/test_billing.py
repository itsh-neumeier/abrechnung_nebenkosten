import pytest

from app.billing import AllocationCfg, BillCfg, FixedCostCfg, PartyCfg, compute


def amounts(party):
    return {l["label"]: l["amount"] for l in party["lines"]}


@pytest.fixture
def setup():
    bill = BillCfg(grid_kwh=500, energy_cost_net=125.0, fixed_cost_net=20.0, vat_rate=0.19, battery_rate_ct=10)
    parties = [
        PartyCfg(1, "Eigentümer", is_owner=True),
        PartyCfg(2, "Mieter EG", meters=["sensor.eg"]),
        PartyCfg(3, "Mieter OG", meters=["sensor.og_a", "sensor.og_b"]),
    ]
    values = {
        "sensor.total": 1000.0,
        "sensor.batt": 250.0,  # 25 % Batterieanteil
        "sensor.grid": 500.0,
        "sensor.eg": 200.0,
        "sensor.og_a": 100.0,
        "sensor.og_b": 100.0,
    }
    return bill, parties, values


def run(bill, parties, values, fixed=(), allocs=()):
    return compute(bill, parties, list(fixed), list(allocs), values,
                   total_entity="sensor.total", battery_entity="sensor.batt", grid_entity="sensor.grid")


def test_prices_and_battery_split(setup):
    r = run(*setup)
    assert r["price_net"] == pytest.approx(0.25)
    assert r["price_gross"] == pytest.approx(0.2975)
    assert r["battery_share"] == pytest.approx(0.25)
    eg = r["parties"][1]
    a = amounts(eg)
    # 200 kWh: 150 Netz * 0.2975, 50 Batterie * 0.25 netto + 50 * 0.10
    assert a["Netzstrom"] == pytest.approx(44.63)
    assert a["Batteriestrom – Energie netto"] == pytest.approx(12.50)
    assert a["Batteriestrom – Nutzungssatz"] == pytest.approx(5.00)
    assert r["warnings"] == []


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
    # Warmwasser-Strom wird vom Restverbrauch des Eigentümers abgezogen
    assert owner["kwh"] == pytest.approx(500)
    pot = 75 * 0.2975 + 25 * 0.25 + 25 * 0.10
    assert amounts(eg)["Warmwasser"] == pytest.approx(round(pot * 0.75, 2))
    assert amounts(og)["Warmwasser"] == pytest.approx(round(pot * 0.25, 2))
    assert "Warmwasser" not in amounts(owner)


def test_amount_allocation_percent(setup):
    alloc = AllocationCfg(2, "Wasser", "amount", amount=100.0, key_type="percent", key={1: 50, 2: 30, 3: 20})
    r = run(*setup, allocs=[alloc])
    assert [amounts(p)["Wasser"] for p in r["parties"]] == [50.0, 30.0, 20.0]


def test_reconciliation_without_battery_matches_bill():
    bill = BillCfg(grid_kwh=400, energy_cost_net=100.0, fixed_cost_net=30.0, vat_rate=0.19, battery_rate_ct=10)
    parties = [PartyCfg(1, "A", is_owner=True), PartyCfg(2, "B", meters=["m"])]
    values = {"t": 400.0, "b": 0.0, "m": 150.0}
    r = compute(bill, parties, [], [], values, total_entity="t", battery_entity="b")
    assert r["difference"] == pytest.approx(0.0, abs=0.02)


def test_warnings_negative_rest_and_grid_deviation(setup):
    bill, parties, values = setup
    values["sensor.eg"] = 900.0
    values["sensor.grid"] = 600.0
    r = run(bill, parties, values)
    assert r["parties"][0]["kwh"] == 0
    assert any("negativ" in w for w in r["warnings"])
    assert any("weicht" in w for w in r["warnings"])
