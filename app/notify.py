"""Push-Benachrichtigungen an Benutzer (über :mod:`webpush`).

Anlässe:
* Mieter: eine Abrechnung ihrer Partei wurde im Mieterportal veröffentlicht (je Benutzer nur einmal).
* Verwalter: eine neue Stromrechnung ist eingegangen (Postfach / Upload).
"""

from __future__ import annotations

import logging
from datetime import datetime
from email.utils import parseaddr
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from . import render, webpush
from .config import config
from .db import Billing, Party, PushSubscription, User, get_settings, save_settings

log = logging.getLogger("notify")
MAX_FAILURES = 5


def vapid_key(s: Session) -> str:
    if config.vapid_private_key:
        return config.vapid_private_key
    st = get_settings(s)
    if not st["vapid_private"]:
        st["vapid_private"] = webpush.generate_vapid()
        save_settings(s, {"vapid_private": st["vapid_private"]})
        s.commit()
    return st["vapid_private"]


def public_key(s: Session) -> str:
    return webpush.public_key_b64(vapid_key(s))


def subject() -> str:
    """Kontakt für die Push-Dienste (Pflichtangabe in VAPID)."""
    addr = parseaddr(config.smtp_from)[1]
    if addr:
        return f"mailto:{addr}"
    if config.app_base_url.startswith("https://"):
        return config.app_base_url
    return "mailto:admin@localhost"


def subscribe(s: Session, user_id: Optional[int], data: dict, user_agent: str = "") -> PushSubscription:
    endpoint = str(data.get("endpoint", ""))
    keys = data.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("Ungültiges Push-Abo")
    sub = s.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first() or PushSubscription(
        endpoint=endpoint)
    sub.user_id, sub.p256dh, sub.auth = user_id, keys["p256dh"], keys["auth"]
    sub.user_agent, sub.failures = user_agent[:300], 0
    s.add(sub)
    s.commit()
    return sub


def unsubscribe(s: Session, endpoint: str) -> None:
    s.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).delete()
    s.commit()


def send_to(s: Session, subs: Iterable[PushSubscription], data: dict) -> tuple[int, list[str]]:
    """An alle Abos senden; abgelaufene Abos werden entfernt. Ergebnis: (zugestellt, Fehler)."""
    key, sub_claim = vapid_key(s), subject()
    ok, errors = 0, []
    for sub in list(subs):
        try:
            webpush.send({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}, data,
                         key, sub_claim)
            sub.last_ok, sub.failures = datetime.now(), 0
            ok += 1
        except webpush.PushError as e:
            sub.failures = (sub.failures or 0) + 1
            if e.gone or sub.failures >= MAX_FAILURES:
                s.delete(sub)
            errors.append(str(e))
        except Exception as e:  # noqa: BLE001 – Benachrichtigung darf nie den eigentlichen Vorgang stören
            log.exception("Push fehlgeschlagen")
            errors.append(str(e))
    s.commit()
    return ok, errors


def subs_for_users(s: Session, user_ids: Iterable[Optional[int]]) -> list[PushSubscription]:
    ids = list(user_ids)
    if not ids:
        return []
    q = s.query(PushSubscription)
    with_none = None in ids
    real = [i for i in ids if i is not None]
    subs = q.filter(PushSubscription.user_id.in_(real)).all() if real else []
    if with_none:
        subs += q.filter(PushSubscription.user_id.is_(None)).all()
    return subs


def admin_ids(s: Session) -> list[Optional[int]]:
    ids: list[Optional[int]] = [u.id for u in s.query(User).filter(User.role == "admin", User.active.is_(True))]
    return ids + [None]  # Abos aus der Zeit ohne Login


def billing_published(s: Session, b: Billing) -> int:
    """Mieter benachrichtigen, deren Partei die (veröffentlichte, abgeschlossene) Abrechnung sehen darf."""
    if not (b.published and b.status == "final"):
        return 0
    already = set(b.notified or [])
    parties = {p.id: p for p in s.query(Party).filter(Party.portal.is_(True)).all()}
    sent_to = []
    for rp in (b.result or {}).get("parties", []):
        if rp["id"] not in parties:
            continue
        users = s.query(User).filter(User.role == "tenant", User.active.is_(True), User.party_id == rp["id"]).all()
        targets = [u.id for u in users if u.id not in already]
        if not targets:
            continue
        data = {"title": f"Neue Nebenkostenabrechnung {b.period_start:%m/%Y}",
                "body": f"{rp['name']}: {render.fmt_eur(rp['total'])} · zahlbar bis {render.fmt_date(render.due_date(b))}",
                "url": f"/portal/{b.id}", "tag": f"billing-{b.id}"}
        send_to(s, subs_for_users(s, targets), data)
        sent_to += targets
    if sent_to:
        b.notified = sorted(already | set(sent_to))
        s.commit()
    return len(sent_to)


def invoice_imported(s: Session, b: Billing, text: str) -> int:
    data = {"title": "Neue Stromrechnung eingegangen", "body": f"{b.title}: {text}"[:180],
            "url": f"/billings/{b.id}", "tag": f"import-{b.id}"}
    ok, _ = send_to(s, subs_for_users(s, admin_ids(s)), data)
    return ok
