"""Shared constants for the PlutoSDR advanced RX app.

Deliberately its own, independent set of RX-tuning values -- free to diverge
from pluto_tx's own TX-tuning values without risking that app. Only
genuinely generic, non-RX-specific constants/helpers are re-exported from
pluto_tx.config (bandplan/URI helpers, M17 PHY constants that the RX demod
chain must match exactly).
"""
import math

from gnuradio.fft import window

from pluto_tx.config import DEFAULT_URI, DE_AMATEUR_BANDS_HZ, in_amateur_band, normalize_uri  # noqa: F401 (re-exported)
# M17 PHY constants -- genuinely generic (not TX-specific): the RX demod
# chain (flowgraph.py's M17 branch) must use the EXACT same symbol rate/
# RRC shape/deviation the TX side encodes with, so these are re-exported
# rather than duplicated, matching the reasoning above.
from pluto_tx.config import (  # noqa: F401 (re-exported)
    M17_CODEC2_RATE, M17_SYMBOL_RATE, M17_RRC_ALPHA, M17_RRC_NTAPS, M17_RRC_SPS,
    M17_BASEBAND_RATE, M17_DEVIATION_HZ, M17_DEFAULT_DST_CALLSIGN, M17_CALLSIGN_MAX_LEN,
)
# Baseband mode's deviation -- genuinely generic, same reasoning as the M17
# constants above: the RX demod chain (flowgraph.py's Baseband branch) must
# use the SAME deviation the TX side modulates with, or a default-settings
# receiver won't correctly capture a default-settings transmission.
from pluto_tx.config import BASEBAND_DEVIATION_HZ  # noqa: F401 (re-exported)

# RX baseband ("quadrature") rate presets -- these double as the waterfall's
# "zoom levels": each is the actual AD9361 RX sample rate (and, in "Auto"
# filter mode, the analog RX filter bandwidth the driver derives from it).
# Chosen as clean multiples of DEMOD_IF_RATE so the IF decimation stage below
# always lands on an integer decimation factor. Changing this requires a full
# flowgraph rebuild (GNU Radio FIR/resampler blocks can't change their
# decimation ratio at runtime) -- see gui.py's _on_bandwidth_changed.
#
# Extended on request up to what actually produces data on real hardware --
# each preset here was re-verified. 5M/8M/10M DO work but show real buffer
# overruns ("O" printed by GNU Radio) and audio underruns: the CURRENT
# ip:plutoplus.local / IIOD-network-protocol connection has a measured
# throughput ceiling around ~4.7-4.9 Msps (see README) -- expect choppy
# audio/gaps in the waterfall at these presets until a native USB backend
# is used instead (see README ToDo).
#
# 15M/20M were tried and are NOT included: at that decimation ratio (400:1
# down to DEMOD_IF_RATE) the auto-designed IF filter grows to >13,000 taps,
# which exceeds what GNU Radio's scheduler buffer can feed it per work()
# call -- not just overruns, but a hard scheduler error and ZERO data
# produced. Increasing buffer sizes alone does not fix this (tried,
# confirmed insufficient); it needs a multi-stage/cascaded decimation
# instead of the current single-stage rational_resampler_ccf, which is real
# rework, not a config change -- see README ToDo.
RX_BANDWIDTH_PRESETS = [1_000_000, 2_500_000, 5_000_000, 8_000_000, 10_000_000]
DEFAULT_RX_BANDWIDTH = 2_500_000

# Fixed IF rate the demodulator chain always runs at, regardless of which
# RX_BANDWIDTH_PRESETS entry is selected -- keeps the FM/SSB demod + audio
# resampler chain identical across zoom levels; only the IF decimation
# stage's ratio changes per bandwidth.
DEMOD_IF_RATE = 50_000

AUDIO_RATE = 48_000
FM_DEVIATION_HZ = 2500.0  # matches pluto_tx's narrowband voice FM default
SSB_AUDIO_BAND_HZ = (300.0, 2700.0, 300.0)  # (f_lo, f_hi, trans_width), USB

# FM audio low-pass filter (demod noise cleanup above the voice band, AFTER
# quadrature_demod_cf -- distinct from FM_DEMOD_WIDTH below, which band-limits
# the IF/RF signal BEFORE demod).
FM_AUDIO_CUTOFF_HZ = 3000.0
FM_AUDIO_TRANS_HZ = 500.0

# --- Demodulator width: an actual, operator-adjustable channel filter, not
# just a display estimate. FM: a real/low-pass-shaped filter (fir_filter_ccc
# with real low-pass taps, symmetric around 0 Hz -- a standard technique for
# band-limiting a complex signal) applied to the IF signal BEFORE
# quadrature_demod_cf, i.e. the actual RF/IF channel width. SSB: directly
# the width of the existing complex_band_pass demodulator filter (f_hi - f_lo,
# with f_lo held fixed -- widening/narrowing extends f_hi). Both are
# runtime-adjustable via set_taps() on the already-connected filter blocks,
# no flowgraph rebuild needed. The waterfall's demod-band overlay is driven
# by these same values, so it now shows the ACTUAL filter width, not an
# estimate.
FM_DEMOD_WIDTH_DEFAULT_HZ = 12_500.0  # standard NBFM channel spacing
FM_DEMOD_WIDTH_RANGE_HZ = (2_500.0, 20_000.0)
FM_CHANNEL_TRANS_HZ = 1_000.0

SSB_DEMOD_WIDTH_DEFAULT_HZ = 3_000.0
SSB_DEMOD_WIDTH_RANGE_HZ = (1_000.0, 5_000.0)

# Baseband mode's own IF channel-filter width -- wider range than FM's
# (up to the real ceiling: AUDIO_RATE/2 = 24kHz, tighter than
# DEMOD_IF_RATE/2 = 25kHz -- see flowgraph.py's Baseband branch), since
# its whole point is carrying content wider than typical 3kHz voice/SSB
# audio (RTTY/PSK31/Olivia/MFSK etc.).
BASEBAND_WIDTH_DEFAULT_HZ = 20_000.0
BASEBAND_WIDTH_RANGE_HZ = (2_500.0, 24_000.0)

# RADE V1's OFDM occupied bandwidth -- NOT operator-adjustable (a fixed
# protocol constant, unlike FM/SSB's width sliders above), used only to
# drive the waterfall's RADE demod-band overlay. Derived directly from
# rade_c's own source (verified this session, not assumed): rade_dsp.h
# (RADE_FS=8000, RADE_NC=30, RADE_M=160) and rade_ofdm.c's rade_ofdm_init()
# (Rs'=Fs/M=50Hz, carrier_1_freq=1500-Rs'*Nc/2=750Hz,
# carrier_1_index=round(750/50)=15, carriers at (15+c)*50Hz for c=0..29 ->
# 750-2200Hz) -- the same "centered in the middle of an SSB passband"
# convention already documented for pluto_tx's RADE-over-soundcard feature.
RADE_OFDM_LOW_HZ = 750.0
RADE_OFDM_HIGH_HZ = 2200.0

DEFAULT_FREQUENCY = 432_150_000  # Hz, matches the TX app's default test frequency
FINE_TUNE_RANGE_HZ = 2_000  # +/- range of the fine-tune slider, same as the TX app

GAIN_MODES = ["manual", "slow_attack", "fast_attack", "hybrid"]
DEFAULT_GAIN_MODE = "slow_attack"
DEFAULT_MANUAL_GAIN_DB = 40.0
MANUAL_GAIN_RANGE_DB = (0.0, 73.0)  # AD9361 RX1 gain table range

DEFAULT_NF_GAIN = 1.0  # audio volume multiplier, applied after the demodulator

FFT_SIZE_PRESETS = [1024, 2048, 4096, 8192, 16384]
DEFAULT_FFT_SIZE = 1024

# --- Pluto RX libiio buffer size -- an OPTIONAL, RX-app-only tuning knob.
# Real throughput testing this session (direct-Ethernet-cabled Pluto+,
# Z7010 SoC) found the achievable clean sample rate is capped not by link
# speed (both ends confirmed Gigabit) but by this client-side libiio
# buffer size (iio.fmcomms2_source_fc32()'s 3rd arg): the app's original
# hardcoded 32768 (0x8000) plateaus around ~9-10 Msps, while 262144 (8x)
# reliably reached ~11-12 Msps -- a real ~20-25% gain, matching a
# documented LibreSDR-project tuning tip. Larger still (1M/2M) measured
# WORSE, confirming a real sweet spot, not "bigger is always better".
#
# Deliberately NOT changed as the default, and deliberately NOT exposed
# in pluto_tx at all: a larger buffer trades throughput for latency
# (more samples held before the flowgraph sees them), which is fine for
# passive RX (audio/waterfall) but was never measured against pluto_tx's
# own safety-critical PTT/E-STOP/M17-EOT-tail timing -- left untouched
# there by explicit request.
PLUTO_RX_BUFFER_SIZE_PRESETS = [32768, 65536, 131072, 262144, 524288]
PLUTO_RX_BUFFER_SIZE_DEFAULT = 32768  # == today's hardcoded 0x8000, unchanged -- opt-in only

# --- Waterfall widget (pyqtgraph) -------------------------------------------
WATERFALL_HISTORY_ROWS = 200  # rolling time-history depth of the waterfall image
WATERFALL_POLL_INTERVAL_MS = 33  # ~30 Hz GUI-side poll of fft_probe's latest row
FFT_COMPUTE_RATE_HZ = 30  # fft_probe's own compute throttle, independent of poll rate/sample rate
WATERFALL_WINDOW = window.WIN_BLACKMAN_hARRIS  # matches GNU Radio's own qtgui.waterfall_sink_c default window
WATERFALL_COLORMAP = "viridis"
WATERFALL_DB_RANGE = (-80.0, 0.0)  # fixed color/Y-axis levels (no per-frame autoscale)

# --- RADE V1 "Auto Fine-Tune" (rade_autotune.py + gui.py's _autotune_*
# state machine) -- see the RADE auto-tune plan for the full rationale.
# TARGET_SNR_DB is relative to the window's own local noise floor, NOT an
# absolute FftProbe dB reading -- a real bug found calibrating this on real
# hardware: FftProbe's dB scale is unnormalized FFT output, so its absolute
# magnitude shifts by tens of dB with fft_size/zoom (measured: peak_db=53
# at zoom=16 for a signal that would read far lower at zoom=1). An absolute
# target read a genuinely overloaded RTL-SDR front-end as needing MORE
# gain. peak-floor cancels that offset out -- see
# rade_autotune.measure_peak_snr_db()'s docstring.
# TARGET_SNR_DB/DWELL_S got a first real-hardware validation pass this
# session (Pluto TX -> RTL-SDR RX, both real, at 432.15MHz): a real-hardware
# SNR-vs-gain sweep found this RTL-SDR's own peak SNR sits right around
# 29dB at a non-overloaded TX power, and a full deliberately-mistuned
# (+900Hz, 5dB gain, true optimum ~30dB) end-to-end Auto Fine-Tune run
# reached genuine RADE sync via the Stage 2 frequency sweep. Still coarse --
# only one antenna/distance/TX-power combination tested, and the reached
# lock was marginal (SNR ~-4dB, brief) -- but no longer a pure guess.
RADE_AUTOTUNE_TARGET_SNR_DB = 30.0
RADE_AUTOTUNE_SNR_TOLERANCE_DB = 5.0
RADE_AUTOTUNE_MAX_GAIN_STEP_DB = 20.0  # per-iteration clamp, safety against wild single-step jumps
RADE_AUTOTUNE_DWELL_S = 4.0
# Widened/densified this session (was (100,-100,200,-200,400,-400,800,-800),
# 8 points, big gaps between 400 and 800): 12 points, denser at the low end,
# reaches further out (+-1200Hz vs +-800Hz) -- raises the worst-case Stage-2
# frequency-sweep time from 32s to 48s (12*DWELL_S), total worst case (Stage
# 1 + freq sweep + gain sweep) from ~54s to ~70s. A deliberately bounded
# increase, not an open-ended/continuous search -- see rade_autotune plan.
RADE_AUTOTUNE_FREQ_SWEEP_STEPS_HZ = (100, -100, 200, -200, 300, -300, 500, -500, 800, -800, 1200, -1200)
RADE_AUTOTUNE_GAIN_SWEEP_STEPS_DB = (-6.0, 6.0, -12.0, 12.0)
RADE_AUTOTUNE_MIN_ZOOM = 16
RADE_AUTOTUNE_SETTLE_S = 0.4  # after a zoom/frequency/gain change, before trusting a fresh FFT row
# Radius around the search center EXCLUDED from the noise-floor estimate
# (measure_noise_floor_db()/measure_peak_snr_db()/estimate_signal_center()'s
# internal floor) -- a real bug found and fixed this session: both used to
# take the floor's percentile from the SAME +-FINE_TUNE_RANGE_HZ (2kHz)
# window used to find the peak, which RADE's own ~1.45kHz-wide OFDM signal
# (RADE_OFDM_LOW_HZ/HIGH_HZ above) can dominate, inflating the "floor" and
# biasing both the centering estimate and the gain-SNR correction. Set to
# RADE's half-bandwidth (~725Hz) plus margin for not-yet-corrected mistuning
# (matches the sweep's largest step above) -- NOT a recalibration of
# TARGET_SNR_DB itself, which may need a fresh real-hardware pass now that
# the floor is measured correctly (see README ToDo).
RADE_AUTOTUNE_NOISE_EXCLUDE_HZ = 1500.0

# --- File Broadcast (repetitive file-broadcast mode) -- MIRRORED copy of
# pluto_tx/config.py's FILEBROADCAST_* PHY block, MUST stay byte-for-byte
# in sync with it (see that file's own comment for the full rationale and
# Phase 0 real-hardware provenance of these exact numbers). A mismatch here
# silently breaks the real link -- the demodulator would assume the wrong
# deviation/working_rate for what the transmitter is actually sending.
FILEBROADCAST_SYMBOL_RATE_HZ = 100_000.0
FILEBROADCAST_DEVIATION_HZ = 50_000.0  # h=1 (deviation = symbol_rate/2)
FILEBROADCAST_BT = 0.35
FILEBROADCAST_SPS = 5
FILEBROADCAST_WORKING_RATE_HZ = FILEBROADCAST_SYMBOL_RATE_HZ * FILEBROADCAST_SPS  # 500,000 Hz
# digital.gfsk_demod's own symbol_sync_ff loop bandwidth -- its DEFAULT
# (0.175) reproducibly cycle-slips mid-transmission at 100kbaud on real
# hardware (Phase 0 finding); 0.005 gave BER well under 0.2%, twice,
# reproducibly. RX-side only -- this is the copy that's actually read.
FILEBROADCAST_GAIN_MU = 0.005
FILEBROADCAST_CHUNK_SIZE = 64  # see pluto_tx/config.py's sizing rationale (frame survival vs. overhead)
FILEBROADCAST_MAX_FILENAME_LEN = 64

# --- PSK31 RX (BPSK31 keyboard-to-keyboard chat digimode) -- MIRRORED copy
# of pluto_tx/config.py's PSK31_* PHY block (symbol rate/tone defaults,
# MUST stay in sync -- see that file's own comment for the full real-
# hardware provenance), plus RX-only demod-chain/AFC constants that have
# no TX-side equivalent (pluto_tx never receives).
PSK31_SYMBOL_RATE_HZ = 31.25
# 2500Hz -- kept in sync with pluto_tx/config.py's own PSK31_DEFAULT_TONE_HZ
# (see its comment for the real-hardware mirror-image finding that
# motivated raising it from 1500Hz): this RX default is the search center
# psk31_afc_step() starts from, so it must match what a fresh TX session
# actually sends by default, or a fresh RX session wouldn't find it at all
# (the real signal would sit outside PSK31_AFC_SEARCH_RADIUS_HZ of a
# stale 1500Hz assumption).
PSK31_DEFAULT_TONE_HZ = 2500.0
PSK31_TONE_RANGE_HZ = (300.0, 2700.0)
# Approximate BPSK31 occupied bandwidth, for the waterfall's PSK31 marker
# overlay ONLY -- not a precise RF bandwidth model. A small multiple of the
# real 31.25 baud symbol rate, so the overlay is visually proportionate to
# the actual narrow signal instead of reusing an unrelated, much wider
# FM/SSB/RADE demod-band constant (see gui.py's _sync_waterfall()).
PSK31_DISPLAY_BANDWIDTH_HZ = PSK31_SYMBOL_RATE_HZ * 4

# Fixed working rate the demod chain (Costas loop/symbol_sync_ff) runs at,
# decimated down from DEMOD_IF_RATE via psk31_tone_filter. Raised from the
# originally-verified 1000Hz to 2000Hz as part of the Phase 4.5 continuous-
# drift rework (see PSK31_XLATE_CUTOFF_HZ's own comment): a higher working
# rate gives psk31_tone_filter's own decimated Nyquist (working_rate/2) more
# headroom for a wider passband without aliasing, AND (a free side effect,
# since digital.costas_loop_cc's loop_bw is a normalized rad/sample value)
# doubles the Costas loop's own default +-1.0 rad/sample frequency-capture
# range from roughly +-159Hz to +-318Hz for the same PSK31_LOOP_BW. sps =
# 2000/31.25 = 64, still a comfortable margin (matches FILEBROADCAST_SPS's
# own sps>=4 lesson).
PSK31_WORKING_RATE_HZ = 2_000.0
# psk31_tone_filter's low-pass cutoff/transition (Hz).
#
# Phase 0 (original, narrow-passband + periodic-external-retune AFC
# design): 100Hz/80Hz, real-hardware-verified against that era's
# characterized drift (slow, over MINUTES, between separate short test
# sessions). A WIDER passband (250/150Hz) was tried then and made results
# WORSE (more admitted noise hurt Costas lock more than the wider margin
# helped).
#
# Phase 4.5 (this rework): real end-to-end testing with the actual app
# classes found a DIFFERENT, much faster problem Phase 0 never
# characterized -- a CONTINUOUS, near-linear intra-message drift of
# roughly 8-19Hz/s, ~400-500Hz over a single ~25s message with no sign of
# leveling off. The AFC's own external-retune mechanism (psk31_afc_step())
# now deliberately stops retuning once a message is actively decoding (see
# its own docstring) and instead relies on the Costas loop's own
# continuous closed-loop tracking to absorb further drift for the REST of
# a message once locked -- which requires the passband to stay wide enough
# to contain a full message's worst-case drift on its own, unlike Phase
# 0's design where the filter only ever needed to bracket a slow, between-
# message offset. Widened to 700Hz/200Hz (stopband edge at 900Hz, still
# comfortably under the new 2000Hz working rate's 1000Hz Nyquist) as the
# Phase 4.5 starting candidate -- Phase 0's own "wider is worse" finding
# was measured against the OLD, slower-drift problem shape and a design
# that still retuned mid-message; it does not necessarily still hold here
# and needs its own real-hardware re-verification, not a blind carry-over
# in either direction.
PSK31_XLATE_CUTOFF_HZ = 700.0
PSK31_XLATE_TRANS_HZ = 200.0
# Costas loop / symbol_sync_ff loop bandwidth (radians/sample). Phase 0
# real-hardware-verified this exact value against ITS OWN narrow-passband/
# periodic-retune design (a 3x SLOWER loop, mirroring the direction that
# fixed FILEBROADCAST_GAIN_MU's own GFSK cycle-slip problem, made results
# WORSE then). Left unchanged as the Phase 4.5 starting candidate -- the
# working-rate increase above already doubles its EFFECTIVE Hz bandwidth
# for free (loop_bw is normalized to working_rate), which may be enough
# continuous-tracking headroom on its own; re-verify on real hardware
# before tuning this independently.
PSK31_LOOP_BW = 2 * math.pi / 30

# --- PSK31 AFC (frequency-drift compensation) -----------------------------
# Real over-the-air testing (Phase 0) found a substantial, CONTINUOUSLY
# DRIFTING TX/RX frequency offset between this project's own Pluto+RTL-SDR
# pairing -- both a slow, between-message drift (Phase 0, >150Hz over ~40
# minutes) and (Phase 4.5, found once real end-to-end testing with the
# actual app classes was tried, not just standalone scripts) a much
# faster, sustained, CONTINUOUS intra-message drift (~4-19Hz/s, ~400-500Hz
# over a single ~25s message, never leveling off). Design (current, Phase
# 4.5): a SECOND, dedicated FftProbe (psk31_afc_probe, see flowgraph.py)
# taps if_filter's output at real frequency resolution (~12Hz/bin at
# DEMOD_IF_RATE=50kHz here) independent of the main waterfall's own zoom/
# display state; periodically, rade_autotune.estimate_signal_center()
# (REUSED, not reimplemented -- this mode's own plan explicitly
# recommended reusing RADE's Auto Fine-Tune machinery) finds the tone's
# actual current position, anchored at the operator's own stable nominal
# (psk31_tone_hz, not a walking center -- see psk31_afc_step()'s own
# docstring for why). AdvancedRxFlowgraph.psk31_afc_step() only retunes
# psk31_tone_filter when that estimate differs from the current tuning by
# more than the LARGE PSK31_AFC_DEADBAND_HZ below -- ordinary continuous
# drift stays under the deadband and is deliberately left to the Costas
# loop's own NCO to track on its own (see PSK31_XLATE_CUTOFF_HZ/
# PSK31_LOOP_BW above for the wide-passband/fast-loop half of this design
# that makes that possible); external retuning becomes a rare, coarse
# correction (e.g. the initial gap between nominal and the real drifted
# position at the start of a session), not a constant fight with the
# loop's own tracking. Two smaller-deadband/gated-retune variants were
# tried first and rejected -- see psk31_afc_step()'s own docstring for the
# real-hardware findings that ruled each one out. This design (wide
# passband + fast loop + large-deadband coarse retuning) is the one
# CONFIRMED via a real over-the-air test with the actual
# PlutoTxFlowgraph/AdvancedRxFlowgraph classes (not a standalone script)
# to correctly decode a full test message end-to-end.
PSK31_AFC_FFT_SIZE = 4096
PSK31_AFC_COMPUTE_RATE_HZ = 20  # FftProbe's own internal compute throttle
PSK31_AFC_POLL_INTERVAL_S = 2.0  # how often gui.py's _poll_psk31() actually calls psk31_afc_step()
PSK31_AFC_SEARCH_RADIUS_HZ = 600.0  # margin above the largest drift actually observed (~430Hz) this session
# Real, IMPORTANT finding from wiring this up against real Pluto hardware
# (432.15MHz, no PSK31 signal transmitting -- just real RF background):
# RADE_AUTOTUNE's own default threshold_db=6.0 (a sensible choice for
# RADE's own use, an ALWAYS-present-while-tuned-in wide OFDM signal) is
# FAR too permissive here -- with no real PSK31 tone present at all,
# estimate_signal_center() still returned a confident-looking estimate at
# 6/10/15/20dB, repeatedly, across a real live 8-poll test (this is
# actually mostly a property of the estimator itself, not noisy hardware:
# a power-weighted centroid computed over a symmetric search window with
# no real single dominant peak systematically regrades toward the
# window's own center, i.e. `freq_hz`, on roughly-flat/symmetric noise --
# RADE's own Auto Fine-Tune masks this by only ever trusting an estimate
# that emerges from its own coarse frequency SWEEP with dwell time, not a
# single bare snapshot; PSK31's simpler one-shot use of the same function
# needed its own real threshold instead). 25.0dB reliably rejected the
# real background in all 8 live samples (est=None every time) while a
# separate deliberately weak synthetic test signal (heavy added noise,
# ~44dB local peak SNR -- comfortably weaker than the ~88dB seen with an
# actual strong real over-the-air PSK31 signal in Phase 0 testing) was
# still found and correctly located at this same threshold. Residual risk
# accepted deliberately, not fixed further: a genuinely strong, STABLE
# real interferer landing inside the search window could in principle
# still fool this -- this mode is a best-effort, operator-supervised
# receive aid (the operator sees the transcript and can always retune
# set_psk31_tone_hz() manually), the same "passive, best-effort" posture
# already established for File Broadcast's RX side, not a claim of a
# bulletproof automatic system.
PSK31_AFC_THRESHOLD_DB = 25.0
# LARGE deadband, by design (Phase 4.5) -- see PSK31_AFC section's own
# comment above and psk31_afc_step()'s docstring for the full real-
# hardware story. The original value (15Hz) forced a retune on almost
# every poll given real continuous drift, fighting the Costas loop's own
# tracking; 120Hz confirmed (real over-the-air test, actual app classes)
# to let the loop absorb ordinary intra-message drift on its own while
# still catching genuinely large offsets (e.g. a session's starting gap
# from the operator's nominal).
PSK31_AFC_DEADBAND_HZ = 120.0

# --- RTTY RX (2-tone FSK Baudot digimode) -- MIRRORED defaults of
# pluto_tx/config.py's RTTY_* block (mark/shift/baud presets, MUST stay
# in sync), plus RX-only demod-chain/AFC constants with no TX-side
# equivalent. Unlike PSK31 (fixed 31.25 baud, Costas-loop phase-tracked),
# RTTY's shift/baud are real runtime-adjustable settings and its
# quadrature-discriminator demod has no continuous carrier-tracking loop
# of its own -- see pluto_advanced_rx/flowgraph.py's RTTY branch and
# rtty_deframer.py's own docstring for why it uses open-loop UART-style
# framing instead of PSK31's symbol_sync_ff approach.
RTTY_MARK_HZ_DEFAULT = 2125.0
RTTY_MARK_HZ_RANGE = (300.0, 2700.0)
RTTY_SHIFT_HZ_DEFAULT = 170.0
RTTY_SHIFT_HZ_PRESETS = (170.0, 425.0, 850.0)
RTTY_BAUD_RATE_DEFAULT = 45.45
RTTY_BAUD_RATE_PRESETS = (45.45, 50.0, 75.0, 100.0)
RTTY_STOP_BITS = 1.5

# Fixed working rate rtty_band_filter decimates DEMOD_IF_RATE down to --
# unlike PSK31_WORKING_RATE_HZ (tied to the Costas loop's own sps
# requirement), this is sized purely to comfortably contain the widest
# supported shift preset (850Hz) plus filter guard band while staying
# far above what even the fastest baud preset (100) needs for the RX
# deframer's own oversampled edge-timing arithmetic (5000/100=50
# samples/bit, vs. 5000/45.45=~110 at the slowest -- both comfortably
# oversampled). Deliberately NOT tied to baud_rate the way an earlier
# draft of this design considered -- keeping it fixed means a baud-rate
# change only updates cheap arithmetic inside rtty_deframer.py's
# set_baud_rate(), no GNU Radio block reconfiguration at all.
RTTY_WORKING_RATE_HZ = 5_000.0
# rtty_band_filter's cutoff/transition are sized from the CONFIGURED
# shift (cutoff = shift_hz/2 + this margin) via set_rtty_shift_hz()'s
# set_taps() retune, the same already-proven live-retune pattern as
# set_fm_demod_width()/set_ssb_demod_width()/set_baseband_width() --
# see flowgraph.py. 300Hz gives comfortable margin at both the
# narrowest (170Hz) and widest (850Hz) shift presets while staying well
# under RTTY_WORKING_RATE_HZ/2's 2500Hz Nyquist even at 850Hz shift
# (cutoff=725Hz there).
RTTY_FILTER_GUARD_HZ = 300.0
# rtty_lowpass (smooths the quadrature-demod discriminator output before
# slicing) -- a single FIXED cutoff/transition sized for the FASTEST
# baud preset (100, needing the widest passband), not retuned per baud
# change: given RTTY_WORKING_RATE_HZ's large oversampling margin at
# every baud preset, a filter wide enough for 100 baud is never too
# narrow for a slower one, just marginally less noise-rejecting -- an
# acceptable, simpler trade-off vs. adding a second baud-triggered
# GNU Radio retune alongside rtty_band_filter's shift-triggered one.
RTTY_LOWPASS_CUTOFF_HZ = 150.0
RTTY_LOWPASS_TRANS_HZ = 100.0

# --- RTTY AFC (frequency-drift compensation) -- structurally mirrors
# PSK31_AFC's own design (a dedicated FftProbe + rade_autotune.
# estimate_signal_center(), see that section's comment above for the
# full mechanism), with two real differences the Plan-review for this
# feature specifically called out:
# 1. RTTY has TWO tones with data-dependent relative energy (idle/LTRS-
#    heavy vs. FIGS/digit-heavy traffic shifts which tone dominates) --
#    a single wide centroid search across both tones (PSK31's approach)
#    would drift toward whichever tone is momentarily more common, not
#    the true mark/space midpoint. rtty_afc_step() therefore runs TWO
#    separate narrow-radius searches (one near mark_hz, one near the
#    current space frequency) and averages the two found peaks.
# 2. RTTY's occupied span varies 3x across the shift presets (170 vs
#    850Hz), so the search radius is a function of the CONFIGURED shift
#    (shift_hz/2 + this margin), not a static constant like PSK31's.
RTTY_AFC_FFT_SIZE = 4096
RTTY_AFC_COMPUTE_RATE_HZ = 20
# More frequent than PSK31_AFC_POLL_INTERVAL_S (2.0s) and a smaller
# deadband than PSK31_AFC_DEADBAND_HZ (120Hz) -- deliberate, NOT a
# blind carry-over: PSK31's Costas loop continuously tracks ordinary
# drift on its own between AFC polls, so PSK31's AFC only needs to catch
# rare, large corrections. RTTY's quadrature-discriminator demod has no
# equivalent continuous tracking at all, so AFC is the ONLY mechanism
# correcting drift here -- starting candidates only, not yet real-
# hardware-verified (unlike PSK31_AFC_THRESHOLD_DB/DEADBAND_HZ's own
# documented real over-the-air calibration history above).
RTTY_AFC_POLL_INTERVAL_S = 1.0
RTTY_AFC_SEARCH_MARGIN_HZ = 300.0  # search radius = shift_hz/2 + this
RTTY_AFC_THRESHOLD_DB = 25.0  # starting point, mirrors PSK31_AFC_THRESHOLD_DB's real-hardware-verified value
RTTY_AFC_DEADBAND_HZ = 60.0
