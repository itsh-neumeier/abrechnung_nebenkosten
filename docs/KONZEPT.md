# Konzept & Entscheidungen

## Getroffene Entscheidungen (Interview)

| Thema | Entscheidung |
|---|---|
| Batterie-Aufteilung | anteilig über den Abrechnungszeitraum: Batterie-Entladung ÷ Gesamtverbrauch |
| Batteriestrom-Preis | Ø Preis netto (ohne MwSt.) + Batterienutzungssatz ct/kWh |
| Übriger Strom (Netz und PV direkt) | Ø Preis brutto laut Rechnung |
| Fixkosten | gleichmäßig pro Partei (weitere Fixkosten optional nur für ausgewählte Parteien) |
| Restverbrauch (Allgemeinstrom usw.) | an Eigentümer/Hauptpartei |
| Ausgabe | PDF je Partei (+ ZIP), Web-Vorschau |
| Betrieb | Docker, Webinterface, SQLite-Volume |
| HA-Daten | Langzeitstatistik per WebSocket (`recorder/statistics_during_period`), nicht History (10-Tage-Purge) |

## Offene Fragen

1. **PV-Direktverbrauch**: Strom aus PV ohne Umweg über die Batterie wird derzeit wie Netzstrom
   (brutto) berechnet. Das ergibt eine positive „Differenz zur Rechnung“. Gewünscht oder eigener Satz?
2. **Batterieladung aus dem Netz**: Lädt die Batterie auch aus dem Netz (z. B. dynamischer Tarif)?
   Dann wäre ein Teil des Batteriestroms bereits über die Rechnung bezahlt.
3. **Einspeisung**: Soll die Einspeisevergütung irgendwo auftauchen? Derzeit nicht.
4. **Mehrere Tarifzeiträume/Preisänderung** in einer Rechnung: aktuell Mittelwert über die Rechnung.
5. **Versand**: PDFs automatisch per E-Mail an die Parteien schicken?
6. **Automatik**: Monatlich automatisch einen Entwurf anlegen (z. B. am 1.) oder Rechnung per
   E-Mail/PDF-Upload auslesen?
7. **Rechnungslayout**: Logo, Kopfdaten, Hinweistexte (Kleinunternehmer/keine USt-Ausweisung)?
