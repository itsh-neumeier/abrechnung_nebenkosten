"""Datenbankmodelle (SQLite via SQLAlchemy)."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint,
                        create_engine, inspect, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import config


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Party(Base):
    """Hauspartei / Mieter."""

    __tablename__ = "parties"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(200), default="")  # Wohnung / Lage
    unit_id: Mapped[str] = mapped_column(String(50), default="")  # Wohneinheiten-ID, z. B. WE-001
    address: Mapped[str] = mapped_column(Text, default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    meters: Mapped[list] = mapped_column(JSON, default=list)  # Shelly-Energie-Entitäten
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class FixedCost(Base):
    """Weitere Fixkosten je Abrechnung, z. B. IPTV-Bereitstellung."""

    __tablename__ = "fixed_costs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    amount_gross: Mapped[float] = mapped_column(Float, default=0.0)
    party_ids: Mapped[list] = mapped_column(JSON, default=list)  # leer = alle
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Allocation(Base):
    """Umlage, z. B. Warmwasserbereitung (Strom eines Shelly) oder Trinkwasser (m³ x Preis)."""

    __tablename__ = "allocations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(20), default="energy")  # energy | quantity | amount
    source_entity: Mapped[str] = mapped_column(String(255), default="")
    source_unit: Mapped[str] = mapped_column(String(20), default="m³")  # Einheit bei quantity
    price_source: Mapped[str] = mapped_column(String(20), default="custom")  # custom | water (Einstellungen)
    default_amount: Mapped[float] = mapped_column(Float, default=0.0)  # Betrag bzw. Preis je Einheit
    key_type: Mapped[str] = mapped_column(String(20), default="percent")  # entity | percent | equal
    key_unit: Mapped[str] = mapped_column(String(20), default="")
    key: Mapped[dict] = mapped_column(JSON, default=dict)  # {party_id: entity | prozent}
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Billing(Base):
    """Eine Abrechnung = eine Rechnung des Stromanbieters."""

    __tablename__ = "billings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    invoice_no: Mapped[str] = mapped_column(String(100), default="")
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    grid_kwh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    energy_cost_net: Mapped[float] = mapped_column(Float, default=0.0)
    fixed_cost_net: Mapped[float] = mapped_column(Float, default=0.0)
    vat_rate: Mapped[float] = mapped_column(Float, default=0.19)
    battery_rate_ct: Mapped[float] = mapped_column(Float, default=0.0)  # Batterieverschleißsatz
    spot_price_ct: Mapped[float] = mapped_column(Float, default=0.0)  # Ø Börsenpreis netto lt. Rechnung
    pv_rate_ct: Mapped[float] = mapped_column(Float, default=0.0)  # PV-Bereitstellungssatz
    sent: Mapped[dict] = mapped_column(JSON, default=dict)  # party_id -> Versandzeitpunkt / Fehler
    source_file: Mapped[str] = mapped_column(String(255), default="")  # importierte Original-Rechnung (PDF)
    import_info: Mapped[dict] = mapped_column(JSON, default=dict)  # Positionen / Prüfungen des Imports
    mail_message_id: Mapped[str] = mapped_column(String(255), default="")
    values: Mapped[dict] = mapped_column(JSON, default=dict)  # entity_id -> Verbrauch
    values_meta: Mapped[dict] = mapped_column(JSON, default=dict)  # entity_id -> Methode/Abdeckung aus HA
    amounts: Mapped[dict] = mapped_column(JSON, default=dict)  # allocation_id -> Betrag
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | final
    notes: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class VictronBucket(Base):
    """Victron direkt: Energie je 15-Minuten-Block und Schlüssel."""

    __tablename__ = "victron_buckets"
    __table_args__ = (UniqueConstraint("start", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    start: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    key: Mapped[str] = mapped_column(String(64), index=True)
    kwh: Mapped[float] = mapped_column(Float, default=0.0)
    seconds: Mapped[float] = mapped_column(Float, default=0.0)  # abgedeckte Zeit


class VictronState(Base):
    """Victron direkt: letzter gelesener Zählerstand je Schlüssel."""

    __tablename__ = "victron_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    raw: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime)  # UTC


if config.database_url.startswith("sqlite:///"):
    os.makedirs(os.path.dirname(config.database_url.removeprefix("sqlite:///")) or ".", exist_ok=True)

engine = create_engine(config.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Einfache Migration: neue Spalten in bestehenden Tabellen ergänzen."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if isinstance(default, bool):
                    literal = "1" if default else "0"
                elif isinstance(default, (int, float)):
                    literal = str(default)
                elif isinstance(default, str):
                    literal = "'" + default.replace("'", "''") + "'"
                elif isinstance(col.type, JSON):
                    literal = "'{}'" if col.name in ("sent", "values", "amounts", "result", "key", "import_info", "values_meta") else "'[]'"
                else:
                    literal = "NULL"
                coltype = col.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {coltype} DEFAULT {literal}'))


def get_session():
    with SessionLocal() as s:
        yield s


SETTING_DEFAULTS = {
    # Entitäten
    "entity_grid": "",
    "entity_total": "",
    "entity_battery": "",  # Batterie entladen
    "entity_battery_charge": "",  # Batterie geladen gesamt
    "entity_battery_charge_grid": "",  # Batterie aus Netz geladen (dyn. ESS)
    "entity_pv_direct": "",  # optional
    # Sätze
    "battery_rate_ct": "8",  # Batterieverschleißsatz
    "pv_rate_ct": "5",  # PV-Bereitstellungssatz
    "water_price_m3": "",  # Trinkwasser €/m³ brutto
    "sewage_price_m3": "",  # Abwasser €/m³ brutto
    "vat_rate": "19",
    "owner_free_own_energy": "1",  # Eigentümer zahlt keinen PV-/Batterie-/Graustrom
    # Objekt
    "building_title": "Nebenkostenabrechnung",
    "building_address": "",
    "building_id": "",
    # E-Mail
    "mail_auto_send": "",
    "mail_subject": "Nebenkostenabrechnung Strom {zeitraum} – {wohneinheit}",
    "mail_body": "Hallo {name},\n\nanbei die Nebenkostenabrechnung Strom für den Zeitraum {zeitraum}.\n"
                 "Betrag: {betrag}\n\nViele Grüße\n{absender}",
    "mail_bcc": "",
    # Rechnungsimport
    "import_mode": "review",  # review | auto_if_clean | auto_always
    "notify_email": "",
    # Victron direkt (Modbus TCP, nur lesend)
    "victron_enabled": "",
    "victron_host": "",
    "victron_port": "502",
    "victron_units": "",  # gefundene Unit-IDs (JSON), leer = automatisch suchen
    # VRM (Cloud, optional) – Token kommt aus VRM_TOKEN
    "vrm_site_id": "",  # Hinweis-Mail bei neu importierter Rechnung
    "landlord_name": "",
    "landlord_address": "",
    "landlord_contact": "",
    "landlord_iban": "",
    "payment_days": "14",
    "invoice_text": "",
}


def get_settings(s: Session) -> dict[str, str]:
    out = dict(SETTING_DEFAULTS)
    for row in s.query(Setting).all():
        out[row.key] = row.value
    return out


def save_settings(s: Session, data: dict[str, str]) -> None:
    for k, v in data.items():
        if k not in SETTING_DEFAULTS:
            continue
        row = s.get(Setting, k)
        if row is None:
            s.add(Setting(key=k, value=v))
        else:
            row.value = v
    s.commit()
