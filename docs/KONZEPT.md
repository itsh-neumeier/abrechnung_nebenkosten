# Konzept & Entscheidungen

## Getroffene Entscheidungen (Interview)

| Thema | Entscheidung |
|---|---|
| Energiemix | anteilig über den Abrechnungszeitraum: Netz direkt, PV direkt, Batterie aus PV, Batterie aus Netz |
| Netzstrom | Ø Arbeitspreis lt. Rechnung brutto |
| PV-Direktstrom | Ø Börsenpreis netto lt. Rechnung + PV-Bereitstellungssatz, ohne MwSt. |
| Graustrom (Batterie aus Netz, dyn. ESS) | Ø Börsenpreis netto + Batterieverschleißsatz, ohne MwSt. |
| Batteriestrom aus PV | Ø Börsenpreis netto + PV-Bereitstellungssatz (abschaltbar) + Batterieverschleißsatz |
| E-Mail-Versand | SMTP, Adresse je Partei, automatisch beim Abschließen oder manuell |
| Layout | ohne Logo: „Nebenkostenabrechnung“, Objektanschrift, Wohneinheit, Gebäude-ID + Wohneinheiten-ID |
| Entitäten | Auswahl live über die HA-API im Webinterface |
| Rechnungseingang | aWATTar-PDF aus E-Mail (IMAP, nur lesend) oder Upload; Positionen in Cent/kWh → Arbeitspreis, Euro/Monat/Jahr → Fixkosten, „HOURLY“ → Börsenpreis |
| Trinkwasser | m³ aus HA × Preis je m³, nach festen Prozenten |
| Warmwasserbereitung | kWh des Shelly × Hausstrom-Mix, nach festen Prozenten |
| Fixkosten | gleichmäßig pro Partei (weitere Fixkosten optional nur für ausgewählte Parteien) |
| Restverbrauch (Allgemeinstrom usw.) | an Eigentümer/Hauptpartei |
| Ausgabe | PDF je Partei (+ ZIP), Web-Vorschau, E-Mail |
| Betrieb | Docker, Webinterface, SQLite-Volume |
| HA-Daten | Langzeitstatistik per WebSocket (`recorder/statistics_during_period`), nicht History (10-Tage-Purge) |

## Offene Fragen

1. **Batteriestrom aus PV**: Bekommt er zusätzlich den PV-Bereitstellungssatz (Standard) oder nur
   Börsenpreis + Verschleiß? → Schalter in den Einstellungen / je Abrechnung.
2. **Speicherstand über den Monatswechsel**: Der Netzladeanteil wird aus den Ladungen *im Monat*
   bestimmt; Energie, die Ende des Vormonats geladen und im neuen Monat entladen wird, wird nicht
   gesondert verfolgt (stündliche Berechnung wäre möglich).
3. **Einspeisung**: Einspeisevergütung wird nicht berücksichtigt.
4. **Automatisch abschließen**: Importierte Rechnungen ohne Hinweise können direkt abgeschlossen und
   versendet werden (Einstellung, standardmäßig aus) – oder lieber immer erst prüfen?
5. **Wasserpreis**: Preis je m³ (Wasser + Abwasser) bleibt fest hinterlegt und ist je Abrechnung änderbar.
   Soll die Wasserrechnung (jährlich) später ebenfalls importiert werden?
