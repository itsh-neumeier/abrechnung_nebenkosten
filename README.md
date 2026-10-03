# ⚡ Stromabrechnung Hausparteien

Web-Tool zur monatlichen Weiterberechnung der Stromrechnung an die Parteien eines Hauses –
mit Messwerten aus **Home Assistant** (Shelly, Victron, Stromzähler), **Batterie-Logik**,
**Fixkosten** (Grundpreis, IPTV …) und **Umlagen** (Warmwasser, Wasser …). Ausgabe als PDF je Partei.

## Ablauf pro Monat

1. Rechnung des Stromanbieters (aWATTar) kommt per E-Mail → das Tool prüft das Postfach per IMAP
   (nur lesend) und liest das PDF aus: Zeitraum, Bezug kWh, Arbeitspreis netto (alle Cent/kWh-Positionen),
   Fixkosten netto (Grundpreis, Grundentgelt), Ø Börsenpreis (Position „Stromverbrauch (HOURLY)“), MwSt.
   Alternativ PDF oder `.eml` im Webinterface hochladen oder Werte manuell erfassen.
2. Plausibilitätsprüfung: Summe der Positionen = Rechnungssumme, Ø Arbeitspreis = ausgewiesener Arbeitspreis.
3. Verbrauchswerte werden aus Home Assistant geladen (Langzeitstatistik, auch Monate rückwirkend).
4. Prüfen, ggf. Werte korrigieren → **Abschließen** → PDFs je Partei bzw. alle als ZIP.
5. Versand per E-Mail an alle Parteien mit hinterlegter Adresse – automatisch beim Abschließen
   (Einstellung) oder per Knopf, mit Versandstatus je Partei.

## Berechnung

**Energiemix des Hauses** (je Abrechnungszeitraum, aus Victron + Stromzähler):

| Quelle | Menge | Preis je kWh |
|---|---|---|
| Netzstrom direkt | Netzbezug − Netz→Batterie | Ø Arbeitspreis lt. Rechnung **brutto** |
| PV-Strom direkt | Gesamtverbrauch − Netz direkt − Batterie-Entladung | Ø Börsenpreis netto + PV-Bereitstellungssatz |
| Batteriestrom aus PV | Entladung × (1 − Netzladeanteil) | Ø Börsenpreis netto + PV-Bereitstellungssatz¹ + Batterieverschleißsatz |
| Batteriestrom aus Netz (Graustrom, dyn. ESS) | Entladung × Netzladeanteil | Ø Börsenpreis netto + Batterieverschleißsatz |

Netzladeanteil = Batterie aus Netz geladen ÷ Batterie geladen gesamt. Alles außer Netzstrom ohne MwSt.
¹ abschaltbar („PV-Bereitstellungssatz auch auf Batteriestrom aus PV“).

**Je Partei**: Verbrauch (Summe der Shelly-Zähler) × Mix-Anteile × Preis. Der Eigentümer bekommt den
Restverbrauch (Gesamt − andere Parteien − Strom-Umlagen).

| Weitere Positionen | Verteilung |
|---|---|
| Fixkosten Anbieter (Grundpreis, Messstelle) | brutto ÷ Anzahl Parteien (centgenau) |
| Weitere Fixkosten (z. B. IPTV) | Betrag ÷ ausgewählte Parteien |
| Warmwasserbereitung | kWh des Shelly (HA) × Hausstrom-Mix, verteilt nach festen Prozenten; wird vom Restverbrauch des Eigentümers abgezogen |
| Trinkwasser | m³ des Wasserzählers (HA-Adapter) × Preis je m³, verteilt nach festen Prozenten |
| Sonstige Umlagen | fester Betrag; Verteilung auch nach Verbrauch je Partei (HA-Entität) oder gleichmäßig |

## Installation (Docker)

```bash
git clone https://github.com/itsh-neumeier/abrechnung_nebenkosten.git
cd abrechnung_nebenkosten
cp .env.example .env      # HA_TOKEN, APP_PASSWORD, SMTP_* und IMAP_* eintragen
docker compose up -d --build
```

Webinterface: `http://<host>:8000`. Daten (SQLite) liegen in `./data`.
Fertige Images werden per GitHub Actions nach `ghcr.io/itsh-neumeier/abrechnung_nebenkosten` gebaut
(bei Push auf `main`).

### Home-Assistant-Token

HA → Profil → Sicherheit → *Langlebige Zugriffstoken* → erstellen → in `.env` als `HA_TOKEN`.
Der Token wird nur aus der Umgebung gelesen, nie in der Datenbank oder im Repo gespeichert.

### Anforderungen an die Entitäten

Alle Zähler-Entitäten brauchen eine `state_class` `total_increasing` (oder `total`), damit HA
**Langzeitstatistiken** führt – das ist bei Shelly-Energie-, Victron- und Zählersensoren normalerweise
der Fall (gleiche Voraussetzung wie für das HA-Energie-Dashboard). Energie wird in kWh, Volumen in m³
abgefragt (HA rechnet um).

## Einrichtung im Webinterface

Alle Entitätsfelder haben einen Knopf **„Aus HA wählen“**: Er lädt die Sensoren live über die
HA-API (Suche, Filter Energie/Wasser, nur mit Langzeitstatistik, aktueller Zählerstand).

1. **Einstellungen**: Entitäten für Netzbezug, Gesamtverbrauch, Batterie entladen/geladen,
   Batterie aus Netz geladen (dyn. ESS), optional PV-Direktverbrauch; PV-Bereitstellungs- und
   Batterieverschleißsatz; Objektanschrift und Gebäude-ID; Absender/IBAN; E-Mail-Vorlage.
2. **Parteien**: Wohneinheit (Name), Wohneinheiten-ID, E-Mail, Shelly-Entitäten; eine Partei als
   *Eigentümer* markieren (bekommt den Restverbrauch).
3. **Fixkosten & Umlagen**: „IPTV-Bereitstellung“ (Betrag, Parteien), „Trinkwasser“ (Menge × Preis,
   Wasserzähler-Entität, €/m³, Prozente) und „Warmwasserbereitung“ (Strom, Shelly-Entität, Prozente).

## Entwicklung

```bash
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --reload
```

Struktur: `app/billing.py` (reine Berechnung), `app/ha.py` (HA REST/WebSocket),
`app/service.py` (DB ↔ Berechnung), `app/main.py` (Web), `app/templates/` (UI + Rechnungsvorlage).

Offene Punkte / Entscheidungen: siehe [`docs/KONZEPT.md`](docs/KONZEPT.md).
