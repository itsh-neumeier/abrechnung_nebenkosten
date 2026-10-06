import base64
import io

from fastapi.testclient import TestClient

from app import auth
from app.db import Billing, Building, SessionLocal, User
from app.main import app

BILL = {"period_start": "2026-09-01", "period_end": "2026-09-30", "grid_kwh": "100", "energy_cost_net": "25,00",
        "fixed_cost_net": "10", "spot_price_ct": "10", "vat_rate": "19", "battery_rate_ct": "8", "pv_rate_ct": "5"}


def _jpeg() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (37, 99, 235)).save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def login(c, user, pw):
    return c.post("/login", data={"username": user, "password": pw, "remember": "1"}, follow_redirects=False)


def test_multisite_roles_scoping_and_photo():
    auth.login_throttle.hits.clear()
    with TestClient(app) as c:
        with SessionLocal() as s:  # Migration: Standardgebäude existiert
            assert s.query(Building).count() == 1
        c.post("/setup", data={"username": "chef", "password": "chef-pass-1", "password2": "chef-pass-1"})
        with SessionLocal() as s:
            assert s.query(User).one().role == "superadmin"
        # zweites Gebäude + Verwalter dafür
        r = c.post("/admin/buildings/0", data={"code": "GID-02", "name": "Gartenhaus", "address": "Weg 2, 1 Ort",
                                               "title": "Nebenkostenabrechnung", "active": "1"})
        assert "Gespeichert" in r.text
        c.post("/admin/users/0", data={"username": "verwalter", "role": "admin", "building_ids": ["2"],
                                       "password": "verw-pass-1", "active": "1"})
        # Parteien je Gebäude, Abrechnungen je Gebäude
        c.post("/admin/parties/0", data={"name": "Haus 1 Mieter", "meters": "sensor.a", "active": "1", "building_id": "1"})
        c.post("/admin/parties/0", data={"name": "Haus 2 Mieter", "meters": "sensor.b", "active": "1", "building_id": "2",
                                         "portal": "1"})
        c.post("/admin/billings", data={**BILL, "building_id": "1", "title": "B1"})
        c.post("/admin/billings", data={**BILL, "building_id": "2", "title": "B2"})
        with SessionLocal() as s:
            b1 = s.query(Billing).filter(Billing.title == "B1").one()
            b2 = s.query(Billing).filter(Billing.title == "B2").one()
            assert (b1.building_id, b2.building_id) == (1, 2)
            assert [p["name"] for p in b2.result["parties"]] == ["Haus 2 Mieter"]  # nur Parteien des Gebäudes
            assert b2.result["building"]["building_id"] == "GID-02"
        page = c.get("/admin").text
        assert "Gartenhaus" in page and "GID-02" in page  # Gebäude-Spalte bei mehreren Gebäuden
        # Foto mit Zuschnitt (Data-URL) hochladen
        r = c.post("/admin/buildings/2", data={"code": "GID-02", "name": "Gartenhaus", "address": "Weg 2, 1 Ort",
                                               "active": "1", "photo_data": _jpeg(), "managers": []})
        assert "Foto aktualisiert" in r.text
        assert c.post("/admin/buildings/2", data={"name": "Gartenhaus", "photo_data": "data:text/html;base64,PGI+"}
                      ).text.count("Foto ungültig") == 1
        assert c.get("/photo/building/2.jpg").content[:3] == b"\xff\xd8\xff"
        c.post("/admin/users/0", data={"username": "mieter2", "role": "tenant", "party_id": "2",
                                       "password": "mieter-pass", "active": "1"})
        c.post("/admin/users/0", data={"username": "mieter1", "role": "tenant", "party_id": "1",
                                       "password": "mieter-pass", "active": "1"})
        c.get("/logout")

        # Verwalter von Gebäude 2: sieht nur Gebäude 2, keine globalen Einstellungen
        login(c, "verwalter", "verw-pass-1")
        page = c.get("/admin").text
        assert "B2" in page and "B1" not in page
        assert c.get(f"/admin/billings/{b1.id}").status_code == 404
        assert c.get(f"/admin/billings/{b2.id}").status_code == 200
        assert "Haus 1 Mieter" not in c.get("/admin/parties").text and "Haus 2 Mieter" in c.get("/admin/parties").text
        r = c.get("/admin/settings", follow_redirects=False)
        assert r.status_code == 303 and "Super-Admins" in r.headers["location"]
        assert c.post("/admin/users/0", data={"username": "x"}).status_code == 403
        assert "Einstellungen" not in c.get("/admin").text.split("</nav>")[0]
        assert c.get("/admin/buildings/1").status_code == 404 and c.get("/admin/buildings/2").status_code == 200
        assert c.get("/admin/buildings/0").status_code == 403
        c.get("/logout")

        # Mieter Gebäude 2: rundes Foto in „Mein Zuhause“; Mieter Gebäude 1 darf das Foto nicht laden
        login(c, "mieter2", "mieter-pass")
        home = c.get("/").text
        assert 'class="card home-hero"' in home and "/photo/building/2.jpg" in home and "Gartenhaus" in home
        assert c.get("/photo/building/2.jpg").status_code == 200
        c.get("/logout")
        login(c, "mieter1", "mieter-pass")
        assert c.get("/photo/building/2.jpg").status_code == 404


def test_migration_promotes_existing_admins():
    from app import db

    with SessionLocal() as s:
        s.add(User(username="alt", role="admin", password_hash=auth.hash_password("x" * 10), active=True))
        s.query(User).filter(User.role == "superadmin").delete()
        s.commit()
    db._migrate_multisite()
    with SessionLocal() as s:
        assert s.query(User).filter(User.username == "alt").one().role == "superadmin"
