"""E-Mail-Versand der Abrechnungen (SMTP, Zugangsdaten aus .env)."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import config


class MailError(Exception):
    pass


def configured() -> bool:
    return bool(config.smtp_host and config.smtp_from)


def send_mail(to: list[str], subject: str, body: str, attachments: list[tuple[str, bytes]],
              bcc: list[str] | None = None) -> None:
    if not configured():
        raise MailError("SMTP ist nicht konfiguriert (SMTP_HOST / SMTP_FROM in .env).")
    if not to:
        raise MailError("Keine E-Mail-Adresse hinterlegt.")
    msg = EmailMessage()
    msg["From"] = config.smtp_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)

    recipients = to + (bcc or [])
    ctx = ssl.create_default_context()
    if config.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
    with server:
        if config.smtp_security == "starttls":
            server.starttls(context=ctx)
        if config.smtp_user:
            server.login(config.smtp_user, config.smtp_password)
        server.send_message(msg, to_addrs=recipients)
