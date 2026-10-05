"""Import-Tests mit einer anonymisierten Nachbildung der aWATTar-Rechnung."""

import imaplib
from email.message import EmailMessage

import re

import pytest

from app import invoice_import as ii

AWATTAR_TEXT = """aWATTar Deutschland GmbH
Rechnung Strom
Rechnungsnummer: 2026000001 Rechnungsdatum: 07.09.2026
Kundennummer: 1000000000000 Anlagenadresse: Musterstraße 1
Marktlokation: 50000000000 Bezug 74,79 kWh
wir verrechnen den Stromverbrauch für den Zeitraum 01.08.2026 - 31.08.2026.
Beschreibung Netto Brutto
Stromkosten 30,93 € 36,81 €
Summe 30,93 € 36,81 €
MWSt. 19% auf 30,93 € 5,88 €
Zu zahlender Betrag 36,81 €
Arbeitspreis: 29,900 Cent/kWh
Grundpreis1: 14,36 Euro/Monat
Seite 1/3
Detailaufstellung
Energie
Beschreibung Zeitraum Preis Menge Netto
Grundpreis Energie 01.08.2026 – 31.08.2026 3,85 Euro/Monat 31,00 Tage 3,93 €
Stromverbrauch (HOURLY) 01.08.2026 – 31.08.2026 14,09 Cent/kWh 74,79 kWh 10,54 €
Summe 14,46 €
Netznutzung
Beschreibung Zeitraum Preis Menge Netto
Grundentgelt 01.08.2026 – 31.08.2026 98,55 Euro/Jahr 1,00 Monate 8,21 €
Netznutzung 01.08.2026 – 31.08.2026 4,72 Cent/kWh 74,79 kWh 3,53 €
Summe 11,74 €
Umlagen, Abgaben und Steuern
Beschreibung Zeitraum Preis Menge Netto
Konzessionsabgabe 01.08.2026 – 31.08.2026 1,32 Cent/kWh 74,79 kWh 0,99 €
Umlage Abschaltbare Lasten 01.08.2026 – 31.08.2026 0,00 Cent/kWh 74,79 kWh 0,00 €
KWK-Umlage 01.08.2026 – 31.08.2026 0,45 Cent/kWh 74,79 kWh 0,33 €
Offshore-Haftungsumlage 01.08.2026 – 31.08.2026 0,94 Cent/kWh 74,79 kWh 0,70 €
Stromsteuer 01.08.2026 – 31.08.2026 2,05 Cent/kWh 74,79 kWh 1,53 €
Aufschlag für besondere Netznutzung
(ehemals §19 StromNEV-Umlage)
01.08.2026 – 31.08.2026 1,56 Cent/kWh 74,79 kWh 1,17 €
Summe 4,72 €
"""


def make_pdf(text: str = AWATTAR_TEXT) -> bytes:
    from weasyprint import HTML

    html = "".join(f"<p style='margin:0;font-size:7pt'>{line}</p>" for line in text.splitlines())
    return HTML(string=html).write_pdf()


def make_eml(pdf: bytes, mid: str = "<test-1@awattar.de>") -> bytes:
    m = EmailMessage()
    m["From"] = "aWATTar Service <service@awattar.de>"
    m["To"] = "ich@test.de"
    m["Subject"] = "aWATTar - Rechnung Strom 08/2026 - 2026000001"
    m["Message-ID"] = mid
    m.set_content("Im Anhang finden Sie Ihre Stromrechnung.")
    m.add_attachment(pdf, maintype="application", subtype="pdf", filename="aWATTar-Rechnung-2026000001.pdf")
    return m.as_bytes()


def test_parse_awattar_text():
    inv = ii.parse_text(AWATTAR_TEXT)
    assert inv.supplier == "aWATTar"
    assert inv.invoice_no == "2026000001"
    assert (str(inv.period_start), str(inv.period_end)) == ("2026-08-01", "2026-08-31")
    assert inv.grid_kwh == 74.79
    assert inv.energy_cost_net == pytest.approx(18.79)
    assert inv.fixed_cost_net == pytest.approx(12.14)
    assert inv.spot_price_ct == 14.09
    assert inv.vat_rate == pytest.approx(0.19)
    assert inv.total_gross == 36.81
    assert len(inv.positions) == 10
    assert inv.positions[-1].description == "Aufschlag für besondere Netznutzung (ehemals §19 StromNEV-Umlage)"
    assert inv.warnings == []


def test_parse_detects_mismatch():
    inv = ii.parse_text(AWATTAR_TEXT.replace("Summe 30,93 € 36,81 €", "Summe 40,00 € 47,60 €"))
    assert any("weicht" in w for w in inv.warnings)


@pytest.mark.parametrize("change, expect", [
    (lambda t: t.replace("Summe", "Gesamt"), "Rechnungssumme"),
    (lambda t: re.sub(r"Bezug(\s+[\d.,]+\s*kWh)", r"Verbrauch\1", t), "Netzbezug"),
    (lambda t: t.replace("HOURLY", "dynamisch"), "HOURLY"),
    (lambda t: re.sub("MWSt", "Steuer", t, flags=re.I), "MwSt.-Satz"),
])
def test_layout_changes_warn_instead_of_silent(change, expect):
    """Leicht geändertes Rechnungsdesign: Werte bleiben richtig, aber es gibt einen Hinweis (→ Prüfung)."""
    inv = ii.parse_text(change(AWATTAR_TEXT))
    assert inv.spot_price_ct == 14.09 and inv.grid_kwh == 74.79
    assert any(expect in w for w in inv.warnings)


def test_label_variants_are_accepted():
    text = AWATTAR_TEXT.replace(" €", " EUR").replace("MWSt.", "USt.")
    inv = ii.parse_text(text)
    assert len(inv.positions) == 10 and inv.total_net == 30.93 and inv.vat_rate == pytest.approx(0.19)
    assert inv.warnings == []


def test_unknown_format():
    with pytest.raises(ii.ImportError_):
        ii.parse_text("Irgendein Brief ohne Positionen")


def test_pdf_and_eml_roundtrip():
    pdf = make_pdf()
    assert ii.parse_pdf(pdf).energy_cost_net == pytest.approx(18.79)
    files = ii.load_upload("rechnung.eml", make_eml(pdf))
    assert files[0][0] == "aWATTar-Rechnung-2026000001.pdf"
    assert files[0][2] == "<test-1@awattar.de>"
    assert ii.parse_pdf(files[0][1]).invoice_no == "2026000001"


def test_upload_and_imap(tmp_path, monkeypatch):
    """Upload über das Webinterface + automatischer Abruf aus einem (simulierten) Postfach."""
    import asyncio

    from fastapi.testclient import TestClient

    from app import mailbox
    from app.config import config
    from app.db import Billing, SessionLocal
    from app.main import app

    pdf = make_pdf()
    with TestClient(app) as c:
        c.post("/admin/settings", data={"import_mode": "review"})
        r = c.post("/admin/billings/import", files={"file": ("rechnung.pdf", pdf, "application/pdf")})
        assert "Rechnung 2026000001" in r.text and "importiert" in r.text and "Entwurf" in r.text
        assert "Importierte Rechnung" in r.text and "Original-PDF" in r.text
        bid = int(str(r.url).split("/admin/billings/")[1].split("?")[0])
        assert c.get(f"/admin/billings/{bid}/source.pdf").content[:4] == b"%PDF"

        # gleiche Rechnung nochmal -> kein Duplikat
        r = c.post("/admin/billings/import", files={"file": ("x.pdf", pdf, "application/pdf")})
        assert "bereits erfasst" in r.text

    # IMAP: zweite Rechnung (andere Nummer) liegt im Postfach
    pdf2 = make_pdf(AWATTAR_TEXT.replace("2026000001", "2026000002").replace("01.08.2026 - 31.08.2026",
                                                                              "01.09.2026 - 30.09.2026"))
    raw = make_eml(pdf2, "<test-2@awattar.de>")

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
                assert "SINCE" in args
                return "OK", [b"7"]
            assert "PEEK" in args[-1]
            return "OK", [(b"7 (BODY[] {123}", raw), b")"]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(mailbox, "configured", lambda: True)
    result = asyncio.run(mailbox.check_mailbox())
    assert "2026000002" in result
    assert "Keine neuen" in asyncio.run(mailbox.check_mailbox())
    with SessionLocal() as s:
        b = s.query(Billing).filter(Billing.invoice_no == "2026000002").one()
        assert b.mail_message_id == "<test-2@awattar.de>"
        assert str(b.period_start) == "2026-09-01"
        assert b.spot_price_ct == 14.09
    assert config.imap_sender == "awattar.de"


def test_auto_send_without_validation(monkeypatch):
    """Modus „immer automatisch“: abschließen und versenden, auch wenn Hinweise vorliegen."""
    import asyncio
    import smtplib

    from fastapi.testclient import TestClient

    from app import service
    from app.db import Party, SessionLocal
    from app.main import app

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **kw): pass
        def login(self, *a): pass
        def send_message(self, msg, to_addrs=None): sent.append(to_addrs)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    text = AWATTAR_TEXT.replace("2026000001", "2026000099").replace("Summe 30,93 € 36,81 €", "Summe 31,93 € 37,99 €")
    with TestClient(app) as c:
        c.post("/admin/settings", data={"import_mode": "auto_always"})
        with SessionLocal() as s:
            s.add(Party(name="Auto Mieter", email="auto@test.de", meters=[], active=True, is_owner=False, sort=0))
            s.commit()
            b, msg = asyncio.run(service.import_invoice(s, make_pdf(text), "r.pdf"))
            assert b.status == "final"
            assert "trotz" in msg and "versendet" in msg
        assert ["auto@test.de"] in sent
        c.post("/admin/settings", data={"import_mode": "auto_if_clean"})
        with SessionLocal() as s:
            b, msg = asyncio.run(service.import_invoice(s, make_pdf(text.replace("2026000099", "2026000098")), "r.pdf"))
            assert b.status == "draft" and "bitte prüfen" in msg  # Hinweis vorhanden -> Entwurf
        c.post("/admin/settings", data={"import_mode": "review"})


def _forward(pdf: bytes, inline: bool) -> "EmailMessage":
    """Weiterleitung aus dem Hauptpostfach: als Text (inline) oder mit angehängter Original-Mail."""
    from email import message_from_bytes, policy as _policy

    m = EmailMessage()
    m["From"] = "Timo <ich@hauptmail.de>"
    m["To"] = "nebenkosten@test.de"
    m["Subject"] = "WG: aWATTar - Rechnung Strom 08/2026"
    m["Message-ID"] = "<fwd-1@hauptmail.de>"
    if inline:
        m.set_content("---------- Weitergeleitete Nachricht ---------\nVon: aWATTar Service <service@awattar.de>\n"
                      "Betreff: Rechnung\n\nIm Anhang finden Sie Ihre Stromrechnung.")
        m.add_attachment(pdf, maintype="application", subtype="pdf", filename="rechnung.pdf")
    else:
        m.set_content("Siehe Anhang.")
        m.add_attachment(message_from_bytes(make_eml(pdf), policy=_policy.default))
    return message_from_bytes(m.as_bytes(), policy=_policy.default)


def test_sender_matching_and_forwarded_mails():
    from email import message_from_bytes, policy as _policy

    from app import mailbox

    pdf = make_pdf()
    direct = message_from_bytes(make_eml(pdf), policy=_policy.default)
    inline, attached = _forward(pdf, True), _forward(pdf, False)

    assert mailbox.matches(direct, ["awattar.de"])[0]
    assert mailbox.matches(inline, ["awattar.de"]) == (True, "weitergeleitet (awattar.de)")
    assert mailbox.matches(attached, ["awattar.de"])[0]
    assert ii.pdf_attachments(attached)  # PDF in der angehängten Original-Mail wird gefunden
    # ohne Erkennung der Weiterleitung nur über die eigene Absenderadresse
    assert not mailbox.matches(inline, ["awattar.de"], forwarded=False)[0]
    assert mailbox.matches(inline, ["awattar.de", "ich@hauptmail.de"], forwarded=False) == (True, "Absender ich@hauptmail.de")
    # fremde Mail
    spam = EmailMessage()
    spam["From"] = "jemand@example.com"
    spam.set_content("Hallo")
    assert not mailbox.matches(spam, ["awattar.de", "ich@hauptmail.de"])[0]
    assert mailbox.matches(spam, [])[0]  # keine Absender eingetragen = alle
    assert mailbox.sender_patterns({"imap_senders": " awattar.de; Ich@Hauptmail.de "}) == ["awattar.de", "ich@hauptmail.de"]
