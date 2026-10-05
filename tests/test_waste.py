import json
from datetime import date, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from app import notify, waste, webpush
from app.db import SessionLocal, get_settings, save_settings
from app.main import app
from tests.test_push import decrypt, device

D = date.today() + timedelta(days=2)


def ics(days_kinds, extra=""):
    ev = "".join(f"BEGIN:VEVENT\r\nUID:{i}\r\nSUMMARY;LANGUAGE=de-de:{k}\r\nDTSTART;VALUE=DATE:{d:%Y%m%d}\r\nEND:VEVENT\r\n"
                 for i, (d, k) in enumerate(days_kinds))
    # wie beim Landkreis Bamberg: VTIMEZONE mit eigener RRULE außerhalb der Termine
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\nBEGIN:DAYLIGHT\r\n"
            "DTSTART:19700329T020000\r\nRRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n"
            + ev + extra + "END:VCALENDAR\r\n")


def test_parse_rrule_exdate_and_styles():
    text = ics([(D, "Restmülltonne"), (D, "Gelber Sack"), (D + timedelta(days=7), "Biotonne")],
               extra=("BEGIN:VEVENT\r\nSUMMARY:Papier\\, blau\r\nDTSTART;VALUE=DATE:" + f"{D:%Y%m%d}" +
                      "\r\nRRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4\r\nEXDATE;VALUE=DATE:" +
                      f"{D + timedelta(weeks=2):%Y%m%d}" + "\r\nEND:VEVENT\r\n"))
    ev = waste.parse_ics(text)
    assert waste.kinds(ev) == ["Biotonne", "Gelber Sack", "Papier, blau", "Restmülltonne"]
    assert [d for d, k in ev if k == "Papier, blau"] == [D, D + timedelta(weeks=4), D + timedelta(weeks=6)]
    assert waste.style("Restmülltonne")[1] == "⚫" and waste.style("Gelber Sack")[1] == "🟡"
    assert waste.style("Biotonne")[1] == "🟤" and waste.style("Windelsack")[1] == "🟠"
    up = waste.upcoming(ev, ["Restmülltonne", "Gelber Sack"])
    assert up[0] == (D, ["Gelber Sack", "Restmülltonne"]) and len(up) == 1


def test_due_times():
    ev = [(D, "Restmülltonne"), (D, "Sperrmüllanmeldung")]
    st = {"waste_types": json.dumps(["Restmülltonne"]), "waste_notify_evening": "1", "waste_evening_time": "19:30",
          "waste_notify_morning": "1", "waste_morning_time": "06:00"}
    eve = datetime.combine(D - timedelta(days=1), datetime.min.time())
    assert waste.due_notifications(ev, st, eve.replace(hour=19), set()) == []
    (key, title, _, kinds), = waste.due_notifications(ev, st, eve.replace(hour=19, minute=31), set())
    assert key.endswith("|evening") and title == "🗑️ Morgen Abholung: Restmülltonne" and kinds == ["Restmülltonne"]
    assert waste.due_notifications(ev, st, eve.replace(hour=20), {key}) == []
    day = datetime.combine(D, datetime.min.time())
    assert waste.due_notifications(ev, st, day.replace(hour=5, minute=59), set()) == []
    assert waste.due_notifications(ev, st, day.replace(hour=6, minute=1), set())[0][1].startswith("🗑️ Heute")
    assert waste.due_notifications(ev, st, day.replace(hour=12, minute=1), set()) == []  # mittags nicht mehr


def test_fetch_loads_current_and_next_year(monkeypatch):
    calls = []

    def fake_get(url, timeout=None, follow_redirects=None):
        calls.append(url)
        year = int(url.split("year=")[1][:4])
        if year == 2028:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(200, text=ics([(date(year, 1, 5), "Restmülltonne")]), request=httpx.Request("GET", url))

    monkeypatch.setattr(waste.httpx, "get", fake_get)
    text = waste.fetch("webcal://example.test/ics?year=2025&BIO=true", today=date(2026, 12, 1))
    assert calls == ["https://example.test/ics?year=2026&BIO=true", "https://example.test/ics?year=2027&BIO=true"]
    assert [d.year for d, _ in waste.parse_ics(text, horizon_days=5000)] == [2026, 2027]
    calls.clear()
    assert len(waste.parse_ics(waste.fetch("https://example.test/ics?year=2026", today=date(2027, 3, 1)),
                               horizon_days=5000)) == 1  # 2028 noch nicht da → nur 2027


def test_waste_page_portal_and_tick(monkeypatch):
    posted = []
    monkeypatch.setattr(webpush.httpx, "post",
                        lambda url, content=None, headers=None, timeout=None: posted.append((url, content)) or httpx.Response(201))
    dev = device()
    text = ics([(D, "Restmülltonne"), (D, "Windelsack"), (D + timedelta(days=3), "Sperrmüllanmeldung")])
    with TestClient(app) as c:
        c.post("/admin/parties/0", data={"name": "Familie Muster", "meters": "sensor.eg", "active": "1", "portal": "1"})
        c.post("/setup", data={"username": "admin", "password": "admin-pass-1", "password2": "admin-pass-1"})
        c.post("/admin/users/0", data={"username": "muster", "role": "tenant", "party_id": "1", "password": "mieter-pass",
                                 "active": "1"})
        r = c.post("/admin/waste", data={"waste_evening_time": "18:00", "waste_morning_time": "06:00",
                                   "waste_notify_evening": "1", "waste_notify_morning": "1"},
                   files={"ics_file": ("abfall.ics", text.encode(), "text/calendar")})
        assert "3 Abholtermine aus der Datei" in r.text and "Sperrmüllanmeldung" in r.text
        # nur Restmüll + Windelsack auswählen
        c.post("/admin/waste", data={"types_form": "1", "waste_types": ["Restmülltonne", "Windelsack"],
                               "waste_notify_evening": "1", "waste_notify_morning": "1",
                               "waste_evening_time": "18:00", "waste_morning_time": "06:00"})
        page = c.get("/admin/waste").text
        assert page.count('name="waste_types"') == 3 and "Sperrmüllanmeldung</span>" not in page.split("Nächste Abholungen")[1]
        c.get("/logout")
        c.post("/login", data={"username": "muster", "password": "mieter-pass"})
        c.post("/api/push/subscribe", json=dev[2])
        p = c.get("/").text
        assert "Nächste Abholungen" in p and "⚫ Restmülltonne" in p and "🟠 Windelsack" in p
        assert c.get("/admin/waste", follow_redirects=False).status_code == 303  # Mieter: keine Verwaltung

    with SessionLocal() as s:
        eve = datetime.combine(D - timedelta(days=1), datetime.min.time()).replace(hour=18, minute=10)
        assert notify.waste_tick(s, eve) == 1 and notify.waste_tick(s, eve + timedelta(minutes=5)) == 0
        assert "|evening" in get_settings(s)["waste_sent"]
    msg = decrypt(posted[-1][1], *dev[:2])
    assert msg["title"] == "🗑️ Morgen Abholung: Restmülltonne, Windelsack" and msg["url"] == "/#abfall"
    with SessionLocal() as s:
        assert notify.waste_tick(s, datetime.combine(D, datetime.min.time()).replace(hour=6, minute=5)) == 1
        assert notify.waste_tick(s, datetime.combine(D + timedelta(days=3), datetime.min.time()).replace(hour=7)) == 0
    assert decrypt(posted[-1][1], *dev[:2])["title"].startswith("🗑️ Heute Abholung")
