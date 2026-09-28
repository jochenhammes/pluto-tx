# M7 — Abnahme auf der Hardware

Nach M6, gemeinsam mit dem Betreiber. Jeden Punkt abhaken oder mit
Befund notieren. **Gesendet wird nur nach ausdrücklicher Freigabe**, auf
Frequenz, Leistung und Modus, die der Betreiber nennt, mit niedriger
Leistung und nie unbeaufsichtigt.

Vorbereitung: `export PATH=~/.local/bin:$PATH` (falls nicht schon gesetzt).

## A. Übernahme der Laufzeitdaten

- [ ] `web-trx status` zeigt `läuft (PID …), Backend GnuRadioBackend`.
- [ ] Ein Browser, der vor dem Umzug angemeldet war, ist nach dem Neuladen
      **ohne neue Anmeldung** wieder drin (`sessions.json` übernommen).
- [ ] Das alte Passwort funktioniert (neues Fenster, privat).
- [ ] Das Zertifikat ist dasselbe: Der Browser fragt **nicht** erneut nach.
- [ ] Die Stationsdaten zeigen **DA2JH / JO43**.
- [ ] Der TX-Verlauf enthält die alten Einträge.
- [ ] Frequenzen, Modi, Leistung und Audio-Aufbereitung sind wie vor dem
      Umzug (`settings.json`); Geräte, die vorher verbunden waren, sind
      wieder verbunden.
- [ ] `curl -sk https://localhost:8321/health` enthält `devices`, passend
      zu den verbundenen Geräten.
- [ ] Von einem anderen Rechner: `curl -sk https://<server>:8321/health`
      enthält **kein** `devices`.
- [ ] In `web-trx log` erscheinen keine `GET /health`-Zeilen.

## B. RX über den Browser

- [ ] Wasserfall und Spektrum laufen flüssig; Zoom und Klick-zum-Tunen
      funktionieren.
- [ ] FM-Audio im Browser.
- [ ] SSB-Audio im Browser.
- [ ] FT8 auf 20 m (RTL-SDR Direct Sampling oder Pluto): Dekodierungen
      erscheinen je 15-s-Slot.
- [ ] M17 bzw. RADE, falls installiert: im Modus-Menü wählbar (Hinweis,
      dass gr-m17/rade_c gefunden werden).

## C. TX über den Browser (nur nach Freigabe)

- [ ] FM mit niedriger Leistung auf der genannten Frequenz: Das
      Kontroll-Empfangsgerät hört das Signal.
- [ ] **NOTAUS** beendet die Aussendung sofort: Pluto auf maximaler
      Dämpfung, LO aus. Prüfen mit
      `iio_attr -u ip:<pluto> -c ad9361-phy voltage0 hardwaregain`
      bzw. `altvoltage1 powerdown`.
- [ ] Kein-Bediener-Watchdog: Wird der Browser-Tab während einer
      Aussendung geschlossen, endet die Aussendung sofort. Nach ca. 10 s
      werden die Geräte getrennt.

## D. TX-App (`pluto-tx`)

- [ ] Die Zeile „Web-TRX“ zeigt den richtigen Zustand, z. B.
      `running (RX RTL-SDR, TX Pluto)`; im Tooltip stehen URL und PID.
- [ ] **Open** öffnet die Web-TRX-Seite.
- [ ] **Stop** (nach der Rückfrage): Die Zeile wechselt auf `stopped`.
      Der Pluto ist im Safe-State (`iio_attr` wie oben).
- [ ] Die App verbindet den Pluto (**Connect**). Dann **Start** →
      Dialog: Er nennt die Geräte, die Web-TRX wieder öffnet. Nach
      *Disconnect and start* wird die App-Verbindung getrennt (Safe-State),
      Web-TRX startet und übernimmt den Pluto. Web-TRX meldet keinen
      Fehler.
- [ ] **Connect** in der App, während Web-TRX den Pluto hält →
      Warnabfrage. *No* verbindet nicht.
- [ ] Mit Gerätetyp HackRF (Web-TRX hält den Pluto): **Connect** fragt
      nicht.
- [ ] App schließen und neu starten, während Web-TRX den Pluto hält: Die
      App verbindet sich **nicht** automatisch, und die Statuszeile sagt
      warum.
- [ ] App schließen (Fenster) → `web-trx status` meldet weiterhin „läuft“.
- [ ] Die App aus einem Terminal starten, Web-TRX über die App starten,
      dann **Strg-C im Terminal der App** → Die App beendet sich, der
      Server läuft weiter. Prüfen mit
      `grep SigBlk /proc/$(cat ~/Dokumente/plutosdr/web-trx/run/web-trx.pid)/status`:
      Der Wert ist `0000000000000000`.

## E. RX-App (`pluto-advanced-rx`)

- [ ] Dieselben Punkte wie in D, sinngemäß für die RX-App.
- [ ] Die RX-App empfängt auf dem **RTL-SDR**, während Web-TRX den
      **Pluto** hält. Beim Connect mit RTL-SDR gibt es keine Warnung,
      beide laufen parallel.
- [ ] Das Fensterlayout ist unverändert: Die neue Zeile sitzt in der
      Gruppe „Device“, und der Wasserfall ist so breit wie vorher.

## F. Unveränderte Teile

- [ ] `./install.sh` **nicht** nötig. Falls der Betreiber es trotzdem
      laufen lassen will: Danach sind die Starter `pluto-tx`,
      `pluto-advanced-rx` und `pluto-cli` wie vorher, anschließend wie
      gewohnt `./install-m17.sh` bzw. `./install-rade.sh` erneut
      ausführen. `web-trx` bleibt unverändert.
- [ ] `pluto-cli --help` und eine kurze `pluto-cli`-Empfangsprobe
      funktionieren wie vorher.
- [ ] Die pluto-tx-Tests laufen im Produktiv-Checkout grün (bzw. wie in
      der Baseline):
      `cd ~/Dokumente/plutosdr && QT_QPA_PLATFORM=offscreen PLUTO_WEBTRX_CONTROL=off python3 -m unittest discover tests`
- [ ] `git -C ~/Dokumente/plutosdr status` ist sauber, auch mit laufendem
      Server und migrierten Daten.

## Ergebnis

- [ ] Abnahme bestanden → M8.
- [ ] Nicht bestanden: Befund, Entscheidung (beheben / M9):
      …
