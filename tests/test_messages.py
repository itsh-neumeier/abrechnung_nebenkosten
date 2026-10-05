from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from app import notify, webpush
from app.db import Message, SessionLocal
from app.main import app
from tests.test_push import decrypt, device


def test_broadcast_unicast_notices_and_reminder(monkeypatch):
    posted = []
    monkeypatch.setattr(webpush.httpx, "post",
                        lambda url, content=None, headers=None, timeout=None: posted.append((url, content, headers)) or httpx.Response(201))
    d2, d3 = device(), device()  # Gerät Familie Muster (Partei 2) und Familie Andere (Partei 3)

    with TestClient(app) as c:
        c.post("/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1"})
        c.post("/parties/0", data={"name": "Familie Muster", "meters": "sensor.eg", "active": "1", "portal": "1"})
        c.post("/parties/0", data={"name": "Familie Andere", "meters": "sensor.og", "active": "1", "portal": "1"})
        c.post("/setup", data={"username": "admin", "password": "admin-pass-1", "password2": "admin-pass-1"})
        for name, pid in (("muster", 2), ("andere", 3)):
            c.post("/users/0", data={"username": name, "role": "tenant", "party_id": str(pid),
                                     "password": "mieter-pass", "active": "1"})
        c.get("/logout")
        for name, dev in (("muster", d2), ("andere", d3)):
            c.post("/login", data={"username": name, "password": "mieter-pass"})
            c.post("/api/push/subscribe", json=dev[2])
            c.get("/logout")

        c.post("/login", data={"username": "admin", "password": "admin-pass-1"})
        assert "Neue Mitteilung" in c.get("/messages").text
        # Broadcast: geplante Wasserabschaltung morgen 8–12 Uhr, mit Erinnerung
        start = (datetime.now() + timedelta(hours=20)).replace(second=0, microsecond=0)
        r = c.post("/messages", data={"category": "abschaltung", "priority": "urgent", "party_ids": "all",
                                      "title": "Wasser wird abgestellt",
                                      "body": "Arbeiten am Hausanschluss.", "event_start": start.strftime("%Y-%m-%dT%H:%M"),
                                      "event_end": (start + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M"),
                                      "remind": "1", "notify_now": "1"})
        assert "Push an 2 von 2 Gerät(en)" in r.text
        msg = decrypt(posted[-1][1], *d3[:2])
        assert msg["title"] == "‼️ ⛔ Wasser wird abgestellt" and "Arbeiten am Hausanschluss" in msg["body"]
        assert msg["priority"] == "urgent" and posted[-1][2]["Urgency"] == "high"
        assert msg["url"].startswith("/portal#m")
        # Unicast: nur Familie Muster, dauerhaft angeheftet
        posted.clear()
        r = c.post("/messages", data={"category": "termin", "priority": "low", "party_ids": "2",
                                      "title": "Ablesung Wasserzähler",
                                      "body": "Bitte Zugang zum Keller ermöglichen.", "pinned": "1", "notify_now": "1"})
        assert "Push an 1 von 1 Gerät(en)" in r.text and len(posted) == 1 and posted[0][0] == d2[2]["endpoint"]
        assert posted[0][2]["Urgency"] == "low" and decrypt(posted[0][1], *d2[:2])["priority"] == "low"
        # Info ohne Datum/Anheften → nur Verlauf
        c.post("/messages", data={"category": "info", "party_ids": "all", "title": "Neue Mülltonnen"})
        page = c.get("/messages").text
        assert "Aktuell im Mieterportal" in page and page.count('class="notice prio-') == 2
        c.get("/logout")

        # Portal Familie Muster: beide Hinweise farbig oben, Info im Verlauf
        c.post("/login", data={"username": "muster", "password": "mieter-pass"})
        p = c.get("/portal").text
        assert p.count('class="notice prio-') == 2 and "Ablesung Wasserzähler" in p and "--nc:#dc2626" in p
        assert p.index("Wasser wird abgestellt") < p.index("Ablesung Wasserzähler")  # dringend zuerst
        assert "prio-badge prio-urgent" in p
        assert "Mitteilungen" in p and "Neue Mülltonnen" in p
        c.get("/logout")
        # Portal Familie Andere: Unicast an Muster ist nicht sichtbar
        c.post("/login", data={"username": "andere", "password": "mieter-pass"})
        p = c.get("/portal").text
        assert "Ablesung Wasserzähler" not in p and "Wasser wird abgestellt" in p
        assert c.post("/messages", data={"title": "x"}).status_code == 403

    # Erinnerung am Vortag: genau einmal, an alle Geräte
    posted.clear()
    with SessionLocal() as s:
        assert notify.send_reminders(s) == 1
        assert notify.send_reminders(s) == 0
        m = s.query(Message).filter(Message.title == "Wasser wird abgestellt").one()
        assert m.reminded_at is not None
        # nach dem Ereignis nicht mehr „aktuell“
        assert notify.is_current(m) and not notify.is_current(m, now=m.event_end + timedelta(minutes=1))
    assert len(posted) == 2 and decrypt(posted[0][1], *d2[:2])["title"].startswith("Erinnerung: ‼️ ⛔")
