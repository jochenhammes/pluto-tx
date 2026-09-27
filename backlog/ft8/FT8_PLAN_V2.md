# PLAN: FT8, second attempt (TX + RX, PHY first and validated against real signals)

## Context

We parked FT8 on 2026-09-16 (`backlog/ft8/`, handoff doc `backlog/ft8/FT8_HANDOFF.md`). In software the
encoder, the ctypes bindings and the GFSK synthesis round-trip bit-exactly. Over real hardware
(Pluto → 40 dB → RTL, 432 MHz) there was never a decode. The suspected cause was "deviation compression
to ~50 %", but it was never explained.

**What we have learned since then:**
- POCSAG, MeshCore and FM voice were all verified with the same method: capture IQ from the RTL, then
  demodulate and analyse offline, with a known-good reference.
- The ppm correction exists now, and the Pluto address is now `ip:169.254.10.209`.

**What went wrong in the first attempt:**
- **There was no independent judge.** Only our own decoder was used, so we could never tell whether TX,
  RX or the measurement method was broken.
- **RTL drift was never corrected.** The RTL drift was measured at ~2.8 Hz/s. Over one 12.6 s frame that
  is ≈35 Hz, i.e. more than 5 FT8 tone spacings. ft8_lib has no drift tracking, so this alone is enough
  to make every frame undecodable. The 0/7 alternating-tone measurement was also sensitive to drift and
  alignment.

**New resources (checked):**
- **WSJT-X 3.0.2 is installed (`/usr/bin/jt9`).** This is the reference decoder, used as an independent
  judge for every stage.
- **`ft8_lib/test/wav/`** holds 27 real HF recordings, each with a `.txt` file of the expected decodes.
  That gives an offline benchmark with no network.
- **Internet access works.** Live reception goes through a **KiwiSDR** using `kiwirecorder.py`
  (jks-prv/kiwiclient). It delivers raw 12 kHz USB audio or IQ as WAV and is scriptable.
  - **Why not the classic websdr.org:** the classic websdr.org (PA3FWM) has no documented audio API,
    which is why the KiwiSDR route is recommended here.

Goal: FT8 as a digimode with manual message selection per 15 s slot, as originally planned.
- TX in `pluto_tx`, RX in `pluto_advanced_rx` (including the Soundcard device with audio frequency
  tuning), plus the CLI.
- **Gate as before:** GUI work starts only after the PHY has been decoded reproducibly (≥2 times in a
  row), by jt9 **and** by our decoder.

## Phase 0: Restore and sanity check (no hardware)

- **Move the files back:**
  - `backlog/ft8/pluto_tx/{ft8,ft8_ctypes}.py` → `pluto_tx/`
  - `backlog/ft8/pluto_advanced_rx/ft8_ctypes.py` → `pluto_advanced_rx/`
  - `install-ft8.sh` and `ft8_lib/` → repo root
  - Update the `.gitignore` entry. Keep the handoff doc as history in `backlog/ft8/` (or delete it at the
    end).
- Run the software round trip (`ft8.encode_message` → `Ft8Monitor`) again.
- **New:** write the same TX audio as a 12 kHz/16-bit WAV and decode it with `jt9 -8`. This checks the
  encoder independently of our own decoder.

## Phase 1: Harden the RX decoder with real signals (no transmission at all)

1. **Offline benchmark:**
   - Run a scratchpad script `ft8_bench.py` over all `ft8_lib/test/wav/*.wav`.
   - Compare our decodes with the `.txt` reference and with `jt9` on the same file.
   - Metrics: decode rate, false decodes (CRC-OK but not in the reference), runtime per slot.
2. **Tune `Ft8Monitor.find_and_decode`.** Today it uses 32 candidates; `demo/decode_ft8.c` uses about
   140. Parameters to tune:
   - `num_candidates`, `min_score`, `max_iterations`, `time_osr`/`freq_osr`
   - duplicate filtering
   - a callsign hash table instead of the no-op `lookup_hash`, so that `<...>` calls resolve
3. **Live test via KiwiSDR:**
   - Clone `kiwiclient` into the scratchpad (not into the repo) and pick a public Kiwi from
     kiwisdr.com/public. Use 20 m at 14.074 MHz USB in daytime, or 40 m at 7.074 MHz.
   - Record continuously for about 5–10 min and cut it into 15 s slots at the UTC boundaries. The Kiwi
     latency is estimated from jt9's DT values.
   - Decode every slot with jt9 and with our decoder.
   - **Target:** ≥80 % of jt9's decodes with SNR ≥ −15 dB, and 0 false decodes.
4. The result goes into `pluto_advanced_rx/ft8_decoder.py`. It contains the slot buffering at 12 kHz
   (`block_size` = 1920) and the decode call. It is GNU-Radio-independent and testable with WAVs.
5. **Unit tests:** `tests/test_ft8.py`, using a few `ft8_lib` test WAVs with expected messages, plus the
   encoder round trip.

## Phase 2: TX chain without RF (fake device)

- **Production TX synthesis is direct complex baseband:** `exp(j·φ)` at audio offset f0 (e.g. 1500 Hz),
  as in `ft8._synth_gfsk`. There is no Hilbert path. Resamplers use explicit `firdes.low_pass` taps, like
  File Broadcast.
- Put it into a minimal test flowgraph with the fake device. Take the captured IQ, do a software USB
  demodulation, and write a 12 kHz WAV.
- Decode that WAV with jt9 and with our decoder. Also measure the frequency of each of the 8 tones
  directly.
- **Hold-tone test:** each tone is held steady for 1 s, not alternated. This removes the old measurement
  artefact from the "compression" question for good.

## Phase 3: OTA over cable (Pluto → 40 dB → RTL)

This needs the user's approval for PTT, as in the FM test. The FM-test settings are reused:
- Pluto at `ip:169.254.10.209`, power −40 dB (ceiling −30), 432.15 MHz, **or better, 144.174 MHz**
  (3× less drift in Hz)
- RTL via soapy at 1.024 MS/s, +200 kHz offset
- Offline analysis in the scratchpad script `ft8_ota.py`

**Steps:**
1. **Drift measurement:** carrier only, 60 s. Pluto and RTL each warmed up for 5 min first. This gives
   the Hz/s value at this frequency.
2. **Hold-tone test** (tones 0..7, 1 s each) → the measured tone spacing. This settles the "compression"
   question definitively.
3. **FT8 frame plus a CW pilot**, pilot at +1 kHz from the FT8 signal.
   - Offline, the pilot frequency is tracked and removed from the IQ, which corrects the drift.
   - Then USB demodulation → 12 kHz WAV → jt9 plus our decoder, **with and without** drift correction.
   - This shows directly whether drift was the old cause.
4. **Gate:** 2 consecutive frames decoded by jt9 and by us, with sensible SNR and DF.
5. **If there is still no decode:** work through the "not yet tested" list in the handoff doc §5, in this
   order:
   - Pluto TX `bandwidth`
   - resampler ripple
   - File Broadcast deviation as a comparison

## Phase 4: App integration (only after the gate)

- **`pluto_tx`:**
  - `MODE_FT8` / digimode.
  - **GUI:**
    - Callsign and locator fields
    - Message selection: CQ, reply, report, RRR, RR73, 73, free text
    - Audio offset (200–3000 Hz, default 1500)
    - Even/odd slot choice
    - Slot countdown
  - **TX timing:** start at the UTC slot boundary +0.5 s; PTT only for that one slot (≈13 s).
  - Pattern from PSK31/POCSAG: `tx_end_loss_s` padding for HackRF.
  - Soundcard/AIOC get the real audio, the same as for PSK31.
- **`pluto_advanced_rx`:**
  - FT8 digimode: USB demodulation to 12 kHz feeding into `ft8_decoder`, running the decode in a worker
    thread at slot end.
  - A table (UTC, SNR, DT, DF, message) in the same style as the POCSAG/MeshCore tables.
  - Clicking a CQ fills in the reply message? That depends on the TX app being a separate process, so
    it stays out of scope for now.
  - **Soundcard mode:** an audio passband control (f_min/f_max), plus the audio offset tuning the user
    asked for.
  - The RTL/Pluto path uses the existing ppm correction.
- **CLI:** `pluto_cli tx ft8 --msg "CQ DA2JH JO31" [--offset 1500] [--slot even|odd] [--repeat N]` and
  `pluto_cli rx ft8` (prints decodes).
- **Clock check:** a warning in the GUI/CLI if the system time is not NTP-synced (`timedatectl`), because
  FT8 needs < ±1 s.
- **Install:** integrate `install-ft8.sh` into the existing install script. `FT8_AVAILABLE` gates the
  mode, as with M17/RADE.
- Update the READMEs.

## Phase 5: Real on-air test (by the user, not automated)

- TX on 2 m at **144.174 MHz USB** with Pluto + PA. Verify on pskreporter.info or with a nearby station.
- Soundcard/AIOC route optionally with an SSB rig.
- RX: live 2 m/HF via the user's antenna, or a Kiwi as a cross-check.

## Verification

- `QT_QPA_PLATFORM=offscreen LD_LIBRARY_PATH=$HOME/.local/lib/x86_64-linux-gnu python3 -m unittest discover tests`,
  including the new `tests/test_ft8.py`, which covers:
  - encoder ↔ decoder
  - ft8_lib reference WAVs
  - the TX fake-device chain decoding
  - GUI widgets
- Phases 1–3 use jt9 as the independent judge each time, with numbers (decode rate, SNR, DF, drift)
  reported to the user.

## Safety and constraints

- Real PTT only with the user's approval. For Phase 3 I ask again before the first transmission; the
  same cable setup as the FM test.
- Commits only when the user says so. `kiwiclient` and test recordings stay in the scratchpad, not in
  the repo.

## Critical files

- `backlog/ft8/*` (restore)
- `pluto_tx/ft8.py`, `pluto_tx/ft8_ctypes.py`
- new `pluto_advanced_rx/ft8_decoder.py`
- `pluto_tx/flowgraph.py`, `pluto_tx/gui.py`, `pluto_tx/config.py`
- `pluto_advanced_rx/flowgraph.py`, `pluto_advanced_rx/gui.py`
- `pluto_cli/tx.py`, `pluto_cli/rx.py`
- `install-ft8.sh` or the main install script
- new `tests/test_ft8.py`
