"""VRM-Anbindung mit simulierter API (keine echten Zugangsdaten)."""

import asyncio
import types
from datetime import date

import pytest

from app import vrm


def fake_get(responses):
    async def _get(self, path, params=None):
        for prefix, data in responses.items():
            if path.startswith(prefix):
                return data
        raise AssertionError(path)
    return _get


def test_totals_and_derived(monkeypatch):
    monkeypatch.setattr(vrm, "config", types.SimpleNamespace(vrm_token="x"))
    monkeypatch.setattr(vrm.VRMClient, "_get", fake_get({"/installations/1/stats": {
        "success": True, "totals": {"Gc": 24.4, "Gb": 6.5, "Pc": 754.0, "Pb": 717.9, "Pg": 20.6, "Bc": 536.7,
                                    "Bg": 13.0, "kwh": 1}}}))
    ids = ["vrm:grid_import", "vrm:consumption", "vrm:Gb", "vrm:battery_discharged", "vrm:unbekannt"]
    vals, meta = asyncio.run(vrm.consumption("1", ids, date(2026, 9, 1), date(2026, 9, 30), "Europe/Berlin"))
    assert vals["vrm:grid_import"] == pytest.approx(30.9)
    assert vals["vrm:consumption"] == pytest.approx(1315.1)
    assert vals["vrm:Gb"] == 6.5 and vals["vrm:battery_discharged"] == pytest.approx(549.7)
    assert vals["vrm:unbekannt"] is None and meta["vrm:Gb"] == {"method": "vrm"}


def test_installations(monkeypatch):
    monkeypatch.setattr(vrm.VRMClient, "_get", fake_get({
        "/users/me": {"success": True, "user": {"id": 7}},
        "/users/7/installations": {"success": True, "records": [{"idSite": 840379, "name": "Haus"}]}}))
    assert asyncio.run(vrm.VRMClient("t").installations()) == [{"id": 840379, "name": "Haus"}]


def test_missing_site_raises():
    with pytest.raises(vrm.VRMError):
        asyncio.run(vrm.consumption("", ["vrm:Gc"], date(2026, 9, 1), date(2026, 9, 30), "Europe/Berlin"))
