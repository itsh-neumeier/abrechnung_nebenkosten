"""Konfiguration über Umgebungsvariablen (siehe .env.example)."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    ha_url: str = os.getenv("HA_URL", "").strip()
    ha_token: str = os.getenv("HA_TOKEN", "").strip()
    timezone: str = os.getenv("TZ", "Europe/Berlin")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/abrechnung.db")
    app_user: str = os.getenv("APP_USER", "")
    app_password: str = os.getenv("APP_PASSWORD", "")
    # E-Mail-Versand
    smtp_host: str = os.getenv("SMTP_HOST", "").strip()
    smtp_port: int = int(os.getenv("SMTP_PORT", "587") or 587)
    smtp_security: str = os.getenv("SMTP_SECURITY", "starttls").strip().lower()  # starttls | ssl | none
    smtp_user: str = os.getenv("SMTP_USER", "").strip()
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "").strip()


config = Config()
