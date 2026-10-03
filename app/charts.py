"""Einfache SVG-Diagramme für die Abrechnung (serverseitig, ohne JavaScript – druckfest im PDF)."""

from __future__ import annotations

import math
from datetime import date
from html import escape
from typing import Optional

SOURCE_COLORS = {
    "grid": ("#64748b", "Netzstrom"),
    "pv": ("#f59e0b", "PV direkt"),
    "bat_pv": ("#16a34a", "Batterie aus PV"),
    "bat_grid": ("#8b5cf6", "Batterie aus Netz"),
}


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if m * exp >= v:
            return m * exp
    return 10 * exp


def _de(x: float, digits: int = 1) -> str:
    return f"{x:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _frame(days: list[str], vmax: float, unit: str, width: int, height: int, left: int, bottom: int,
           top: int = 14, right: int = 8) -> tuple[list[str], float, float]:
    plot_w = width - left - right
    plot_h = height - top - bottom
    parts = []
    for i in range(5):  # Rasterlinien
        v = vmax * i / 4
        y = top + plot_h - plot_h * i / 4
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#e5e7eb" stroke-width="0.6"/>')
        digits = 0 if float(v).is_integer() else (2 if vmax < 10 else 1)
        parts.append(f'<text x="{left - 4}" y="{y + 3:.1f}" font-size="7" text-anchor="end" fill="#6b7280">{_de(v, digits)}</text>')
    parts.append(f'<text x="{left - 4}" y="{top - 5}" font-size="7" text-anchor="end" fill="#6b7280">{escape(unit)}</text>')
    step = plot_w / max(1, len(days))
    for i, d in enumerate(days):  # Tageszahlen
        day = date.fromisoformat(d).day
        if len(days) <= 16 or day % 2 == 1 or i == len(days) - 1:
            x = left + step * (i + 0.5)
            parts.append(f'<text x="{x:.1f}" y="{height - bottom + 10}" font-size="7" text-anchor="middle" fill="#374151">{day}</text>')
    return parts, step, plot_h


def bar_chart(days: list[str], values: list[Optional[float]], unit: str = "kWh", color: str = "#2563eb",
              width: int = 680, height: int = 190, avg_label: str = "Ø") -> str:
    """Balken je Tag + gestrichelte Durchschnittslinie."""
    vals = [v or 0.0 for v in values]
    vmax = _nice_max(max(vals) if vals else 0)
    left, bottom, top = 34, 18, 14
    parts, step, plot_h = _frame(days, vmax, unit, width, height, left, bottom, top)
    bw = step * 0.7
    for i, v in enumerate(vals):
        h = plot_h * v / vmax
        x = left + step * i + (step - bw) / 2
        parts.append(f'<rect x="{x:.1f}" y="{top + plot_h - h:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{color}" rx="1"/>')
    known = [v for v in values if v is not None]
    if known:
        avg = sum(known) / len(known)
        y = top + plot_h - plot_h * avg / vmax
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - 8}" y2="{y:.1f}" stroke="#dc2626" stroke-width="0.9" stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{width - 10}" y="{y - 3:.1f}" font-size="7.5" text-anchor="end" fill="#dc2626">'
                     f'{escape(avg_label)} {_de(avg, 2)} {escape(unit)}/Tag</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" '
            f'font-family="DejaVu Sans, Arial, sans-serif">{"".join(parts)}</svg>')


def stacked_chart(days: list[str], series: dict[str, list[float]], unit: str = "kWh",
                  width: int = 680, height: int = 190) -> str:
    """Gestapelte Balken je Tag (Energiemix) mit Legende."""
    keys = [k for k in SOURCE_COLORS if k in series]
    totals = [sum(series[k][i] or 0 for k in keys) for i in range(len(days))]
    vmax = _nice_max(max(totals) if totals else 0)
    left, bottom, top = 34, 30, 14
    parts, step, plot_h = _frame(days, vmax, unit, width, height, left, bottom, top)
    bw = step * 0.7
    for i in range(len(days)):
        y = top + plot_h
        x = left + step * i + (step - bw) / 2
        for k in keys:
            v = series[k][i] or 0
            h = plot_h * v / vmax
            if h > 0:
                y -= h
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{SOURCE_COLORS[k][0]}"/>')
    lx = left
    for k in keys:  # Legende
        color, label = SOURCE_COLORS[k]
        parts.append(f'<rect x="{lx}" y="{height - 10}" width="8" height="8" fill="{color}"/>')
        parts.append(f'<text x="{lx + 11}" y="{height - 3}" font-size="7.5" fill="#374151">{escape(label)}</text>')
        lx += 22 + 5.2 * len(label)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" '
            f'font-family="DejaVu Sans, Arial, sans-serif">{"".join(parts)}</svg>')
