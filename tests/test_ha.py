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
