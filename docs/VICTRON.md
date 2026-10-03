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
| **Netzbezug (Stromzähler)** | Victron Energy Meter `grid_energy_forward_total` bzw. `victron_netzzaehler_bezug` (siehe unten) oder eigener Zähler (z. B. EasyMeter „Gesamtbezug“) | Bezug lt. Zähler, muss zur Rechnung passen |
| **Gesamtverbrauch Haus** | 3 Phasen Leistung `system_consumption_l1` / `_l2` / `_l3` (W) | Verbrauch aller AC-Lasten; das Tool integriert die Stundenmittel |
| **Batterie entladen** | `battery_history_dischargedenergy` | Energie aus der Batterie (DC-seitig) |
| **Batterie geladen gesamt** | `battery_history_chargedenergy` | gesamte Ladung (Netz + PV) |
| **Batterie aus Netz geladen – dyn. ESS** | `vebus_acin1toinverter` | Netz → Wechselrichter = Ladung aus dem Netz |
| PV-Direktverbrauch | leer lassen | wird berechnet: Gesamt − Netz direkt − Batterie entladen |

Alternative für den Gesamtverbrauch: `vebus_acin1toacout + vebus_invertertoacout` (Zähler). Die
VE.Bus-Zähler werden aber bei Neustarts von GX/MultiPlus (z. B. Firmware-Updates) zurückgesetzt. In einem
Praxistest (Jan–Okt 2026, 25 Rücksetzungen) lagen sie in Monaten mit Rücksetzungen 8–16 % unter den
Leistungssensoren, in ruhigen Monaten nur ~1,5 %. Die Leistungssensoren hatten durchgehend 100 % Abdeckung.

Netzladeanteil = `vebus_acin1toinverter ÷ battery_history_chargedenergy` (jeweils Verbrauch im Monat).

## Victron Energy Meter (VM-3P75CT o. Ä.) als Netzzähler

Der Energiezähler hinter dem EVU-Zähler zählt Bezug und Einspeisung in kWh im Gerät. hass-victron liest ihn
als „grid“-Gerät (`grid_energy_forward_total`) – aber nur, wenn seine Modbus-Unit-ID in der Scanliste liegt
(nicht gescannt werden u. a. 13–19, 47–99, 102–203) und alle Register des Blocks 2600–2644 antworten.
Fehlt er nach „Rescan available devices“, die HA-eigene Modbus-Integration nutzen:
[`ha-modbus-victron-netzzaehler.yaml`](ha-modbus-victron-netzzaehler.yaml) (Register 2634 Bezug, 2636 Einspeisung).
Danach den Monatswert einmal mit dem EVU-Zähler bzw. der Rechnung vergleichen – das Tool warnt ab 5 % Abweichung.

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
- **Netzbezug nicht aus Victron-Leistung bilden**: Es gibt per Modbus keinen Victron-Netzzähler in kWh
  (nur mit Victron-Energiezähler: `grid_energy_forward_total`). Aus der Leistung `system_grid_l1–l3` bzw.
  `vebus_activein_l1–l3` ergibt sich je Phase einzeln ein Vielfaches des echten Bezugs (Phasenausgleich),
  und selbst stündlich saldiert lag der Wert in einem Praxistest 3–34 % unter dem saldierenden Zähler,
  weil sich Bezug und Einspeisung innerhalb einer Stunde in den HA-Stundenmitteln aufheben.
  Daher: eigenen Stromzähler verwenden oder das Feld leer lassen – dann gilt der Bezug laut Rechnung.
- **Dynamic ESS verkauft ins Netz**: auch das läuft über `vebus_invertertoacin1` und ist nicht Teil des
  Hausverbrauchs.
- `battery_history_*energy` sind 16-Bit-Register (max. 6553,5 kWh) und können überlaufen;
  Home Assistant behandelt das bei `total_increasing` wie einen Zählerreset, die Monatswerte bleiben korrekt.
- Mehrere Zähler je Feld werden im Tool mit `+` addiert.
