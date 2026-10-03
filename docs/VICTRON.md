# Victron-Entitäten mit hass-victron (Modbus TCP)

Nur die **lokale** Integration hass-victron verwenden – keine Sensoren der Cloud-Integration
„Victron Remote Monitoring“ (`sensor.victron_remote_monitoring_*`, das sind VRM-Prognosen/-Werte).

Integration: <https://github.com/sfstar/hass-victron>. Alle kWh-Zähler dieser Integration haben
`state_class: total_increasing`, Home Assistant führt also Langzeitstatistiken. Die Entity-IDs enthalten
die Modbus-Unit-ID des Geräts, z. B. `sensor.victronvebus_acin1toinverter227` (VE.Bus mit Unit-ID 227).
In der Entitäten-Auswahl („Aus HA wählen“) einfach nach dem Schlüssel suchen, z. B. `acin1toinverter`.

## Zuordnung zu den Feldern unter *Einstellungen → Haus-Entitäten*

Aufbau: **alle Verbraucher am AC-out des MultiPlus-II** (auch 3-phasig), Netz an AC-in 1, PV über
MPPT-Laderegler (DC). Je nach hass-victron-Version heißen die Sensoren z. B.
`sensor.victron_vebus_acin1toinverter_229` oder `sensor.victronvebus_acin1toinverter229` (229 = Unit-ID).

| Feld im Tool | Sensor | Bedeutung |
|---|---|---|
| **Netzbezug (Stromzähler)** | eigener Zähler (z. B. EasyMeter „Gesamtbezug“) | Bezug lt. Zähler, muss zur Rechnung passen |
| **Gesamtverbrauch Haus** | `vebus_acin1toacout + vebus_invertertoacout` | alles, was am AC-out ankommt = Hausverbrauch |
| **Batterie entladen** | `battery_history_dischargedenergy` | Energie aus der Batterie (DC-seitig) |
| **Batterie geladen gesamt** | `battery_history_chargedenergy` | gesamte Ladung (Netz + PV) |
| **Batterie aus Netz geladen – dyn. ESS** | `vebus_acin1toinverter` | Netz → Wechselrichter = Ladung aus dem Netz |
| PV-Direktverbrauch | leer lassen | wird berechnet: Gesamt − Netz direkt − Batterie entladen |

Alternative für den Gesamtverbrauch: Leistung je Phase `system_consumption_l1/_l2/_l3` (W) als
„3 Phasen“ – das Tool integriert die stündlichen Mittelwerte. Zähler sind aber robuster.

Netzladeanteil = `vebus_acin1toinverter ÷ battery_history_chargedenergy` (jeweils Verbrauch im Monat).

## Hinweise

- **Warum nicht `vebus_invertertoacout` für „Batterie entladen“?** Bei PV über MPPT-Laderegler (DC)
  fließt auch der PV-Direktstrom durch den Wechselrichter zum AC-out – der Zähler enthält dann Batterie
  *und* PV. Der Batteriezähler (DC) misst nur die Batterie und passt für DC- wie AC-gekoppelte PV.
- **Verluste**: `vebus_acin1toinverter` wird AC-seitig gemessen, die Batterieladung DC-seitig. Der
  Netzladeanteil fällt dadurch um die Ladeverluste (ca. 5–10 %) etwas höher aus – also eher zugunsten
  des Graustrom-Anteils.
- **Phasenausgleich / saldierender Zähler**: Speist der MultiPlus auf einer Phase ein, während andere
  Phasen beziehen, zählt `vebus_invertertoacin1` diese Einspeisung (kann hunderte kWh im Monat sein)
  und `vebus_acin1toacout` den Bezug der anderen Phasen. Ein saldierender Zähler verrechnet beides –
  für die Abrechnung zählt nur der saldierte Bezug des Stromzählers. `invertertoacin1` wird nicht verwendet.
- **Dynamic ESS verkauft ins Netz**: auch das läuft über `vebus_invertertoacin1` und ist nicht Teil des
  Hausverbrauchs.
- `battery_history_*energy` sind 16-Bit-Register (max. 6553,5 kWh) und können überlaufen;
  Home Assistant behandelt das bei `total_increasing` wie einen Zählerreset, die Monatswerte bleiben korrekt.
- Mehrere Zähler je Feld werden im Tool mit `+` addiert.
