# pluto-cli

A headless command-line interface to `pluto_tx` and `pluto_advanced_rx` --
built for scripting, automation, and integrating this project's TX/RX
capabilities into other workflows (cron-style unattended jobs, other
scripts, CI-style smoke tests, headless boxes with no display at all).

## 1. Overview & philosophy

`pluto_cli` is a **separate top-level package** that **imports and reuses**
`pluto_tx`'s and `pluto_advanced_rx`'s existing flowgraph/device/audio code
directly -- it does not fork, copy, or reimplement any of it. Every TX mode
here is `pluto_tx.flowgraph.PlutoTxFlowgraph` under the hood; every RX mode
is `pluto_advanced_rx.flowgraph.AdvancedRxFlowgraph`. If a bug is fixed or a
mode gains a feature in either GUI app, `pluto-cli` picks it up automatically
-- there is nothing mode-specific to keep in sync by hand.

**Process isolation, not shared runtime state.** `pluto-cli` runs as its own
plain Python process, entirely separate from the `pluto-tx`/`pluto-advanced-rx`
GUI apps. Launching a CLI command never reaches into, disturbs, or shares any
state with a running GUI instance. This does **not** mean you can run two
things against the same radio at once: a Pluto/HackRF/RTL-SDR device can only
be opened by one process at a time (a `libiio`/USB exclusivity fact, not a
`pluto_cli` design choice) -- if the GUI app has a device open, `pluto-cli`
commands targeting that same device will fail to connect, and vice versa.
Two different physical devices (e.g. the GUI driving a Pluto for TX while
`pluto-cli` drives an RTL-SDR for RX), or a `soundcard`/`audio` backend
alongside a GUI using real RF hardware, work fine simultaneously.

**A note on Qt.** `pluto_tx.flowgraph`/`pluto_advanced_rx.flowgraph`
themselves have zero direct PyQt5 imports. However, importing them
transitively imports `gnuradio.qtgui` (used only when a GUI app passes
`enable_waterfall=True`, which `pluto_cli` never does), and `gnuradio.qtgui`'s
own Python bindings load the full PyQt5 module tree as a side effect of that
import. In practice this is harmless: **no `QApplication` is ever constructed**
and no event loop, display-server connection, or window is ever created --
verified with `QtWidgets.QApplication.instance() is None` after importing
any `pluto_cli` module. `pluto-cli` is fully usable over SSH, in a cron job,
or on a machine with no display server at all. The precise claim is "no Qt
runtime is ever activated," not "PyQt5 is never imported."

**Output philosophy.** Every command supports `--json`, which switches from
human-readable text to one JSON object per line on stdout -- a documented,
stable event schema (see [section 6](#6-json-output-schema)) rather than
something a downstream script has to scrape by regex. This is what actually
makes `pluto-cli` easy to plug into another workflow.

## 2. Installation

`pluto-cli` is installed by the same `install.sh` that sets up `pluto-tx`/
`pluto-advanced-rx` -- there is no separate install step:

```bash
./install.sh
```

This creates a `pluto-cli` launcher in `~/.local/bin` alongside `pluto-tx`
and `pluto-advanced-rx` (add `~/.local/bin` to your `PATH` if `install.sh`
tells you to).

If you also want the optional M17 and/or RADE digital-voice modes, run
`install-m17.sh` and/or `install-rade.sh` same as for the GUI apps -- both
scripts now also regenerate the `pluto-cli` launcher with the right
`LD_LIBRARY_PATH` so `gnuradio.m17`/`librade.so` are found. Without those,
`pluto-cli tx m17`/`rx m17`/`tx rade`/`rx rade` print a clear error (not a
traceback) and exit with status 1 -- the same grey-out condition the GUI
apps show as a disabled combo entry, in text form.

Without a launcher on `PATH`, run everything as a module from the repo root
instead: `python3 -m pluto_cli.app ...`.

## 3. Quick start

```bash
# What hardware is connected right now?
pluto-cli devices scan --tx --device pluto
pluto-cli devices scan --rx --device rtlsdr

# Transmit 3 seconds of FM voice from the default mic, at -20 dB (Pluto's
# safe default power ceiling), asking for confirmation before keying:
pluto-cli tx fm --freq 432150000

# Same, but skip the confirmation prompt and get machine-readable output:
pluto-cli tx fm --freq 432150000 --yes --json

# Receive FM at the same frequency to the default speaker, for 10 seconds:
pluto-cli rx fm --freq 432150000 --duration 10
```

Every level of the command tree documents itself -- `pluto-cli --help`,
`pluto-cli tx --help`, `pluto-cli tx m17 --help`, etc. always reflect the
current, authoritative set of flags; the tables below are a fast reference,
not the source of truth.

## 4. Full command reference

### `pluto-cli tx <mode> [common flags] [mode flags]`

Common flags (every mode):

| flag | meaning |
|---|---|
| `--device {pluto,hackrf,soundcard}` | TX hardware backend (default: `pluto`) |
| `--uri URI` | Connection string (libiio URI for pluto, serial for hackrf; ignored for soundcard) |
| `--freq HZ` | Transmit frequency in Hz |
| `--power-ceiling DB` | Max TX power/attenuation (device-specific meaning); omit for the device's own safe default |
| `--power DB` | Target power within `--power-ceiling` (default: equal to the ceiling) |
| `--source {mic,file}` | Audio source for voice/analog modes (default: `mic`); ignored for digitext/psk31/rtty/filebroadcast |
| `--wav-file PATH` | WAV file, when `--source file` |
| `--audio-device STR` | Mic device string, from `pluto-cli devices list-audio-inputs` |
| `--duration SECONDS` | Seconds to stay keyed (default: 3.0); ignored for digitext/psk31/rtty (see below) and `--interactive` |
| `--interactive` | Enter to key, Enter again to unkey, repeatedly; Ctrl-C to quit |
| `--yes` | Skip the `Type YES to key up` confirmation prompt |
| `--json` | One JSON object per line instead of text |

| mode | extra flags | notes |
|---|---|---|
| `fm`, `ssb` | -- | analog voice |
| `m17` | `--src-callsign`, `--dst-callsign` | requires `install-m17.sh`; holds keyed briefly after unkey to send the EOT frame |
| `freedv` | `--variant {2020,2020b}`, `--callsign` | digital voice |
| `rade` | `--eoo` | requires `install-rade.sh`; `--eoo` sends an End-Of-Over marker after unkey and waits for it to finish |
| `digitext` | `--text` (required), `--layout {horizontal,vertical}`, `--zoom`, `--min-freq-hz` | waterfall-drawn text; `--duration` is ignored -- the transmission runs exactly once for however long the rendered text takes |
| `psk31` | `--text` (required), `--tone-hz` | BPSK31 chat; `--duration` ignored, same reason as digitext |
| `rtty` | `--text` (required), `--mark-hz`, `--shift-hz`, `--baud-rate`, `--reverse` | 2-tone FSK Baudot; `--duration` ignored, same reason as digitext |
| `meshtastic` | `--text` (required), `--region {eu433,eu868}`, `--modem-preset NAME`, `--callsign`, `--node-id HEX`, `--channel`, `--psk`, `--hop-limit` | One Meshtastic (LoRa) broadcast frame; `--freq`/`--duration` ignored (carrier from the preset, hold time from the frame's airtime). carrier and default channel name follow the preset (firmware slot formula); `eu433` = Ham Mode (callsign required, unencrypted); `eu868` = ISM/SRD, 10 % duty cycle enforced. Requires gr-lora_sdr, see `install-lora.sh` |
| `filebroadcast` | `--file PATH` (repeatable, >=1 required) | round-robin file broadcast; each `--file` is read from disk once at startup |
| `baseband` | `--deviation-hz` | raw wideband FM passthrough, no audio processing -- see [Recipes](#7-recipes) |

Example:

```bash
pluto-cli tx m17 --freq 432150000 --src-callsign DA2JH --dst-callsign @ALL --power-ceiling -30 --yes
```

### `pluto-cli rx <mode> [common flags] [mode flags]`

Common flags (every mode):

| flag | meaning |
|---|---|
| `--device {pluto,hackrf,rtlsdr,audio}` | RX hardware backend (default: `pluto`) |
| `--uri URI` | Connection string; ignored for `audio` |
| `--freq HZ` | Receive frequency in Hz |
| `--bandwidth HZ` | One of `1000000/2500000/5000000/8000000/10000000` -- device sample rate ("zoom" span); omit for the device default |
| `--gain-mode {manual,slow_attack,fast_attack,hybrid}` | AGC mode |
| `--gain DB` | Manual gain, used when `--gain-mode manual` |
| `--audio-out STR` | Output device string, from `pluto-cli devices list-audio-outputs`; empty = system default |
| `--duration SECONDS` | Omit to run until Ctrl-C |
| `--digimode {psk31,rtty,meshtastic}` | Also decode this digimode in parallel and print characters as they arrive (see below). Omit to disable digimode decoding entirely |
| `--psk31-tone-hz HZ` | PSK31 tone-filter center frequency, used with `--digimode psk31` |
| `--rtty-mark-hz HZ` | RTTY mark tone frequency, used with `--digimode rtty` |
| `--rtty-shift-hz HZ` | RTTY mark/space shift, used with `--digimode rtty` (presets: 170/425/850) |
| `--rtty-baud-rate BAUD` | RTTY baud rate, used with `--digimode rtty` (presets: 45.45/50/75/100) |
| `--rtty-reverse` | Swap which tone is Mark vs. Space, used with `--digimode rtty` |
| `--meshtastic-region {eu433,eu868}` / `--meshtastic-modem-preset NAME` | Region (default `eu868`) and Meshtastic modem preset (LongFast, LongModerate, LongSlow, MediumFast, MediumSlow, ShortFast, ShortSlow, ShortTurbo, LongTurbo; default LongFast, the only one verified against real hardware) for `--digimode meshtastic`; retunes to the preset's carrier, `--freq` is ignored; RX `--bandwidth` must be at least twice the preset's LoRa bandwidth. Requires gr-lora_sdr, see `install-lora.sh` |
| `--meshtastic-channel NAME` / `--meshtastic-psk B64` | Channel name (default `LongFast`) and key (default `AQ==` = stock channel; empty = unencrypted) used to read frames |
| `--filebroadcast-save-dir DIR` | Also watch for File Broadcast files in parallel and save each as soon as it's complete (see below) |
| `--json` | One JSON object per line instead of text |

**File Broadcast is an always-on parallel branch** inside
`AdvancedRxFlowgraph`, independent of the primary demod mode -- this mirrors
the real flowgraph exactly, not a `pluto_cli` simplification. That means
`pluto-cli rx m17 --filebroadcast-save-dir ./received` decodes M17 audio to
the chosen output *and* saves any File Broadcast files seen in the same
band, at the same time.

**PSK31/RTTY are different: only ONE digimode can ever be active at a
time**, selected via `--digimode`. This is a real GNU Radio limitation, not
a `pluto_cli`-specific restriction: a block with a required input can't be
left disconnected while a flowgraph is running, so switching which digimode
decodes means rebuilding the whole flowgraph -- the GUI apps hit the exact
same constraint (see `pluto_advanced_rx/flowgraph.py`'s own comments) and
handle it by rebuilding whenever the Digimodes tab/selection changes.
`pluto-cli` sidesteps the "switching" case entirely by fixing the digimode
for the whole process lifetime via `--digimode` at startup.

| mode | extra flags | notes |
|---|---|---|
| `fm`, `ssb`, `baseband` | `--width-hz` | demod filter width |
| `rade` | -- | requires `install-rade.sh` |
| `m17` | -- | requires `install-m17.sh`; each decoded frame is emitted as an `m17_fields` event |

Example:

```bash
pluto-cli rx m17 --freq 432150000 --filebroadcast-save-dir ./received --json
```

### `pluto-cli devices <subcommand>`

| subcommand | purpose |
|---|---|
| `scan --tx\|--rx --device TYPE [--timeout S] [--json]` | Find connected hardware for one backend (the same scan the GUI's Scan button runs) |
| `probe --tx\|--rx --device TYPE --connection STR [--timeout S]` | Check one specific connection string is reachable right now |
| `list-audio-inputs [--json]` | Real ALSA input (microphone) devices |
| `list-audio-outputs [--json]` | Real ALSA output (speaker) devices |
| `list-audio-monitors [--json]` | PipeWire sink monitors -- capture whatever's *playing* on a sink, without an acoustic loopback |
| `ensure-loopback --tx-input\|--tx-output\|--rx-input\|--rx-output [--name NAME]` | Create (if missing) one of the four persistent qpwgraph loopback nodes and print its device string |

`--device` for `scan`/`probe` accepts whatever `pluto-cli tx --help`/
`pluto-cli rx --help` list under `--device` for that direction (`pluto`,
`hackrf`, `soundcard` for TX; `pluto`, `hackrf`, `rtlsdr`, `audio` for RX).

Example:

```bash
pluto-cli devices ensure-loopback --tx-input --name my-fldigi-link
```

## 5. Safety model

- **Power ceiling.** Every device backend has its own safe default
  `--power-ceiling` (e.g. Pluto's is -20 dB attenuation) applied when the
  flag is omitted -- the same default the GUI apps start with. `--power`
  (target power) is always clamped between the device minimum and whatever
  ceiling is in effect.
- **Confirmation prompt.** By default, every `tx` command prints `Type YES
  to key up: ` and waits for exactly `YES` before transmitting anything.
  Pass `--yes` to skip this for scripted/unattended use -- doing so is an
  explicit choice to bypass the safety pause, not a default.
- **Ctrl-C / SIGTERM.** Every command installs a signal handler that always
  calls the flowgraph's safe-shutdown path (`shutdown_safe()` on TX, which
  also unkeys if still keyed and forces the device to its safe idle state;
  `shutdown()` on RX) before exiting -- interrupting a `pluto-cli tx`
  command never leaves the radio transmitting.
- **Uncaught exceptions** are also routed through the same safe-shutdown
  path via a `sys.excepthook`, so a crash mid-transmission still unkeys the
  radio before the process exits. Both shutdown paths are idempotent --
  safe to reach from more than one of these converging code paths.
- **M17/RADE end-of-transmission tails.** M17 holds keyed briefly after
  `unkey_ptt()` to send its End-Of-Transmission frame; RADE does the same
  for its End-Of-Over marker when `--eoo` is given. Both waits happen
  automatically before shutdown, matching the GUI apps' own behavior.
- **Connection errors are caught, not thrown.** Before building any
  flowgraph, every `tx`/`rx` command first probes the resolved `--device`/
  `--uri` with a bounded timeout (the same `probe_with_timeout()` check the
  GUI apps' own Connect button runs), and the flowgraph construction itself
  is also wrapped -- an unreachable Pluto IP, a wrong/stale mDNS hit, a
  missing HackRF/RTL-SDR, or a busy audio device all print one `error` line
  (`could not connect to <device> (<connection>): <reason>`) and exit with
  status 1, instead of a raw multi-frame Python traceback. **A real,
  recurring gotcha this surfaces clearly**: the Pluto+'s default connection
  (`ip:plutoplus.local`) is resolved via mDNS, which can intermittently
  return the device's WiFi-interface IP instead of its actual USB-network
  address (`192.168.2.1` in a typical direct-USB setup) -- if you see this
  error for a Pluto+ that's definitely powered on and connected, pass
  `--uri ip:192.168.2.1` (or whatever `pluto-cli devices scan --tx --device
  pluto` reports) explicitly instead of relying on the mDNS name.

## 6. JSON output schema

With `--json`, every line on stdout is exactly one JSON object with an
`"event"` key. Fields beyond `event` vary per event type:

| event | when | fields |
|---|---|---|
| `keyed` | TX: just keyed up | `mode` (int, the flowgraph's `MODE_*` value) |
| `unkeyed` | TX: just unkeyed | `mode` |
| `interactive_ready` | TX `--interactive`: ready for input | `hint` |
| `shutdown` | TX or RX: safe shutdown has completed | -- |
| `started` | RX: flowgraph is running and unmuted | -- |
| `psk31_char` | RX `--digimode psk31`: one decoded character (RX-only; TX `psk31` prints nothing) | `char` (single character string) |
| `meshtastic_listen` | RX `--digimode meshtastic`: once at startup | `preset`, `freq_hz`, `regulatory` |
| `meshtastic_frame` | TX `meshtastic`: the frame about to be sent (`preset`, `freq_hz`, `text`, `bytes`, `airtime_s`). RX: one received frame | RX: `kind`, `bytes`, and for decodable frames `from`, `to` (hex), `id`, `hop_limit`, `hop_start`, `text` |
| `rtty_char` | RX `--digimode rtty`: one decoded character (RX-only; TX `rtty` prints nothing) | `char` (single character string) |
| `m17_fields` | RX `m17` mode: one decoded M17 frame | the decoded LSF fields dict as reported by `gr-m17` (`dst`, `src`, `type`, `meta`, ...; numpy arrays are converted to plain lists) |
| `filebroadcast_added` | TX `filebroadcast`: a `--file` was registered at startup | `file_id`, `filename`, `bytes` |
| `filebroadcast_progress` | RX: a watched file's received-byte count changed | `file_id`, `filename`, `bytes_received`, `total_size`, `is_complete` |
| `filebroadcast_saved` | RX: a complete file was written to `--filebroadcast-save-dir` | `file_id`, `filename`, `path` |
| `signal` | a SIGINT/SIGTERM was caught | `signum` |
| `aborted` | TX: confirmation prompt was declined | `reason` |
| `error` | M17/RADE unavailable, an uncaught exception, or a scan/probe failure | `message` |

Without `--json`, the same events print as plain `event key=value ...` lines
(e.g. `keyed mode=2`), and PSK31/RTTY characters print as a raw, unframed
text stream instead of one `psk31_char`/`rtty_char` event per character.

## 7. Recipes

### fldigi (or any digimode app) over the Baseband mode

Baseband mode does no audio processing at all -- signals pass through as
unfiltered/undeviated as the RF chain allows, and can be wider than typical
3 kHz SSB audio (configurable via `--deviation-hz` on TX / `--width-hz` on
RX). Route fldigi's audio through the persistent qpwgraph loopback nodes
instead of real speakers/microphones:

```bash
# One-time: create the loopback nodes (both apps also do this automatically
# at their own startup, but pluto-cli can provision them standalone).
pluto-cli devices ensure-loopback --tx-input
pluto-cli devices ensure-loopback --rx-output

# In fldigi: set Audio > Devices > Capture to the "pluto-tx-input" monitor,
# and Playback to a device patched from "pluto-advanced-rx-output".
# (Use qpwgraph, or `pluto-cli devices list-audio-monitors`, to patch them.)

pluto-cli tx baseband --freq 432150000 --audio-device "monitor:pluto-tx-input" \
    --duration 30 --yes
pluto-cli rx baseband --freq 432150000 --audio-out "pluto-advanced-rx-output" \
    --duration 30
```

### File Broadcast round-trip

```bash
pluto-cli tx filebroadcast --freq 432150000 --file report.pdf --duration 20 --yes &
pluto-cli rx filebroadcast --freq 432150000 --filebroadcast-save-dir ./received --duration 25
```

Note there's no `rx filebroadcast` mode by itself -- File Broadcast RX is
always-on in parallel with whichever primary mode you pick (`fm` works
fine as a no-op placeholder primary mode if you only care about the file).

### M17 TX -> RX test

```bash
pluto-cli tx m17 --freq 432150000 --src-callsign DA2JH --power-ceiling -50 --yes &
pluto-cli rx m17 --freq 432150000 --device rtlsdr --duration 15 --json
```

(`--power-ceiling -50` here is a deliberately deep attenuation for a
close-range/attended test -- pick a level appropriate to your own setup and
license conditions.)

### RTTY TX -> RX test

```bash
pluto-cli tx rtty --freq 432150000 --text "DE DA2JH PSE K" --power-ceiling -20 --yes &
pluto-cli rx fm --freq 432150000 --device rtlsdr --digimode rtty --duration 15 --json
```

Defaults are the classic HF-RTTY tone pair (Mark 2125Hz, 170Hz shift,
45.45 baud) -- override with `--mark-hz`/`--shift-hz`/`--baud-rate` on TX
and the matching `--rtty-mark-hz`/`--rtty-shift-hz`/`--rtty-baud-rate` on
RX. Any RX mode works alongside `--digimode rtty` (`fm` above is just a
placeholder primary mode -- RTTY decodes from the IF stage independently
of it, same as PSK31).

## 8. Exit codes & known limitations

- Exit code `0`: success. `1`: a handled error (bad device, unreachable
  connection, M17/RADE unavailable, declined confirmation). `130`: Ctrl-C.
- **No live waterfall/spectrum display.** `pluto-cli` is headless by
  design -- for spectrum visualization, PSK31 waterfall tuning, or visual
  digitext composition, use the GUI apps (`pluto-tx`, `pluto-advanced-rx`).
- **One process per physical device at a time** -- see the hardware-
  exclusivity note in [section 1](#1-overview--philosophy).
- `tx digitext`/`tx psk31`/`tx rtty` ignore `--duration`: their transmission
  length is computed from the rendered text and cannot be shortened or
  extended.
- **Only one of PSK31/RTTY can decode at a time** (`--digimode`) -- see
  section 4's own note on why this is a real GNU Radio limitation, not a
  `pluto_cli` restriction.
