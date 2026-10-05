"""Produktname je Rolle: Verwalter sehen „ImmoVerwaltung“, Mieter „Mein Zuhause“ (Name einstellbar).

Gilt für Webseiten, die installierbare App (eigenes Manifest je Rolle) und Mails an Mieter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

ADMIN_NAME = "ImmoVerwaltung"
TENANT_NAME = "Mein Zuhause"
ROLE_COOKIE = "nk_app"  # merkt sich die zuletzt angemeldete Rolle (für die Login-Seite der installierten App)


@dataclass(frozen=True)
class Brand:
    name: str
    kind: str  # admin | tenant

    @property
    def start_url(self) -> str:
        return "/admin" if self.kind == "admin" else "/"


def clean(name: str) -> str:
    name = " ".join((name or "").split())[:30]
    return name


def admin_name(st: dict) -> str:
    return clean(st.get("admin_app_name", "")) or ADMIN_NAME


def tenant_default(st: dict) -> str:
    return clean(st.get("tenant_app_name", "")) or TENANT_NAME


def for_party(st: dict, party=None) -> str:
    """Name der Mieter-App (für alle Parteien gleich)."""
    return tenant_default(st)


def resolve(s: Session, user, st: dict, path: str = "", next_url: str = "", role_hint: str = "") -> Brand:
    """Name nach Bereich: „/admin…“ = Verwalter-App, „/“ (Mein Zuhause) und Portal-Dokumente = Mieter-App.
    Gemeinsame Seiten (Login, Konto, App) richten sich nach der Rolle bzw. der zuletzt genutzten App."""
    admin = Brand(admin_name(st), "admin")
    tenant = Brand(tenant_default(st), "tenant")
    if path == "/admin" or path.startswith("/admin/"):
        return admin
    if path == "/" or path.startswith("/portal"):
        return tenant
    if user is not None:
        return admin if user.is_admin else tenant
    if next_url.startswith("/admin") or role_hint == "admin":
        return admin
    return tenant
