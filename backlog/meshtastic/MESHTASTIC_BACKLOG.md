# Meshtastic / LoRa -- Backlog

Stand: Meshtastic ist als Digimode in `pluto-tx`, `pluto-advanced-rx` und `pluto-cli` integriert
(alle 9 Modem-Presets, EU433/EU868). Verifiziert gegen einen echten Heltec V3 ist nur
**LongFast auf 868 MHz** (beide Richtungen). Nicht aktiv in Arbeit:

## Verifikation
- **Andere Presets gegen echte Hardware pruefen.** Die Sync-Symbole fuer SF != 11 sind
  extrapoliert: `(16, 2**sf - 40)` (`config.meshtastic_sync_symbols`). Vorgehen: Heltec auf z.B.
  ShortFast / LongSlow umstellen, Burst mit dem RTL-SDR aufnehmen, `frame_sync` mit `sync_word=0`
  laufen lassen (gibt "netid1/netid2" aus) bzw. manuelles Dechirp wie bei LongFast; danach
  `LoraPreset.phy_verified` setzen. Nebenbei pruefen: LDRO bei SF12/BW125, CR 4/8.
- **433 MHz (Ham Mode) mit Fremdhardware** -- bisher nur Selbst-Loopback. Firmware-Verhalten bei
  `is_licensed` (unverschluesselt, Kanal-Hash ohne PSK) gegen einen echten Node pruefen.

## Funktionen
- **Direktnachrichten** an bekannte Nodes: `to` = Node-Nummer (der Codec kann es schon:
  `build_text_packet(to=...)`), GUI/CLI fehlt. Zielauswahl "Broadcast / gehoerte Node" aus der RX-Tabelle.
  Offen/komplex: (a) Kanal-PSK-Verschluesselung -- jeder auf dem Kanal kann mitlesen, Annahme durch
  neuere Firmware ungetestet; (b) PKI-DMs (Firmware >= 2.5, x25519 + AES-CCM, Kanal-Byte 0) brauchen
  eigenes Schluesselpaar + den oeffentlichen Schluessel des Ziels aus dessen NodeInfo; (c) ACK
  (`want_ack`) und Wiederholungen.
- **NodeInfo / Position** senden und empfangen (Node-Liste mit Namen in der RX-App).
- **Eigener Kanalname -> Frequenz-Slot** automatisch berechnen (Formel existiert:
  `config.meshtastic_channel_frequency_hz`), statt Frequenz von Hand.
- **MeshCore**: als Digimode umgesetzt (Adverts, Gruppentext, Direktnachrichten; siehe README). Offen: ACK/PATH/TRACE, Weiterleiten, Hashtag-Kanäle (Schlüsselableitung), weitere Presets verifizieren, Custom-Preset, echte Hardwaretests (TX-Advert an Heltec/ESP32).

## Bekannte Grenzen
- Kein Weiterleiten fremder Pakete, kein Routing, nur ein Kanal gleichzeitig.
- Presetwechsel mit anderer SF/BW baut in der TX-App die Kette neu auf.
