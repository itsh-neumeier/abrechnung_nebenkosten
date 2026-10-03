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
    # Postfach für eingehende Stromrechnungen (IMAP)
    imap_host: str = os.getenv("IMAP_HOST", "").strip()
    imap_port: int = int(os.getenv("IMAP_PORT", "993") or 993)
    imap_user: str = os.getenv("IMAP_USER", "").strip()
    imap_password: str = os.getenv("IMAP_PASSWORD", "")
    imap_folder: str = os.getenv("IMAP_FOLDER", "INBOX").strip()
    imap_sender: str = os.getenv("IMAP_SENDER", "awattar.de").strip()
    imap_interval_min: int = int(os.getenv("IMAP_INTERVAL_MIN", "60") or 60)
    imap_since_days: int = int(os.getenv("IMAP_SINCE_DAYS", "40") or 40)
    # Basis-URL für Links in Hinweis-Mails, z. B. https://abrechnung.example.de
    app_base_url: str = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    data_dir: str = os.getenv("DATA_DIR", "")
    # Victron-Logger im Hintergrund (Einstellungen im Webinterface); 0 = nie starten
    victron_logger: bool = os.getenv("VICTRON_LOGGER", "1") != "0"
    # VRM (Cloud, optional): persönlicher Zugriffstoken aus VRM → Einstellungen → Integrationen
    vrm_token: str = os.getenv("VRM_TOKEN", "").strip()
    # WhatsApp Business Cloud API (Meta, optional) – direkter Versand ohne n8n
    wa_token: str = os.getenv("WA_TOKEN", "").strip()  # dauerhaftes Zugriffstoken (Systembenutzer)
    wa_phone_number_id: str = os.getenv("WA_PHONE_NUMBER_ID", "").strip()
    wa_app_secret: str = os.getenv("WA_APP_SECRET", "").strip()  # prüft die Signatur des Status-Webhooks
    wa_api_version: str = os.getenv("WA_API_VERSION", "v21.0").strip()
    wa_graph_url: str = os.getenv("WA_GRAPH_URL", "https://graph.facebook.com").strip().rstrip("/")


config = Config()
