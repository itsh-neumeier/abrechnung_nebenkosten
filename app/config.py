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


config = Config()
