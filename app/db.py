"""Datenbankmodelle (SQLite via SQLAlchemy)."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, String, Text, create_engine
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
    """Umlage nach Verbrauch, z. B. Warmwasserbereitung oder Wasser."""

    __tablename__ = "allocations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(20), default="energy")  # energy | amount
    source_entity: Mapped[str] = mapped_column(String(255), default="")
    default_amount: Mapped[float] = mapped_column(Float, default=0.0)
    key_type: Mapped[str] = mapped_column(String(20), default="entity")  # entity | percent | equal
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
    battery_rate_ct: Mapped[float] = mapped_column(Float, default=0.0)
    values: Mapped[dict] = mapped_column(JSON, default=dict)  # entity_id -> Verbrauch
    amounts: Mapped[dict] = mapped_column(JSON, default=dict)  # allocation_id -> Betrag
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | final
    notes: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


if config.database_url.startswith("sqlite:///"):
    os.makedirs(os.path.dirname(config.database_url.removeprefix("sqlite:///")) or ".", exist_ok=True)

engine = create_engine(config.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session():
    with SessionLocal() as s:
        yield s


SETTING_DEFAULTS = {
    "entity_grid": "",
    "entity_total": "",
    "entity_battery": "",
    "battery_rate_ct": "10",
    "vat_rate": "19",
    "landlord_name": "",
    "landlord_address": "",
    "landlord_contact": "",
    "landlord_iban": "",
    "payment_days": "14",
    "invoice_text": "Hiermit rechne ich die Stromkosten für den oben genannten Zeitraum ab.",
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
