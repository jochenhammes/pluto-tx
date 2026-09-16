# FT8 digimode: handoff notes for a fresh implementation attempt

## BACKLOG — not actively worked on

**As of 2026-09-16, this entire feature is parked in the backlog by user
request.** All FT8-related files were moved out of the live app
directories (`pluto_tx/`, `pluto_advanced_rx/`, repo root) into this
`backlog/ft8/` folder, preserving their original relative paths (so
`backlog/ft8/pluto_tx/ft8.py` belongs back at `pluto_tx/ft8.py`,
`backlog/ft8/install-ft8.sh` at the repo root, etc., if/when this is
resumed). Nothing here is wired into either app; moving these files does
not change any current app behavior. `backlog/ft8/ft8_lib/` (the cloned
dependency + build artifacts) is gitignored, same convention as `gr-m17/`
and `rade_c/` at the repo root.

If resuming: move the files back to their original locations first (see
the path note on each file in §7), THEN start from step 1 in §8 below.
Do not resume in place inside `backlog/`.

---

Status as of 2026-09-13: **PHY layer does not yet produce a real-hardware
decode.** Extensive diagnosis was done this session; the leading suspect
is a TX-side frequency-deviation compression that has NOT been root-caused.
This file exists so a new session (with no memory of this one) can pick up
without repeating the same dead ends. Read this fully before writing any
new code or running any real-hardware test.

The original plan this work followed is (or was) at
`/home/hammesj/.claude/plans/swirling-waddling-noodle.md` — it may not be
accessible to you; the relevant parts are folded into this document.

## 1. What FT8 is supposed to become here

- TX in `pluto_tx`, RX in `pluto_advanced_rx` — same "separate,
  independently-runnable processes" relationship as every other mode in
  this project (M17, RADE, FreeDV, PSK31, Digitext, File Broadcast).
- **Manual message selection**, not full WSJT-X-style automated QSO
  sequencing. Operator picks/composes a standard FT8 message (CQ, reply,
  report, RRR, 73, free text) per 15s slot; RX shows a decoded-message
  list. Mirrors PSK31's "line-based send," not a QSO state machine.
- Reuse an existing open-source FT8 codec (`kgoba/ft8_lib`, MIT) via
  ctypes, not a from-scratch LDPC(174,91)/message-packing
  reimplementation.
- **New requirement from the user, not yet started at all**: FT8 RX must
  also work in `pluto_advanced_rx`'s Soundcard/AudioDevice mode, which
  needs a new audio-frequency-tuning GUI control (Soundcard mode has no
  RF tuning concept, only an audio passband — the operator needs to be
  able to correct for an audio source that isn't exactly on frequency).

## 2. What's already built and PROVEN correct (software-only, zero hardware)

These are solid; do not re-derive or doubt them without new evidence.

- **`install-ft8.sh`** (repo root, uncommitted): clones `kgoba/ft8_lib` at
  pinned commit `9fec6ca39886edbf96f4f5e71edc76da5074e871`, builds
  `libft8.a` via the library's own Makefile, then does
  `gcc -shared -fPIC -Wl,--whole-archive libft8.a -Wl,--no-whole-archive
  <fft/*.o> -lm -o libft8wrap.so` — **no custom C shim needed**, all
  required functions are cleanly exported this way (confirmed via
  `nm -D`). Regenerates both `~/.local/bin/pluto-tx` and
  `~/.local/bin/pluto-advanced-rx` launchers, merge-safe (preserves
  M17/RADE's existing `LD_LIBRARY_PATH`/`PATH` entries). Verified working:
  builds `libft8wrap.so` at `ft8_lib/libft8wrap.so`.
- **`pluto_tx/ft8_ctypes.py`** (identical copy at
  `pluto_advanced_rx/ft8_ctypes.py`, matches this project's per-package
  ctypes-duplication convention like `rade_ctypes.py`): full ctypes
  bindings for `ftx_message_encode/decode`, `ft8_encode`,
  `monitor_init/process/reset/free`, `ftx_find_candidates`,
  `ftx_decode_candidate`. `FT8_AVAILABLE` flag gated on a real
  encode→decode round-trip smoke test at import time (uses callsign
  `"CQ DA2JH JO31"` — **do not use a placeholder like `"DX"`**, it's not
  a standard-format callsign and routes through the NONSTANDARD/hashed-
  callsign path, which the no-op `lookup_hash` stub can't resolve,
  breaking the smoke test with a false failure). `Ft8Monitor` class
  wraps one 15s-slot monitor+decode cycle. **Gap**: does not bind
  `kFT8_Gray_map` (an 8-entry Gray-code permutation table in
  `ft8/constants.c` — `{0,1,3,2,5,6,4,7}` — used internally by
  `ft8_extract_symbol()`/`ft8_extract_likelihood()` in `decode.c` to map
  waterfall bin index → 3-bit LLR symbol value; NOT needed for normal
  encode/decode use, only if you want to replicate the library's own
  soft-bit extraction externally for diagnostics).
- **`pluto_tx/ft8.py`**: pure Python/numpy port of `ft8_lib`'s
  `demo/gen_ft8.c` GFSK synthesis (`gfsk_pulse()`/`synth_gfsk()`,
  `FT8_SYMBOL_BT=2.0`, erf-based Gaussian pulse,
  `GFSK_CONST_K=5.336446`). Produces REAL audio (`sin(phi)`) at a given
  sample rate and base tone frequency. **Verified bit-exact correct**:
  encode → `Ft8Monitor` → decode round-trips exactly for 5 varied
  messages at 12000Hz and 48000Hz, zero hardware involved. This proves
  the encoder itself, the ctypes bindings, and the GFSK math are all
  correct.
- **Key protocol facts** (from reading the real, locally-cloned
  `ft8_lib` source, not assumed): 79 symbols total (58 data + 3×7 Costas
  sync at positions 0/36/72, Costas tone pattern `kFT8_Costas_pattern =
  {3,1,4,0,6,5,2}`), symbol period 0.16s (79 symbols = 12.64s, padded to
  15s slot), tone spacing `FT8_TONE_SPACING_HZ = 6.25Hz` (8 tones = 50Hz
  occupied bandwidth), LDPC(174,91), `ft8_encode()`'s `tones[]` output
  already has `kFT8_Gray_map` applied — i.e. it IS the actual on-air raw
  tone index sequence, directly comparable to a hard-decision tone
  readout without any further Gray-code translation.
- **`monitor_process()`** needs *exactly* `block_size` samples per call
  (`block_size = sample_rate * symbol_period`, e.g. 1920 @ 12000Hz, 2000
  @ 12500Hz) — confirmed via real testing.
- **GFSK's 3-symbol-wide pulse smoothing is real and expected**, not a
  bug: `synth_gfsk()`'s pulse array spans `3*n_spsym`, so a naive "one
  FFT window per symbol" measurement fundamentally cannot cleanly
  resolve fast symbol-to-symbol tone changes — this is inherent to the
  BT=2.0 Gaussian shaping, confirmed by software-only reference tests.
  **However**: a controlled software-only measurement (0,7,0,7,...
  alternating pattern, incoherent power-spectrum averaging across many
  repeats, zero-padded FFT) shows the reference synthesis STILL achieves
  ~99.7-99.8% of the theoretical 43.75Hz tone0↔tone7 separation despite
  this smoothing — so "GFSK smoothing" does NOT explain away a large
  deviation-compression finding on real hardware (see §5).

## 3. Real-hardware setup facts (do not re-derive, ask the user if unsure)

- Hardware: PlutoSDR+ (`ip:192.168.2.1`) for TX, RTL-SDR for RX, an
  RTL-SDR with its own `[R82XX] PLL not locked!` driver warning and a
  confirmed continuous drift (~2.8Hz/s via cable+pure-tone test).
- **Cable topology (confirmed directly with the user 2026-09-13): the
  40dB-pad cable connects Pluto TX → RTL-SDR ONLY.** The Pluto's own RX
  port is NOT connected to anything via this cable. This matters a lot:
  any test that used `rx_devices.build_device("pluto", ...)` /
  `--pluto-rx` (same physical device as TX, `ip:192.168.2.1` for both)
  was **not** receiving via the cable at all — at best it was picking up
  uncontrolled internal TX→RX leakage on the same AD9361/9364 chip, not
  a real, attenuated RF path. Discard conclusions drawn from `--pluto-rx`
  tests about absolute signal quality; they may still be informative for
  relative/software-side comparisons but are not a substitute for a real
  cabled test. **Use the RTL-SDR (the actual default receiver in
  `ft8_ota_test.py`, no `--pluto-rx` flag) for any real-hardware
  over-the-cable verification.**
- **The Pluto's TX and RX channels share a single `sampling_frequency`
  register on `ad9361-phy voltage0`** (confirmed via
  `iio_attr -u ip:192.168.2.1 -c ad9361-phy voltage0 -i/-o
  sampling_frequency`): building an `rx_devices.build_device("pluto",
  sample_rate_hz=1_600_000, ...)` object and THEN a
  `devices.build_device("pluto", sample_rate_hz=2_500_000, ...)` TX
  object on the same connection leaves BOTH channels reporting
  2,500,000 afterward — i.e. whichever device is constructed LAST wins,
  silently overriding the other's actual configured rate. This is a
  device/model-specific hardware behavior (this board reports as
  `ad9364` per `iio_info`), not a bug in gr-iio's Python bindings. If a
  test script ever needs simultaneous TX+RX on ONE physical Pluto again,
  either use the SAME sample rate for both, or read back the actual
  configured rate via `iio_attr` after both are built rather than
  trusting the requested value — a rate MISMATCH between what a script
  assumes and what's physically true produces a clean, deterministic
  frequency-axis SCALING error in every downstream Hz calculation (this
  was directly observed and explains a large chunk of earlier apparent
  "deviation compression" — but NOT all of it, see §5, since it
  persisted through an independent RTL-SDR test too).
- TX attenuation levels found calibrated this session (23cm band,
  432.15MHz, through the 40dB cable pad): `TX_ATTEN_DB=-10.0`,
  `RX_GAIN_DB=49.0` on the RTL-SDR gives roughly 62dB SNR — a very
  strong link, SNR is not the blocker.
- Real IQ amplitude (peak/RMS) is a **misleading** calibration metric —
  dominated by broadband receiver noise across the full RX bandwidth,
  not the narrowband signal. Always calibrate via FFT peak-vs-median-
  floor SNR on a captured segment, not raw amplitude.
- A short-time FFT needs zero-padding (`n_fft` far larger than the
  window) to avoid spectral-leakage/picket-fence jitter that looks like
  false frequency instability on an actually-stable tone.

## 4. Hypotheses tested and RULED OUT this session (with evidence)

Do not re-test these without a genuinely new angle — they were checked
carefully.

1. **Auto-designed `rational_resampler_ccf`/`_fff` taps (`taps=[]`)
   corrupting the RX chain.** Real, confirmed general finding (this
   project's own File Broadcast Phase 0 precedent, `flowgraph.py:526-541`
   — GNU Radio's auto-designed taps really do corrupt phase-continuous
   GFSK badly enough to break symbol clock recovery). Fixed on the RX
   side (`if_filter`/`audio_resampler` use explicit `firdes.low_pass()`
   taps in all current test scripts) — improved things but did NOT fix
   decoding.
2. **TX-side resampler auto-taps** (the same class of bug, unfixed on
   the TX side in every Hilbert-based mode incl. the FT8 test scripts,
   M17, RADE, FreeDV, PSK31, Digitext — only File Broadcast's TX
   resampler already uses explicit taps). Tested explicitly: applying
   File Broadcast's *exact* proven formula
   (`firdes.low_pass(interp, quad_rate, working_rate/2*0.9,
   working_rate*0.3, WIN_HAMMING)`) to the FT8 TX resampler made **no
   measurable difference** (~61% deviation before, ~61% after). Ruled
   out.
3. **Hilbert-transform-of-real-audio approximation** (the "synthesize
   real audio tone → `hilbert_fc` → SSB-style upconversion" technique
   FT8's test scripts copied from PSK31/Digitext/FreeDV) as opposed to
   direct complex-baseband synthesis (what File Broadcast/M17/RADE
   actually do — see §5's architecture table). Tested explicitly:
   synthesizing `exp(1j*phi)` directly (bypassing Hilbert entirely) and
   feeding it straight into the resampler made **no measurable
   difference** either (~57-61% either way). Ruled out.
4. **RX chain (if_filter/ssb_filter/complex_to_real/audio_resampler)
   corrupting deviation.** Tested in **pure software**, no hardware at
   all: fed a synthesized 0/7-alternating complex baseband signal
   directly through the exact same RX GNU Radio blocks
   (`vector_source_c → ssb_filter → complex_to_real → audio_resampler →
   vector_sink_f`). Result: 99.7% of theoretical separation. The RX
   digital chain is clean. Ruled out.
5. **Near-DC / AD9361 DC-avoidance analog artifact** (compression
   depends on how close the baseband tone sits to 0Hz). Tested at
   TONE_HZ=15,000Hz instead of 1,500Hz (10x further from DC, well outside
   any near-DC correction-loop bandwidth) via real hardware. Result:
   **same ~57% compression**, no improvement. Ruled out.
6. **Start-of-symbol timing/alignment artifact in the measurement
   script itself.** Tested by sweeping the assumed start offset by
   ±16ms (a full symbol period) in 1.6ms steps against an already-saved
   real capture: measured separation was **completely flat** (26.6Hz
   ±0.03Hz) across the entire sweep. Ruled out.
7. **`ldpc_errors` from `ftx_decode_candidate()` as a proxy for "close to
   a correct decode."** This was an actively misleading assumption made
   earlier in the session (candidates with `ldpc_errors` as low as 2
   were treated as "almost decoding"). Directly falsified: reconstructed
   the exact bin-indexing formula `ftx_lib` itself uses
   (`get_cand_mag()`/`ft8_extract_likelihood()` in `decode.c`, block
   addressing = `time_offset*time_osr*freq_osr*num_bins + ...`,
   `block_stride = time_osr*freq_osr*num_bins`), extracted the actual
   raw hard-decision tone sequence at the lowest-`ldpc_errors` candidate,
   and compared it to the known transmitted tone sequence:
   **correlation ≈ 0** despite `ldpc_errors=2`. The belief-propagation
   LDPC decoder apparently converges to *some* self-consistent-looking
   codeword fairly easily even from near-random input; a low
   `ldpc_errors` on a candidate that never CRC-validates is not evidence
   of anything. Use `ftx_decode_candidate()`'s return value (CRC pass/
   fail) as the only trustworthy signal, never `ldpc_errors` alone.
8. **A large, real, RANDOM phase-continuity glitch found via raw-IQ
   phase analysis** (~60-80ms glitches, phase_std spiking from ~0.12rad
   baseline to 10+rad) does exist on this Pluto (matches an external
   community report, amsat-dl.org forum user DD1US, of PlutoSDR TX-start
   drift) — but measured at **0/10 occurrence** when the device
   connection is established once and reused across multiple key-ups
   (matching real GUI session behavior), and a separate "warm, glitch-
   free" decode-rate test still showed 0/8 successful decodes. **This
   glitch is confirmed NOT the primary cause** of decode failure.
9. **A `repeat=True` loop-wrap bug** was found in one throwaway test
   script (`ft8_warm_decode_rate.py` — the TX audio source looped
   continuously across a `capture_s` window longer than one message
   period, so the RX capture was not guaranteed to contain one
   uninterrupted 79-symbol span). This is a real bug worth avoiding in
   any reused test harness, but re-testing with the original
   `repeat=False` script (`ft8_ota_test.py`) showed the **same
   ~zero-correlation decode failure** — so it was a real methodology bug
   but not the root cause either.

## 5. The leading unresolved finding: TX-side frequency-deviation compression

**This is the most likely actual root cause and is NOT yet fixed.**

Measured via a maximum-contrast diagnostic: transmit a repeating
`tone=0, tone=7, tone=0, tone=7, ...` GFSK pattern (via
`ft8._synth_gfsk()`/a complex-baseband variant directly, bypassing the
full protocol layer), receive it, and measure the achieved tone0↔tone7
frequency separation via incoherent-averaged, zero-padded FFT power
spectra (robust to phase drift and alignment — cluster the raw
per-symbol peak readings into "low" and "high" groups rather than
trusting a fixed symbol index, since **the fixed-index/averaged version
of this measurement is unreliable** — a slow alignment slip was observed
repeatedly; always re-derive by clustering the raw per-symbol readings,
not a naive "average all evens vs all odds").

Results (theoretical separation = 7×6.25 = 43.75Hz):

| Path tested | Result |
|---|---|
| Pure software (`_synth_gfsk`, real audio, `sin(phi)`) | 99.8% |
| Pure software (complex baseband, `exp(1j·phi)`) | 99.7-99.8% (equivalent) |
| Pure software, RX chain only (if_filter→ssb_filter→resampler) | 99.7% |
| Real HW, Hilbert-audio TX, `--pluto-rx` (later shown to be internal leakage, not cable) | 60.9-61.0% |
| Real HW, complex-baseband TX + File Broadcast's exact resampler taps, `--pluto-rx` (leakage) | 60.9% |
| Real HW, same TX, TONE_HZ=15kHz instead of 1.5kHz, `--pluto-rx` (leakage) | 57.1% |
| **Real HW, complex-baseband TX, received via RTL-SDR (the actually-cabled device)** | **49.5%** |

The RTL-SDR result is the important one: it is the only one of these
that went through the real, physically-cabled RF path, on an
independent-clock device with no shared-register ambiguity with the TX.
**It still shows severe compression** (worse than the Pluto-loopback
numbers, if anything) — ruling out both the shared-sample-rate-register
artifact and the internal-leakage-path concern as explanations. Some of
the RTL-SDR run's spread (std ≈ 7.5Hz across clusters) is attributable
to RTL-SDR's own independent, already-documented ~2.8Hz/s drift, but a
slowly-drifting *common-mode* frequency shift cannot by itself explain a
reduced *separation* between two tones sampled close together in time —
so this is not simply RTL-SDR drift showing up as compression.

**Not yet tested / good next steps for a fresh session:**

- Repeat the RTL-SDR deviation test 2-3 more times to rule out a
  one-off measurement fluke (only run once so far) and get a stable
  average.
- Try varying the Pluto's TX `bandwidth_hz` (currently 200,000 — should
  be far more than enough for a 50Hz-wide signal, but verify; the
  AD9361 analog TX filter's actual -3dB/passband-ripple shape near a
  ~1-2kHz baseband offset has not been independently characterized).
- Check whether `quad_rate` (2,500,000) itself, or the specific TX
  resampler interpolation ratio at that rate, introduces any passband
  ripple across the ~50Hz FT8 signal bandwidth — plot the actual
  resampler's frequency response (not just trust the `firdes.low_pass()`
  design parameters) and check flatness across a 50Hz span near the
  chosen tone offset.
- Consider testing with a **wired (not RF, not even the 40dB pad)
  loopback** — e.g., if the Pluto supports an internal digital
  loopback/BIST mode, or connecting TX and RX with a very short coax and
  a known-good, higher-value attenuator — to further isolate "AD9361
  analog chain" from "cable/attenuator/propagation."
- Directly compare against **File Broadcast's own real, working GFSK
  signal** on real hardware: does File Broadcast (which uses
  `digital.gfsk_mod` + explicit-taps resampler, deviation=50,000Hz) show
  ANY measurable deviation compression on this same hardware, even a
  small percentage? If File Broadcast is provably 100% clean on real
  hardware while FT8's signal (at 1/1000th the deviation) is not, that
  would strongly suggest the issue scales with how NARROW the signal is
  relative to some fixed-Hz-scale distortion (filter ripple, group
  delay non-linearity, some AD9361 correction loop) — worth measuring
  File Broadcast's own real deviation precisely rather than assuming
  it's "obviously fine" because file transfers complete successfully
  (a working file transfer only proves the *symbol clock/framing*
  survives, not that deviation is at 100% — File Broadcast might
  tolerate real compression that FT8, with its 8x finer resolution
  requirement, cannot).
- **The user's own suggestion, not yet fully executed**: synthesize FT8
  directly as complex baseband from the start (like File Broadcast does
  via `digital.gfsk_mod`) rather than via the Hilbert-of-audio-tone
  route, for the REAL production TX chain (not just an isolated test) —
  this was tested in isolation (§5 table) and did not by itself fix the
  compression, but it IS the architecturally-correct approach going
  forward regardless (matches every other GFSK/FSK mode in this
  codebase) and should still be adopted rather than reverted to Hilbert-
  of-audio, since it removes one whole category of approximation even
  if it wasn't the deciding factor found so far.

## 6. Architecture comparison across modes (for context on why this is hard)

| Mode | TX synthesis | Deviation/bandwidth | TX resampler taps |
|---|---|---|---|
| File Broadcast | `digital.gfsk_mod` (direct complex baseband) | 50,000 Hz | **explicit** (only mode that already fixed this) |
| M17 | `analog.frequency_modulator_fc` (direct complex baseband) | 800 Hz (outer symbol) | `taps=[]` (auto) |
| RADE | own OFDM engine, outputs complex IQ directly | wideband OFDM | `taps=[]` (auto) |
| FreeDV | Hilbert-of-audio-tone (same technique as FT8's test scripts) | ~1-2kHz modem bandwidth | `taps=[]` (auto) |
| PSK31 / Digitext | Hilbert-of-audio-tone | narrowband but PSK not FSK — no "deviation" concept the same way | `taps=[]` (auto) |
| **FT8 (this work)** | Hilbert-of-audio-tone in test scripts so far | **43.75 Hz** (smallest of any mode by ~18x) | `taps=[]` in test scripts; File Broadcast's exact explicit formula tested, no improvement |

FT8 has by far the smallest absolute frequency deviation of any mode in
this codebase (6.25Hz per tone step, 43.75Hz total span) — small enough
that a distortion imperceptible to every other mode here could still be
fatal to FT8 specifically. This is why "other modes work fine" does not
by itself clear the TX chain for FT8's purposes; it was investigated
directly (§4.2-4.5) rather than assumed, and the compression survived
every isolation test performed so far except the ones not yet run
(§5's "not yet tested" list).

## 7. Files that exist right now (all parked in `backlog/ft8/`, uncommitted except this note)

All paths below are relative to `backlog/ft8/` and show where each file
belongs if moved back for active work:

- `install-ft8.sh` → back to repo root — verified working
- `ft8_lib/` → back to repo root — cloned dependency, built
  `libft8wrap.so` inside it; gitignored (`backlog/ft8/ft8_lib/` in
  `.gitignore`), same convention as `gr-m17/`/`rade_c/`
- `pluto_tx/ft8.py` → back to `pluto_tx/ft8.py` — verified correct in
  software
- `pluto_tx/ft8_ctypes.py` → back to `pluto_tx/ft8_ctypes.py`,
  `pluto_advanced_rx/ft8_ctypes.py` → back to
  `pluto_advanced_rx/ft8_ctypes.py` (identical copies) — verified
  correct in software
- No GUI integration, no `flowgraph.py` changes, no `MODE_FT8` constant,
  nothing wired into either app yet — Phase 0 (PHY proof of concept) is
  the ONLY thing attempted, and it has not yet passed its own gate (a
  reproducible real-hardware decode).
- All diagnostic/test scripts referenced in this document live in a
  **session-scoped scratchpad directory that will NOT persist** to a new
  session (`/tmp/claude-.../scratchpad/ft8_*.py`) — do not expect them
  to still exist; the useful numeric findings and formulas from them are
  captured in this document instead. If you want the actual test-script
  code back, you'll likely need to rewrite it from the descriptions
  here (they were all fairly short, single-purpose GNU Radio flowgraphs
  following the patterns already shown in this repo's own
  `pluto_tx`/`pluto_advanced_rx` device-abstraction code).

## 8. Recommended approach for a fresh session

0. Move the files listed in §7 back to their original locations (out of
   `backlog/ft8/`) before starting any of the following.
1. Re-read this file fully, and re-read `pluto_tx/ft8.py` +
   `pluto_tx/ft8_ctypes.py` (already correct, don't rewrite them).
2. Re-run the software-only round trip (§2) as a sanity check that
   nothing has bit-rotted — should still pass instantly, no hardware.
3. Before touching real hardware again, resolve the TX-side deviation
   question analytically if possible (§5's "not yet tested" list) —
   this session burned a lot of real transmit time chasing this and a
   fresh, well-targeted test or two is worth more than broad re-
   exploration.
4. Once a real, reproducible (≥2 consecutive successful runs, this
   project's own established gate) hardware decode is achieved, only
   THEN proceed to Phase 1 (GUI integration into both apps) — do not
   start GUI work before the PHY is proven, per this project's own
   established staged-development discipline (see PSK31/File
   Broadcast's own git history for the precedent).
5. Remember the still-pending, not-yet-started user requirement: FT8 RX
   in `pluto_advanced_rx`'s Soundcard/AudioDevice mode, with a new
   audio-frequency-tuning GUI control.
