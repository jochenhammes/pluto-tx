# JS8 in Web-TRX

JS8 ist der Tastatur-Chat-Modus von JS8Call. Er nutzt dieselbe Technik wie
FT8, hat aber freie Texte über mehrere Rahmen, gerichtete Nachrichten und
vier Geschwindigkeiten.

Web-TRX benutzt dafür den JS8-Code von pluto-tx. Das Protokoll ist aus dem
gepinnten JS8Call-Quelltext portiert und bitgenau gegen ihn geprüft (pluto-tx
`docs/js8/SPEC.md`). Gesamtplan: pluto-tx `docs/JS8_PLAN.md`.

## Empfang

- **Einstellungen:** Modus „JS8 (JS8Call)“, Speed (Normal 15 s, Fast 10 s,
  Turbo 6 s, Slow 30 s oder alle vier) und Decoder:
  - `js8` ist JS8Calls eigener Decoder (`tools/js8ref`, gebaut von
    `install-js8.sh`); er ist die Voreinstellung, wenn vorhanden.
  - `eigener` ist der numpy-Decoder von pluto-tx.
- **Auswertung:** Jede Periode wird nach ihrem Ende dekodiert, jede
  Geschwindigkeit an ihren eigenen UTC-Grenzen. Der Empfänger arbeitet in USB
  auf der Dial-Frequenz (7,078 / 14,078 / 144,178 MHz …) und wertet
  200–3000 Hz Audio aus.
- **Panel „Empfang“:**
  - *Chat:* Nachrichten nach Gegenstation und @ALLCALL. Eine noch offene
    Nachricht zeigt „▸“, fehlende Rahmen erscheinen als „…“.
  - *Band:* der letzte Rahmen je Offset.
  - *Stationen:* Rufzeichen, Grid, SNR, Offset, zuletzt gehört.
  - Ein Klick auf eine Station oder Nachricht setzt „An“ im Sender.
- **Mehrteilige Nachrichten:** Rahmen gehören zusammen, wenn sie aus
  aufeinanderfolgenden Perioden derselben Geschwindigkeit stammen und in der
  Frequenz nahe beieinander liegen. Die Toleranz ist die von JS8Call:
  10/16/32/10 Hz.

## Senden

- **Nachrichtenarten:**
  - CQ,
  - Heartbeat,
  - @ALLCALL + Text,
  - Text an eine Station,
  - `SNR?`,
  - SNR-Rapport,
  - ACK,
  - Freitext.

  Der Server baut die Nachricht aus Station und Feldern genau so wie
  JS8Call. Die Vorschau zeigt, was Empfänger sehen, die Zahl der Rahmen und
  die Dauer.
- **Ablauf:**
  - „Nachricht senden“ schaltet die ganze Nachricht scharf.
  - Die Rahmen gehen in aufeinanderfolgenden Perioden raus.
  - **Jeder Rahmen wird einzeln getastet**, dazwischen ist der Sender aus:
    Pluto mit LO aus und maximaler Dämpfung.
  - Der erste Rahmen wird 3 s vor seiner Periode getastet, jeder weitere
    0,5 s vorher.
- **Abbruch:** Ein zweiter Klick, NOTAUS, Trennen, ein Moduswechsel oder
  „kein Browser mehr da“ brechen den Rest ab, auch mitten in einem Rahmen.
  Aus einer abgebrochenen Nachricht wird nie wieder gesendet.
- **Prüfungen vor jeder HF-Aktion:**
  - Das ganze Signal (Dial + Offset … + 8 Töne) liegt in *einem*
    Amateurband.
  - Die Nachricht ist höchstens 20 Rahmen lang.
- **Keine automatischen Antworten:** kein Heartbeat-Automat, kein Relay,
  keine Inbox. Das wäre eine eigene Entscheidung des Betreibers
  (Plan-Phase J9).
- **Freitext in Fast, Turbo und Slow** braucht das JSC-Wörterbuch aus
  `install-js8.sh`. Ohne das Wörterbuch nur Normal verwenden.
- **TX-Log:** Jeder Rahmen erscheint mit dem Nachrichtentext und seiner
  Nummer.

## Aktueller Stand

- Software fertig (Phase J6): Backend-, Sim- und Browser-Smoke-Tests.
- Funkbetrieb: Phase J7 in pluto-tx `docs/JS8_PLAN.md`. Das Protokoll aller
  Aussendungen steht in pluto-tx `docs/js8/TESTS.md`.
