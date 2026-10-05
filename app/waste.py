"""Abfallkalender (ICS): Abholtermine lesen und Mieter am Vortag bzw. am Abholtag benachrichtigen.

Quelle: Link auf die ICS-Datei des Entsorgers (wird täglich neu geladen) oder hochgeladene Datei.
Unterstützt Einzeltermine sowie einfache Wiederholungen (RRULE FREQ=DAILY/WEEKLY/MONTHLY mit INTERVAL,
COUNT, UNTIL) und EXDATE. Die Müllart ist der Titel (SUMMARY) des Termins; welche Arten benachrichtigt
werden, wählt der Verwalter aus.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

# Farben/Symbole nach Stichwort im Titel (erste passende Regel gewinnt)
STYLES = [
    (r"bio|kompost", ("#92400e", "🟤")),
    (r"papier|pappe|karton|blau", ("#2563eb", "🔵")),
    (r"gelb|verpack|wertstoff|leichtverp|lvp", ("#eab308", "🟡")),
    (r"glas", ("#16a34a", "🟢")),
    (r"rest|haus", ("#374151", "⚫")),
    (r"sperr", ("#7c3aed", "🟣")),
    (r"grün|gruen|garten|baum|laub|strauch", ("#65a30d", "🌿")),
    (r"schadstoff|problem", ("#dc2626", "🔴")),
    (r"windel", ("#ea580c", "🟠")),
]


def style(kind: str) -> tuple[str, str]:
    low = kind.lower()
    for pattern, st in STYLES:
        if re.search(pattern, low):
            return st
    return ("#6b7280", "🗑️")


# --------------------------------------------------------------------------- ICS lesen
def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _date(v: str) -> Optional[date]:
    m = re.match(r"(\d{4})(\d{2})(\d{2})", v.strip())
    return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def _expand(start: date, rule: str, horizon: date, exdates: set[date]) -> list[date]:
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = parts.get("FREQ", "").upper()
    step = int(parts.get("INTERVAL", "1") or 1)
    count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    until = _date(parts["UNTIL"]) if parts.get("UNTIL") else None
    out, d, n = [], start, 0
    while d <= horizon and (until is None or d <= until) and (count is None or n < count) and n < 2000:
        if d not in exdates:
            out.append(d)
        n += 1
        if freq == "DAILY":
            d += timedelta(days=step)
        elif freq == "WEEKLY":
            d += timedelta(weeks=step)
        elif freq == "MONTHLY":
            y, mth = divmod(d.month - 1 + step, 12)
            try:
                d = d.replace(year=d.year + y, month=mth + 1)
            except ValueError:  # 31. im kürzeren Monat → überspringen
                d = (d.replace(day=1, year=d.year + y, month=mth + 1))
        else:
            break
    return out


def parse_ics(text: str, horizon_days: int = 400) -> list[tuple[date, str]]:
    """[(Datum, Müllart)] sortiert, ohne Duplikate."""
    events: list[tuple[date, str]] = []
    horizon = date.today() + timedelta(days=horizon_days)
    cur: Optional[dict] = None
    for line in _unfold(text):
        if line.startswith("BEGIN:VEVENT"):
            cur = {"ex": set()}
        elif line.startswith("END:VEVENT") and cur is not None:
            start, summary = cur.get("start"), cur.get("summary", "").strip()
            if start and summary:
                dates = _expand(start, cur["rrule"], horizon, cur["ex"]) if cur.get("rrule") else [start]
                events += [(d, summary) for d in dates]
            cur = None
        elif cur is not None and ":" in line:
            name, _, value = line.partition(":")
            key = name.split(";", 1)[0].upper()
            if key == "DTSTART":
                cur["start"] = _date(value)
            elif key == "SUMMARY":
                cur["summary"] = _unescape(value)
            elif key == "RRULE":
                cur["rrule"] = value.strip()
            elif key == "EXDATE":
                cur["ex"].update(d for d in (_date(x) for x in value.split(",")) if d)
    return sorted(set(events))


def _get(url: str, timeout: float) -> str:
    r = httpx.get(url, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    if "BEGIN:VCALENDAR" not in r.text:
        raise ValueError("Keine ICS-Datei (BEGIN:VCALENDAR fehlt)")
    return r.text


def fetch(url: str, timeout: float = 30.0, today: Optional[date] = None) -> str:
    """ICS laden. Enthält der Link ein Jahr (``year=2026``), werden aktuelles und nächstes Jahr geladen und
    zusammengeführt – so läuft der Kalender über den Jahreswechsel weiter."""
    url = url.strip().replace("webcal://", "https://", 1)
    year = (today or date.today()).year
    m = re.search(r"([?&]year=)(\d{4})", url)
    if not m:
        return _get(url, timeout)
    texts = [_get(url[:m.start(2)] + str(year) + url[m.end(2):], timeout)]
    try:
        texts.append(_get(url[:m.start(2)] + str(year + 1) + url[m.end(2):], timeout))
    except Exception:  # noqa: BLE001 – nächstes Jahr ist oft erst ab Herbst/Dezember verfügbar
        pass
    return merge(texts)


def merge(texts: list[str]) -> str:
    """Mehrere Kalender zu einem zusammenfügen (nur die VEVENT-Blöcke)."""
    if len(texts) == 1:
        return texts[0]
    events = []
    for t in texts:
        events += re.findall(r"BEGIN:VEVENT.*?END:VEVENT", t.replace("\r\n", "\n"), flags=re.S)
    return "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:ImmoVerwaltung\n" + "\n".join(events) + "\nEND:VCALENDAR\n"


# --------------------------------------------------------------------------- Auswertung
def kinds(events: list[tuple[date, str]]) -> list[str]:
    return sorted({k for _, k in events})


def upcoming(events: list[tuple[date, str]], enabled: Optional[list[str]], days: int = 60,
             today: Optional[date] = None) -> list[tuple[date, list[str]]]:
    """Abholtage ab heute, je Tag die (ausgewählten) Müllarten."""
    today = today or date.today()
    by_day: dict[date, list[str]] = {}
    for d, k in events:
        if today <= d <= today + timedelta(days=days) and (not enabled or k in enabled):
            by_day.setdefault(d, []).append(k)
    return sorted(by_day.items())


def _hm(value: str, default: str) -> tuple[int, int]:
    m = re.match(r"(\d{1,2}):(\d{2})", value or default) or re.match(r"(\d{1,2}):(\d{2})", default)
    return int(m[1]), int(m[2])


def due_notifications(events: list[tuple[date, str]], st: dict, now: datetime,
                      sent: set[str]) -> list[tuple[str, str, str, list[str]]]:
    """Fällige Benachrichtigungen: [(Schlüssel, Titel, Text, Müllarten)].

    Vortag: ab der Abendzeit bis Mitternacht. Abholtag: ab der Morgenzeit bis 12 Uhr (danach nicht mehr sinnvoll).
    """
    enabled = json.loads(st.get("waste_types") or "[]")
    out = []
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    if st.get("waste_notify_evening"):
        h, m = _hm(st.get("waste_evening_time", ""), "18:00")
        if now >= now.replace(hour=h, minute=m, second=0, microsecond=0):
            ks = [k for d, k in events if d == tomorrow and (not enabled or k in enabled)]
            key = f"{tomorrow.isoformat()}|evening"
            if ks and key not in sent:
                out.append((key, f"🗑️ Morgen Abholung: {', '.join(ks)}",
                            "Bitte die Tonne(n) heute Abend bzw. morgen früh rechtzeitig an die Straße stellen.", ks))
    if st.get("waste_notify_morning"):
        h, m = _hm(st.get("waste_morning_time", ""), "06:00")
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start <= now < now.replace(hour=12, minute=0, second=0, microsecond=0):
            ks = [k for d, k in events if d == today and (not enabled or k in enabled)]
            key = f"{today.isoformat()}|morning"
            if ks and key not in sent:
                out.append((key, f"🗑️ Heute Abholung: {', '.join(ks)}",
                            "Falls noch nicht geschehen: jetzt die Tonne(n) an die Straße stellen.", ks))
    return out
