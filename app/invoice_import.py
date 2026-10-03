"""Import der Stromrechnung des Lieferanten (PDF oder E-Mail mit PDF-Anhang).

Getestet mit dem Rechnungsformat von aWATTar (HOURLY-Tarif). Die Detailaufstellung
besteht aus Positionszeilen der Form::

    <Beschreibung> <von> – <bis> <Preis> <Preiseinheit> <Menge> <Einheit> <Netto> €

* Preiseinheit ``Cent/kWh``           -> verbrauchsabhängig (Arbeitspreis)
* Preiseinheit ``Euro/Monat|Jahr|Tag`` -> Fixkosten (Grundpreis, Grundentgelt …)
* Position „Stromverbrauch (HOURLY)“  -> Ø Börsenpreis des Monats
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Optional


class ImportError_(Exception):
    pass


NUM = r"-?\d{1,3}(?:\.\d{3})*,\d+|-?\d+,\d+"
DATE = r"\d{2}\.\d{2}\.\d{4}"
POSITION = re.compile(
    rf"(?P<desc>.*?)\s*(?P<von>{DATE})\s*[–-]\s*(?P<bis>{DATE})\s+(?P<price>{NUM})\s+(?P<punit>\S+/\S+)\s+"
    rf"(?P<qty>{NUM})\s+(?P<qunit>\S+)\s+(?P<net>{NUM})\s*€\s*$"
)
SPOT_DESC = re.compile(r"HOURLY|Stromverbrauch|Börse|Spot|Energiepreis", re.I)


def de_float(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def de_date(s: str) -> date:
    d, m, y = s.split(".")
    return date(int(y), int(m), int(d))


@dataclass
class Position:
    description: str
    period_from: str
    period_to: str
    price: float
    price_unit: str
    quantity: float
    quantity_unit: str
    net: float
    kind: str  # energy | fixed | other

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ParsedInvoice:
    supplier: str = ""
    invoice_no: str = ""
    invoice_date: Optional[date] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    grid_kwh: Optional[float] = None
    energy_cost_net: float = 0.0
    fixed_cost_net: float = 0.0
    spot_price_ct: Optional[float] = None
    vat_rate: Optional[float] = None  # 0.19
    total_net: Optional[float] = None
    total_gross: Optional[float] = None
    stated_work_price_ct: Optional[float] = None  # „Arbeitspreis“ lt. Rechnung (brutto)
    positions: list[Position] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def info(self) -> dict:
        return {
            "supplier": self.supplier,
            "invoice_no": self.invoice_no,
            "invoice_date": self.invoice_date.isoformat() if self.invoice_date else None,
            "total_net": self.total_net,
            "total_gross": self.total_gross,
            "stated_work_price_ct": self.stated_work_price_ct,
            "positions": [p.as_dict() for p in self.positions],
            "warnings": list(self.warnings),
        }


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:  # noqa: BLE001
        raise ImportError_(f"PDF konnte nicht gelesen werden: {e}") from e


def _classify(price_unit: str) -> str:
    u = price_unit.lower()
    if u.endswith("/kwh"):
        return "energy"
    if u.split("/")[-1] in ("monat", "jahr", "tag"):
        return "fixed"
    return "other"


def parse_text(text: str) -> ParsedInvoice:
    inv = ParsedInvoice()
    if re.search(r"awattar", text, re.I):
        inv.supplier = "aWATTar"

    if m := re.search(r"Rechnungsnummer:?\s*(\S+)", text):
        inv.invoice_no = m.group(1)
    if m := re.search(rf"Rechnungsdatum:?\s*({DATE})", text):
        inv.invoice_date = de_date(m.group(1))
    if m := re.search(rf"Zeitraum\s+(?:vom\s+)?({DATE})\s*(?:-|–|bis)\s*({DATE})", text):
        inv.period_start, inv.period_end = de_date(m.group(1)), de_date(m.group(2))
    if m := re.search(rf"Bezug\s+({NUM})\s*kWh", text):
        inv.grid_kwh = de_float(m.group(1))
    if m := re.search(r"MWSt\.?\s*(\d+(?:,\d+)?)\s*%", text, re.I):
        inv.vat_rate = de_float(m.group(1) if "," in m.group(1) else m.group(1) + ",0") / 100
    if m := re.search(rf"Summe\s+({NUM})\s*€\s+({NUM})\s*€", text):
        inv.total_net, inv.total_gross = de_float(m.group(1)), de_float(m.group(2))
    if m := re.search(rf"Arbeitspreis:?\s*({NUM})\s*Cent/kWh", text):
        inv.stated_work_price_ct = de_float(m.group(1))

    # Detailaufstellung zeilenweise; mehrzeilige Beschreibungen werden gesammelt
    buffer: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = POSITION.match(line)
        if m:
            desc = (m.group("desc").strip() or " ".join(buffer)).strip()
            buffer = []
            inv.positions.append(Position(
                description=desc,
                period_from=m.group("von"), period_to=m.group("bis"),
                price=de_float(m.group("price")), price_unit=m.group("punit"),
                quantity=de_float(m.group("qty")), quantity_unit=m.group("qunit"),
                net=de_float(m.group("net")), kind=_classify(m.group("punit")),
            ))
        elif line.startswith(("Beschreibung", "Summe")) or re.match(r"^Seite \d", line):
            buffer = []
        else:
            buffer.append(line)
            buffer = buffer[-3:]

    if not inv.positions:
        raise ImportError_("Keine Rechnungspositionen gefunden – Rechnungsformat wird nicht unterstützt.")

    inv.energy_cost_net = round(sum(p.net for p in inv.positions if p.kind == "energy"), 2)
    inv.fixed_cost_net = round(sum(p.net for p in inv.positions if p.kind == "fixed"), 2)
    for p in inv.positions:
        if p.kind == "other":
            inv.warnings.append(f"Position „{p.description}“ ({p.price_unit}) nicht zugeordnet – bitte prüfen.")

    spot = [p for p in inv.positions if p.kind == "energy" and SPOT_DESC.search(p.description)]
    if spot:
        qty = sum(p.quantity for p in spot)
        inv.spot_price_ct = (sum(p.net for p in spot) / qty * 100) if len(spot) > 1 and qty else spot[0].price
        if inv.grid_kwh is None:
            inv.grid_kwh = qty
    else:
        inv.warnings.append("Kein Börsenpreis (Stromverbrauch HOURLY) gefunden – bitte manuell eintragen.")

    if inv.period_start is None:
        froms = [de_date(p.period_from) for p in inv.positions]
        tos = [de_date(p.period_to) for p in inv.positions]
        inv.period_start, inv.period_end = min(froms), max(tos)

    # Plausibilitätsprüfungen
    if inv.total_net is not None:
        diff = inv.total_net - inv.energy_cost_net - inv.fixed_cost_net
        if abs(diff) > 0.05:
            inv.warnings.append(
                f"Summe der Positionen ({inv.energy_cost_net + inv.fixed_cost_net:.2f} €) weicht von der "
                f"Rechnungssumme netto ({inv.total_net:.2f} €) ab."
            )
    if inv.stated_work_price_ct and inv.grid_kwh and inv.vat_rate is not None:
        calc_ct = inv.energy_cost_net / inv.grid_kwh * 100 * (1 + inv.vat_rate)
        if abs(calc_ct - inv.stated_work_price_ct) > 0.2:
            inv.warnings.append(
                f"Berechneter Ø Arbeitspreis ({calc_ct:.2f} ct brutto) weicht vom ausgewiesenen "
                f"({inv.stated_work_price_ct:.2f} ct) ab."
            )
    return inv


def parse_pdf(data: bytes) -> ParsedInvoice:
    return parse_text(pdf_text(data))


def pdf_attachments(msg: EmailMessage) -> list[tuple[str, bytes]]:
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename() or ""
        if part.get_content_type() == "application/pdf" or name.lower().endswith(".pdf"):
            out.append((name or "rechnung.pdf", part.get_payload(decode=True) or b""))
    return out


def parse_email(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def load_upload(filename: str, data: bytes) -> list[tuple[str, bytes, str]]:
    """PDF oder .eml -> Liste (Dateiname, PDF-Bytes, Message-ID)."""
    if data[:5] == b"%PDF-" or filename.lower().endswith(".pdf"):
        return [(filename or "rechnung.pdf", data, "")]
    msg = parse_email(data)
    pdfs = pdf_attachments(msg)
    if not pdfs:
        raise ImportError_("Die E-Mail enthält keinen PDF-Anhang.")
    return [(n, d, str(msg.get("Message-ID", ""))) for n, d in pdfs]
