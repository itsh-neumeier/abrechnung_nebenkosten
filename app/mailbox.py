"""Postfach-Abruf: neue Stromrechnungen per IMAP erkennen und importieren.

Das Postfach wird nur gelesen (``readonly`` + ``BODY.PEEK``) – Mails werden
weder als gelesen markiert noch verschoben. Doppelte Importe verhindert die
Rechnungsnummer bzw. Message-ID.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
from datetime import date, datetime, timedelta

from . import invoice_import, service
from .config import config
from .db import Billing, SessionLocal

log = logging.getLogger("mailbox")
status: dict = {"last_run": None, "last_result": "", "running": False}


def configured() -> bool:
    return bool(config.imap_host and config.imap_user and config.imap_password)


def _fetch_messages() -> list[bytes]:
    since = (date.today() - timedelta(days=config.imap_since_days)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(config.imap_host, config.imap_port) as imap:
        imap.login(config.imap_user, config.imap_password)
        imap.select(f'"{config.imap_folder}"', readonly=True)
        criteria = ["SINCE", since]
        if config.imap_sender:
            criteria = ["FROM", f'"{config.imap_sender}"', *criteria]
        typ, data = imap.uid("SEARCH", None, *criteria)
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
        raws = await asyncio.to_thread(_fetch_messages)
        with SessionLocal() as s:
            known = {m for (m,) in s.query(Billing.mail_message_id).all() if m}
            for raw in raws:
                msg = invoice_import.parse_email(raw)
                mid = str(msg.get("Message-ID", ""))
                if mid and mid in known:
                    continue
                for name, pdf in invoice_import.pdf_attachments(msg):
                    try:
                        b, text = await service.import_invoice(s, pdf, name, mid, source=f"E-Mail „{msg['Subject']}“")
                        if b is not None and b.mail_message_id == mid:
                            results.append(text)
                    except invoice_import.ImportError_ as e:
                        results.append(f"{name}: {e}")
                known.add(mid)
        result = "; ".join(results) if results else f"Keine neuen Rechnungen ({len(raws)} Mail(s) geprüft)."
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
