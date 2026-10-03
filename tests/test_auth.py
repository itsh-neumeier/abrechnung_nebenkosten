import re
import smtplib

from fastapi.testclient import TestClient

from app import auth
from app.main import app

BILL = {"period_start": "2026-09-01", "period_end": "2026-09-30", "grid_kwh": "100", "energy_cost_net": "25,00",
        "fixed_cost_net": "10", "spot_price_ct": "10", "vat_rate": "19", "battery_rate_ct": "8", "pv_rate_ct": "5"}
VALUES = {"val__sensor.grid": "100", "val__sensor.total": "300", "val__sensor.eg": "120", "val__sensor.og": "60"}
MAILS = []


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
        MAILS.append(msg)


def login(c, user, pw, **extra):
    return c.post("/login", data={"username": user, "password": pw, **extra}, follow_redirects=False)


def test_password_hashing():
    h = auth.hash_password("geheim123")
    assert h.startswith("pbkdf2_sha256$") and auth.verify_password("geheim123", h)
    assert not auth.verify_password("falsch", h) and not auth.verify_password("x", "kaputt")
    assert auth.password_problem("kurz") and not auth.password_problem("lang-genug")


def test_login_roles_portal_and_reset(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    MAILS.clear()
    auth.login_throttle.hits.clear()
    auth.reset_throttle.hits.clear()

    with TestClient(app) as c:
        # ohne Benutzer: offen, mit Hinweis
        assert "Kein Login eingerichtet" in c.get("/").text
        assert c.get("/favicon.ico").status_code == 200

        # Grunddaten + Abrechnung (noch offen)
        c.post("/settings", data={"entity_grid": "sensor.grid", "entity_total": "sensor.total", "vat_rate": "19"})
        c.post("/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1"})
        c.post("/parties/0", data={"name": "Familie Muster", "unit_id": "WE-001", "meters": "sensor.eg", "active": "1",
                                   "portal": "1", "email": "muster@test.de"})
        c.post("/parties/0", data={"name": "Familie Andere", "unit_id": "WE-002", "meters": "sensor.og", "active": "1"})
        r = c.post("/billings", data=BILL, follow_redirects=False)
        url = r.headers["location"].split("?")[0]
        bid = int(url.rsplit("/", 1)[1])
        c.post(url, data={"action": "save", **BILL, **VALUES})

        # Ersteinrichtung → Login aktiv, Verwalter angemeldet
        assert c.post("/setup", data={"username": "admin", "password": "kurz", "password2": "kurz"}).url.path == "/setup"
        r = c.post("/setup", data={"username": "admin", "email": "admin@test.de", "password": "admin-pass-1",
                                   "password2": "admin-pass-1"})
        assert "Login ist jetzt aktiv" in r.text and "Kein Login eingerichtet" not in r.text
        assert c.get("/setup", follow_redirects=False).headers["location"] == "/login"

        # Abschließen → automatisch veröffentlicht
        assert "im Mieterportal veröffentlicht" in c.post(url, data={"action": "finalize", **BILL, **VALUES}).text

        # Mieter-Zugang für Familie Muster (Partei 2)
        r = c.post("/users/0", data={"username": "muster", "role": "tenant", "party_id": "2", "name": "Muster",
                                     "email": "muster@test.de", "password": "mieter-pass", "active": "1"})
        assert "Benutzer gespeichert" in r.text
        assert "Mieter müssen einer Partei" in c.post("/users/0", data={"username": "x", "role": "tenant",
                                                                         "password": "mieter-pass", "active": "1"}).text

        # Abmelden → alles gesperrt
        c.get("/logout")
        assert c.get("/settings", follow_redirects=False).headers["location"].startswith("/login?next=/settings")
        assert c.get("/", follow_redirects=False).headers["location"] == "/login"
        assert c.get("/api/entities").status_code == 401
        assert c.get(f"{url}/invoice/2.pdf", follow_redirects=False).status_code == 303
        assert c.get("/static/favicon.svg").status_code == 200

        # falsches Passwort, dann Mieter-Login
        assert "falsch" in c.post("/login", data={"username": "muster", "password": "nein"}).text
        r = login(c, "muster", "mieter-pass")
        assert r.headers["location"] == "/portal"
        page = c.get("/portal").text
        assert "Meine Abrechnungen" in page and f"/portal/{bid}.pdf" in page and "Einstellungen" not in page
        assert c.get(f"/portal/{bid}.pdf").content[:4] == b"%PDF"
        assert "Familie Muster" in c.get(f"/portal/{bid}").text
        # Mieter darf nichts anderes – auch nicht über ?party= eine fremde Partei
        assert c.get("/settings", follow_redirects=False).headers["location"] == "/portal"
        assert c.post("/settings", data={"vat_rate": "7"}).status_code == 403
        assert c.get(f"{url}/invoice/3", follow_redirects=False).status_code == 303
        assert "Familie Andere" not in c.get("/portal?party=3").text
        assert c.get(f"/portal/{bid}?party=3").status_code == 200  # bleibt bei der eigenen Partei
        assert "Familie Muster" in c.get(f"/portal/{bid}?party=3").text
        c.get("/logout")

        # Verwalter: zurückziehen → Mieter sieht nichts mehr; Vorschau „Ansicht als Mieter“
        login(c, "admin", "admin-pass-1")
        c.post(url, data={"action": "unpublish"})
        assert "Noch keine veröffentlichten" in c.get("/portal?party=2").text
        c.post(url, data={"action": "publish"})
        assert f"/portal/{bid}.pdf?party=2" in c.get("/portal?party=2").text
        assert "nicht freigegeben" in c.get("/portal?party=3").text
        # letzter Verwalter bleibt geschützt
        assert "letzte aktive Verwalter" in c.post("/users/1", data={"username": "admin", "role": "tenant",
                                                                       "party_id": "2", "active": "1"}).text
        assert "nicht selbst löschen" in c.post("/users/1", data={"delete": "1"}).text
        c.get("/logout")

        # Passwort vergessen → Mail mit Link → neues Passwort → alte Sitzung ungültig
        login(c, "muster", "mieter-pass")
        old_cookie = c.cookies.get(auth.COOKIE)
        c.get("/logout")
        r = c.post("/password/forgot", data={"login": "muster@test.de"})
        assert "Link zum Zurücksetzen" in r.text
        assert c.post("/password/forgot", data={"login": "gibtsnicht"}).text.count("Link zum Zurücksetzen") == 1
        assert len(MAILS) == 1 and MAILS[0]["To"] == "muster@test.de"
        link = re.search(r"http\S+/password/reset\?token=(\S+)", MAILS[0].get_content()).group(0)
        token = link.split("token=")[1]
        assert "Neues Passwort" in c.get(f"/password/reset?token={token}").text
        r = c.post("/password/reset", data={"token": token, "password": "neues-pass-1", "password2": "neues-pass-1"})
        assert "Passwort gespeichert" in r.text
        assert "ungültig" in c.get(f"/password/reset?token={token}").text  # nur einmal nutzbar
        assert login(c, "muster", "mieter-pass").headers["location"].startswith("/login")
        assert login(c, "muster", "neues-pass-1").headers["location"] == "/portal"
        c.cookies.set(auth.COOKIE, old_cookie)
        assert c.get("/portal", follow_redirects=False).headers["location"].startswith("/login")

        # Einladung: Benutzer ohne Passwort bekommt Link
        c.cookies.clear()
        login(c, "admin", "admin-pass-1")
        r = c.post("/users/0", data={"username": "neu", "role": "tenant", "party_id": "2", "email": "neu@test.de",
                                     "active": "1"})
        assert "Einladung an neu@test.de verschickt" in r.text
        assert "Benutzername: neu" in MAILS[-1].get_content()


def test_login_throttle():
    auth.login_throttle.hits.clear()
    with TestClient(app) as c:
        c.post("/setup", data={"username": "admin", "password": "admin-pass-1", "password2": "admin-pass-1"})
        c.get("/logout")
        for _ in range(5):
            login(c, "admin", "falsch")
        r = c.post("/login", data={"username": "admin", "password": "admin-pass-1"})
        assert "Zu viele Fehlversuche" in r.text
    auth.login_throttle.hits.clear()


def test_bootstrap_admin_from_env(monkeypatch):
    import dataclasses

    from app.config import config
    from app.db import SessionLocal, User

    monkeypatch.setattr(auth, "config", dataclasses.replace(config, app_user="chef", app_password="start-pass-1"))
    with SessionLocal() as s:
        assert auth.bootstrap(s) == "chef"
        u = s.query(User).one()
        assert u.role == "admin" and auth.verify_password("start-pass-1", u.password_hash)
        assert auth.bootstrap(s) is None  # nur beim ersten Mal
