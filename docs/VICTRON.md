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

## Netzbezug: Rechnung maßgeblich, Zähler zur Kontrolle

Gerechnet wird immer mit dem Netzbezug **laut Rechnung** – der amtliche Zähler ist maßgeblich, auch bei
einem Zählerwechsel im Monat. Das Feld „Netzbezug“ dient nur der Kontrolle (Warnung ab 5 % Abweichung).
Empfehlung, solange der neue Zähler nicht ausgelesen werden kann: `victron:grid_import` (Energiezähler über
den eigenen Victron-Logger). Für Monate vor dem Zählerwechsel liegen die Werte des alten Zählers weiter in
der HA-Statistik.

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

## Victron direkt – eigener Logger ohne Home Assistant

Zusätzlich (oder alternativ) zu Home Assistant kann das Tool den GX selbst per **Modbus TCP** lesen
(*Einstellungen → Victron direkt*). Der Client kann ausschließlich lesen (Funktionscode 3).

- **Der GX speichert selbst keine abfragbare Historie** – nur einen Sendepuffer für VRM (intern ca. 48 h,
  mit microSD/USB länger). Daten von „Victron direkt“ gibt es daher erst ab dem Start des Loggers;
  für vergangene Monate bleibt Home Assistant die Quelle.
- **Zähler** (jede Minute): Energiezähler Bezug/Einspeisung (Register 2634/2636), Batteriewächter
  geladen/entladen (302/301, 16-Bit-Überlauf wird erkannt), VE.Bus 74–93. Differenzen werden auch über
  Ausfälle des Containers hinweg verbucht, Rücksetzungen erkannt.
- **Leistungen** (alle 10 s, selbst integriert): Verbrauch L1–L3 (817–819), Netz L1–L3 (820–822, je Abtastung
  über die Phasen saldiert), Batterieleistung (842), PV DC (850).
- Gespeichert werden 15-Minuten-Blöcke. In allen Entitätsfeldern wählbar als `victron:<schlüssel>`, z. B.
  `victron:grid_import`, `victron:consumption`, `victron:battery_charged`, `victron:vebus_acin1toinverter`.
- Jede Abrechnung zeigt eine **Gegenüberstellung** der verwendeten Werte (z. B. aus HA) mit dem Logger.
- Geräte werden automatisch gesucht („Verbindung testen & Geräte suchen“). Der Suchlauf fragt auch Unit-IDs
  ohne Gerät ab – dadurch kann im GX unter Modbus TCP ein harmloser Fehler „Error finding service …“ stehen.
- Netzwerk: Der Container braucht Zugriff auf den GX, Port 502/TCP (Firewall-Regel nur vom Docker-Host).

## VRM (Victron-Cloud) – optionales drittes Standbein

Mit `VRM_TOKEN` (VRM → Einstellungen → Integrationen → Zugriffstokens) und der Anlagen-ID liefert die VRM-API
für beliebige vergangene Zeiträume die Energieflüsse: Gc/Gb (Netz → Verbraucher/Batterie), Pc/Pb/Pg (PV → …),
Bc/Bg (Batterie → …). Wählbar als `vrm:<code>` bzw. zusammengesetzt `vrm:grid_import`, `vrm:consumption`,
`vrm:battery_charged`, `vrm:battery_discharged`, `vrm:pv`, `vrm:grid_export`; zusätzlich im Abgleich.

Praxisvergleich (Aug/Sep 2026) mit den lokalen Werten:

| | lokal | VRM |
|---|---|---|
| Netzbezug | EasyMeter 74,8 / 29,3 kWh | 79,4 / 30,8 kWh (+5–6 %, saldiert) |
| Netz → Batterie | VE.Bus 3,98 / 6,5 kWh | 4,0 / 6,5 kWh (identisch) |
| Batterie entladen | Batteriewächter (DC) 832,6 / 688,5 kWh | Bc + Bg (AC) 685,0 / 549,7 kWh |

Der DC-Zähler des Batteriewächters enthält die Wandlungsverluste des Wechselrichters; VRM zählt, was beim
Verbraucher ankommt. Mit dem DC-Wert fällt der Batterieanteil im Mix höher und PV direkt niedriger aus
(August: Ø 23,36 statt 22,68 ct/kWh für einen Mieter).

### Entscheidung: Energiemix aus VRM

Für den Energiemix werden die VRM-Werte verwendet (Knopf „VRM-Werte für den Energiemix übernehmen“):

| Feld | Wert |
|---|---|
| Gesamtverbrauch | `vrm:consumption` (Gc + Pc + Bc) |
| Batterie entladen | `vrm:Bc` – Batterie → Verbraucher, AC-seitig; **ohne Bg** (Verkauf ins Netz) |
| Batterie geladen gesamt | `vrm:battery_charged` (Gb + Pb) |
| Netz → Batterie | `vrm:Gb` (identisch mit VE.Bus `acin1toinverter`) |
| Netzbezug | nur Kontrolle – gerechnet wird mit dem Bezug laut Rechnung |

Alle Mix-Felder aus einer Quelle halten die Bilanz konsistent; der berechnete PV-Direktanteil entspricht dann
VRM `Pc`. Ergebnis mit echten Daten: August Ø 22,68 statt 23,30 ct/kWh, September Ø 22,52 statt 23,33 ct/kWh
(Mieter mit 245,8 kWh) gegenüber dem lokalen DC-Batteriezähler.
