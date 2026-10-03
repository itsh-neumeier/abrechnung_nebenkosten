# Victron-Entitäten mit hass-victron (Modbus TCP)

Integration: <https://github.com/sfstar/hass-victron>. Alle kWh-Zähler dieser Integration haben
`state_class: total_increasing`, Home Assistant führt also Langzeitstatistiken. Die Entity-IDs enthalten
die Modbus-Unit-ID des Geräts, z. B. `sensor.victronvebus_acin1toinverter227` (VE.Bus mit Unit-ID 227).
In der Entitäten-Auswahl („Aus HA wählen“) einfach nach dem Schlüssel suchen, z. B. `acin1toinverter`.

## Zuordnung zu den Feldern unter *Einstellungen → Haus-Entitäten*

Aufbau hier: **alle Verbraucher am AC-out des MultiPlus-II**, Netz an AC-in 1.

| Feld im Tool | Sensor | Bedeutung |
|---|---|---|
| **Netzbezug (Stromzähler)** | eigener Zähler in HA (Kontrolle gegen die Rechnung) | Bezug aus dem Netz |
| **Gesamtverbrauch Haus** | Victron-Verbrauchszähler hinter dem Stromzähler; alternativ `vebus_acin1toacout + vebus_invertertoacout` (bei PV über MPPT/DC) | gesamter Verbrauch am AC-out |
| **Batterie entladen** | `battery_history_dischargedenergy` (SmartShunt/BMV/BMS) | Energie aus der Batterie (DC-seitig) |
| **Batterie geladen gesamt** | `battery_history_chargedenergy` (SmartShunt/BMV/BMS) | gesamte Ladung der Batterie (Netz + PV) |
| **Batterie aus Netz geladen – dyn. ESS** | `vebus_acin1toinverter` | Energie AC-in 1 (Netz) → Wechselrichter = Batterieladung aus dem Netz |
| PV-Direktverbrauch | leer lassen | wird berechnet: Gesamt − Netz direkt − Batterie entladen |

Netzladeanteil = `vebus_acin1toinverter ÷ battery_history_chargedenergy` (jeweils Verbrauch im Monat).

## Hinweise

- **Warum nicht `vebus_invertertoacout` für „Batterie entladen“?** Bei PV über MPPT-Laderegler (DC)
  fließt auch der PV-Direktstrom durch den Wechselrichter zum AC-out – der Zähler enthält dann Batterie
  *und* PV. Der Batteriezähler (DC) misst nur die Batterie und passt für DC- wie AC-gekoppelte PV.
- **Verluste**: `vebus_acin1toinverter` wird AC-seitig gemessen, die Batterieladung DC-seitig. Der
  Netzladeanteil fällt dadurch um die Ladeverluste (ca. 5–10 %) etwas höher aus – also eher zugunsten
  des Graustrom-Anteils.
- **Dynamic ESS verkauft ins Netz**: Rückspeisung läuft als `vebus_invertertoacin1` und ist hier nicht
  Teil des Hausverbrauchs – kein Handlungsbedarf, solange alle Verbraucher am AC-out hängen.
- `battery_history_*energy` sind 16-Bit-Register (max. 6553,5 kWh) und können überlaufen;
  Home Assistant behandelt das bei `total_increasing` wie einen Zählerreset, die Monatswerte bleiben korrekt.
- Mehrere Zähler je Feld werden im Tool mit `+` addiert.
