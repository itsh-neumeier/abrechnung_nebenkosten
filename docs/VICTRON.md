# Victron-Entitäten mit hass-victron (Modbus TCP)

Integration: <https://github.com/sfstar/hass-victron>. Alle kWh-Zähler dieser Integration haben
`state_class: total_increasing`, Home Assistant führt also Langzeitstatistiken. Die Entity-IDs enthalten
die Modbus-Unit-ID des Geräts, z. B. `sensor.victronvebus_acin1toinverter227` (VE.Bus mit Unit-ID 227).
In der Entitäten-Auswahl („Aus HA wählen“) einfach nach dem Schlüssel suchen, z. B. `acin1toinverter`.

## Zuordnung zu den Feldern unter *Einstellungen → Haus-Entitäten*

| Feld im Tool | hass-victron-Schlüssel | Bedeutung |
|---|---|---|
| **Batterie aus Netz geladen – dyn. ESS** | `vebus_acin1toinverter` | Energie AC-in 1 (Netz) → Wechselrichter = Batterieladung aus dem Netz |
| **Batterie entladen** | `vebus_invertertoacout` (+ ggf. `vebus_invertertoacin1`, siehe unten) | Wechselrichter (Batterie) → AC-out (Verbraucher) |
| **Batterie geladen gesamt** | `battery_history_chargedenergy` (SmartShunt/BMV/BMS) | gesamte Ladung der Batterie (Netz + PV) |
| Netzbezug | eigener Stromzähler in HA oder `grid_energy_forward_total` (Victron-Netzzähler) | Bezug aus dem Netz |

Der Netzladeanteil ergibt sich dann aus `vebus_acin1toinverter ÷ battery_history_chargedenergy`
(jeweils Verbrauch im Abrechnungsmonat).

## Hinweise

- **Verbraucher an AC-in** (nicht am AC-out des Multi): Batterieenergie für diese Verbraucher läuft als
  `vebus_invertertoacin1`. Dann *Batterie entladen* als Summe eintragen:
  `sensor.victronvebus_invertertoacout227 + sensor.victronvebus_invertertoacin1227`.
  Achtung: Verkauft Dynamic ESS Batteriestrom ins Netz, steckt diese Einspeisung ebenfalls in
  `invertertoacin1` und würde als Hausverbrauch gezählt.
- **AC-gekoppelte PV am AC-out** lädt die Batterie über `vebus_outtoinverter` (PV-Ladung) – sie ist in
  *Batterie geladen gesamt* bereits enthalten.
- `battery_history_chargedenergy` ist ein 16-Bit-Register (max. 6553,5 kWh) und kann überlaufen;
  Home Assistant behandelt das bei `total_increasing` wie einen Zählerreset, die Monatswerte bleiben korrekt.
- Mehrere Zähler je Feld werden im Tool mit `+` addiert.
