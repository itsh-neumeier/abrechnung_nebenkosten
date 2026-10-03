# ⚡ Stromabrechnung Hausparteien

Web-Tool zur monatlichen Weiterberechnung der Stromrechnung an die Parteien eines Hauses –
mit Messwerten aus **Home Assistant** (Shelly, Victron, Stromzähler), **Batterie-Logik**,
**Fixkosten** (Grundpreis, IPTV …) und **Umlagen** (Warmwasser, Wasser …). Ausgabe als PDF je Partei.

## Ablauf pro Monat

1. Rechnung vom Stromanbieter kommt → im Webinterface **„Neue Stromrechnung erfassen“**.
2. Zeitraum, Netzbezug (kWh), Arbeitspreis netto, Fixkosten netto, MwSt. eintragen.
3. Verbrauchswerte werden aus Home Assistant geladen (Langzeitstatistik, auch Monate rückwirkend).
4. Prüfen, ggf. Werte korrigieren → **Abschließen** → PDFs je Partei bzw. alle als ZIP.

## Berechnung

| Schritt | Formel |
|---|---|
| Ø Preis netto | Arbeitspreis netto ÷ Netzbezug kWh (laut Rechnung) |
| Ø Preis brutto | Ø Preis netto × (1 + MwSt.) |
| Batterieanteil | Batterie-Entladung ÷ Gesamtverbrauch Haus (beides Victron, im Zeitraum) |
| Verbrauch Partei | Summe ihrer Shelly-Zähler |
| Verbrauch Eigentümer | Gesamtverbrauch − alle anderen Parteien − Strom-Umlagen (Restverbrauch) |
| Netzstrom | Verbrauch × (1 − Batterieanteil) × Ø Preis **brutto** |
| Batteriestrom | Verbrauch × Batterieanteil × (Ø Preis **netto** + Batterienutzungssatz), ohne MwSt. |
| Fixkosten Anbieter | Fixkosten netto × (1 + MwSt.) ÷ Anzahl Parteien (centgenau) |
| Weitere Fixkosten | z. B. IPTV: Betrag ÷ ausgewählte Parteien |
| Umlagen | Energie (kWh aus HA, Preis wie Hausstrom) oder Betrag (€), verteilt nach Verbrauch je Partei (HA-Entität, z. B. Warmwasser-m³), festen Prozenten oder gleichmäßig |

Strom-Umlagen (z. B. Warmwasserbereitung über einen eigenen Shelly) werden vom Restverbrauch
des Eigentümers abgezogen, damit nichts doppelt berechnet wird.

## Installation (Docker)

```bash
git clone https://github.com/itsh-neumeier/abrechnung_nebenkosten.git
cd abrechnung_nebenkosten
cp .env.example .env      # HA_TOKEN und APP_PASSWORD eintragen
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

1. **Einstellungen**: Entitäten für Netzbezug (Zähler), Gesamtverbrauch (Victron), Batterie-Entladung
   (Victron); Batterienutzungssatz; Absenderdaten/IBAN für die PDFs.
2. **Parteien**: je Partei Name, Anschrift und Shelly-Entitäten; eine Partei als *Eigentümer* markieren
   (bekommt den Restverbrauch).
3. **Fixkosten & Umlagen**: z. B. „IPTV-Bereitstellung“ (Betrag, Parteien) und „Warmwasserbereitung“
   (Quell-Entität kWh, Verteilung nach Warmwasserzählern je Partei).

## Entwicklung

```bash
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --reload
```

Struktur: `app/billing.py` (reine Berechnung), `app/ha.py` (HA REST/WebSocket),
`app/service.py` (DB ↔ Berechnung), `app/main.py` (Web), `app/templates/` (UI + Rechnungsvorlage).

Offene Punkte / Entscheidungen: siehe [`docs/KONZEPT.md`](docs/KONZEPT.md).
