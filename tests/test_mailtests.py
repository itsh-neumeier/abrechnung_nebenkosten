import imaplib
import smtplib

from fastapi.testclient import TestClient

from app import mailbox
from app.main import app
from tests.test_import import make_eml, make_pdf

SENT = []


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
        SENT.append((msg, to_addrs))


class BadSMTP(FakeSMTP):
    def send_message(self, msg, to_addrs=None):
        raise smtplib.SMTPSenderRefused(553, b"sender not owned", msg["From"])


def test_testmail_and_imap_check(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    raw = make_eml(make_pdf())

    class FakeIMAP:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def select(self, folder, readonly=False):
            assert readonly

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                return "OK", [b"1"]
            return "OK", [(b"1 (BODY[] {1}", raw), b")"]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(mailbox, "configured", lambda: True)
    SENT.clear()
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert "Test-E-Mail senden" in page and "Postfach testen" in page
        r = c.post("/settings/testmail", data={"test_to": "ich@test.de"})
        assert "Test-E-Mail an ich@test.de verschickt" in r.text
        assert SENT[-1][1] == ["ich@test.de"] and "Absender:" in SENT[-1][0].get_body(("plain",)).get_content()
        assert "Empfängeradresse" in c.post("/settings/testmail", data={"test_to": ""}).text

        monkeypatch.setattr(smtplib, "SMTP", BadSMTP)
        assert "SMTP_FROM muss die Adresse des Postfachs" in c.post("/settings/testmail", data={"test_to": "a@b.de"}).text

        ok = c.post("/settings/testimap", data={"imap_senders": "awattar.de", "imap_forwarded": "1"}).text
        assert "Postfach OK" in ok and "1 als Rechnung erkannt" in ok and "Absender awattar.de" in ok
        none = c.post("/settings/testimap", data={"imap_senders": "andere.de"}).text
        assert "0 als Rechnung erkannt" in none and "Absender-Einstellung prüfen" in none
