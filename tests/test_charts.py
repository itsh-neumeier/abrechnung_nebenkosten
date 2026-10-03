from app import charts

DAYS = [f"2026-08-{d:02d}" for d in range(1, 32)]


def test_bar_chart_svg():
    svg = charts.bar_chart(DAYS, [1.0] * 30 + [None])
    assert svg.startswith("<svg") and svg.count("<rect") == 31
    assert "Ø 1,00 kWh/Tag" in svg and "1,25" not in svg  # Achse bis 1 -> keine krummen Werte


def test_stacked_chart_svg():
    mix = {"grid": [1.0] * 31, "pv": [2.0] * 31, "bat_pv": [0.5] * 31, "bat_grid": [0.0] * 31}
    svg = charts.stacked_chart(DAYS, mix)
    assert svg.count('fill="#f59e0b"') == 32  # 31 Balken + Legende
    assert "Batterie aus Netz" in svg
