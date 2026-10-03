"""Postfach-Abruf: neue Stromrechnungen per IMAP erkennen und importieren.

Das Postfach wird nur gelesen (``readonly`` + ``BODY.PEEK``) – Mails werden
weder als gelesen markiert noch verschoben. Doppelte Importe verhindert die
Rechnungsnummer bzw. Message-ID.

Welche Mails als Rechnung gelten, legen die Absender in den Einstellungen fest (mehrere möglich,
z. B. ``awattar.de, ich@hauptmail.de``). Weitergeleitete Mails werden zusätzlich am Original-Absender
erkannt – im weitergeleiteten Text („Von: … @awattar.de“) oder in einer angehängten Original-Mail.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
from datetime import date, datetime, timedelta

from . import invoice_import, service
from .config import config
from email.message import EmailMessage

from .db import Billing, SessionLocal, get_settings

log = logging.getLogger("mailbox")
status: dict = {"last_run": None, "last_result": "", "running": False}


def configured() -> bool:
    return bool(config.imap_host and config.imap_user and config.imap_password)


def sender_patterns(st: dict) -> list[str]:
    raw = st.get("imap_senders") or config.imap_sender or ""
    return [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]


def _texts(msg: EmailMessage) -> list[str]:
    """Absender angehängter Original-Mails und Textteile (für weitergeleitete Mails)."""
    out = []
    for part in msg.walk():
        if part is msg:
            continue
        if part.get("From"):
            out.append(str(part.get("From")))
        if part.get_content_type() in ("text/plain", "text/html") and not part.get_filename():
            try:
                out.append(part.get_content()[:200_000])
            except Exception:  # noqa: BLE001 – kaputte Kodierung ignorieren
                pass
    return out


def matches(msg: EmailMessage, patterns: list[str], forwarded: bool = True) -> tuple[bool, str]:
    """Passt die Mail zu einem Absender? Ergebnis: (ja/nein, Grund)."""
    if not patterns:
        return True, "alle Absender"
    sender = str(msg.get("From", "")).lower()
    for p in patterns:
        if p in sender:
            return True, f"Absender {p}"
    if forwarded:
        for text in _texts(msg):
            low = text.lower()
            for p in patterns:
                if p in low:
                    return True, f"weitergeleitet ({p})"
    return False, ""


def _fetch_messages(since_days: int) -> list[bytes]:
    since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(config.imap_host, config.imap_port) as imap:
        imap.login(config.imap_user, config.imap_password)
        imap.select(f'"{config.imap_folder}"', readonly=True)
        # Filter nach Absender passiert in Python (weitergeleitete Mails haben einen anderen Absender)
        typ, data = imap.uid("SEARCH", None, "SINCE", since)
        if typ != "OK":
            raise RuntimeError(f"IMAP-Suche fehlgeschlagen: {data}")
        out = []
        for uid in (data[0] or b"").split():
            typ, msg = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ == "OK" and msg and isinstance(msg[0], tuple):
                out.append(msg[0][1])
        return out


async def check_mailbox() -> str:
    """Prüft das Postfach einmal und importiert neue Rechnungen."""
    if not configured():
        return "IMAP ist nicht konfiguriert."
    if status["running"]:
        return "Abruf läuft bereits."
    status["running"] = True
    results = []
    try:
        raws = await asyncio.to_thread(_fetch_messages, config.imap_since_days)
        matched = 0
        with SessionLocal() as s:
            st = get_settings(s)
            patterns = sender_patterns(st)
            forwarded = bool(st.get("imap_forwarded"))
            known = {m for (m,) in s.query(Billing.mail_message_id).all() if m}
            for raw in raws:
                msg = invoice_import.parse_email(raw)
                mid = str(msg.get("Message-ID", ""))
                if mid and mid in known:
                    continue
                ok, _why = matches(msg, patterns, forwarded)
                if not ok or not invoice_import.pdf_attachments(msg):
                    continue
                matched += 1
                for name, pdf in invoice_import.pdf_attachments(msg):
                    try:
                        b, text = await service.import_invoice(s, pdf, name, mid, source=f"E-Mail „{msg['Subject']}“")
                        if b is not None and b.mail_message_id == mid:
                            results.append(text)
                    except invoice_import.ImportError_ as e:
                        results.append(f"{name}: {e}")
                known.add(mid)
        result = "; ".join(results) if results else (
            f"Keine neuen Rechnungen ({len(raws)} Mail(s) geprüft, {matched} neue mit passendem Absender und PDF).")
    except Exception as e:  # noqa: BLE001
        log.exception("Postfach-Abruf fehlgeschlagen")
        result = f"Fehler beim Postfach-Abruf: {e}"
    finally:
        status["running"] = False
    status.update(last_run=datetime.now().isoformat(timespec="seconds"), last_result=result)
    return result


async def poll_forever() -> None:
    await asyncio.sleep(10)
    while True:
        await check_mailbox()
        await asyncio.sleep(max(5, config.imap_interval_min) * 60)
