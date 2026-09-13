"""Shared constants for the PlutoSDR advanced RX app.

Deliberately a SELF-CONTAINED COPY of pluto_rx/config.py's RX-tuning values
(not an import) -- pluto_advanced_rx is a separate, independent app that
should be free to diverge from pluto_rx without risking the stable app, the
same relationship pluto_rx itself has to pluto_tx. Only genuinely generic,
non-RX-specific constants/helpers are still re-exported from pluto_tx.config.
"""
import math

from gnuradio.fft import window

from pluto_tx.config import DEFAULT_URI, DE_AMATEUR_BANDS_HZ, in_amateur_band, normalize_uri  # noqa: F401 (re-exported)

# RX baseband ("quadrature") rate presets -- these double as the waterfall's
# "zoom levels": each is the actual AD9361 RX sample rate (and, in "Auto"
# filter mode, the analog RX filter bandwidth the driver derives from it).
# Chosen as clean multiples of DEMOD_IF_RATE so the IF decimation stage below
# always lands on an integer decimation factor. Changing this requires a full
# flowgraph rebuild (GNU Radio FIR/resampler blocks can't change their
# decimation ratio at runtime) -- see gui.py's _on_bandwidth_changed.
#
# Extended beyond pluto_rx's [1M, 2.5M] on request, up to what actually
# produces data on real hardware -- each preset here was re-verified.
# 5M/8M/10M DO work but show real buffer overruns ("O" printed by GNU Radio)
# and audio underruns: the CURRENT ip:plutoplus.local / IIOD-network-protocol
# connection has a measured throughput ceiling around ~4.7-4.9 Msps (same
# finding pluto_rx/README already documents) -- expect choppy audio/gaps in
# the waterfall at these presets until a native USB backend is used instead
# (see README ToDo).
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

# --- Waterfall widget (pyqtgraph) -------------------------------------------
WATERFALL_HISTORY_ROWS = 200  # rolling time-history depth of the waterfall image
WATERFALL_POLL_INTERVAL_MS = 33  # ~30 Hz GUI-side poll of fft_probe's latest row
FFT_COMPUTE_RATE_HZ = 30  # fft_probe's own compute throttle, independent of poll rate/sample rate
WATERFALL_WINDOW = window.WIN_BLACKMAN_hARRIS  # matches pluto_rx's qtgui.waterfall_sink_c window
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
PSK31_DEFAULT_TONE_HZ = 1500.0
PSK31_TONE_RANGE_HZ = (300.0, 2700.0)

# Fixed working rate the demod chain (Costas loop/symbol_sync_ff) runs at,
# decimated down from DEMOD_IF_RATE via psk31_tone_filter -- sps =
# 1000/31.25 = 32, comfortable margin (matches FILEBROADCAST_SPS's own
# sps>=4 lesson). Real-hardware-verified during this mode's own Phase 0
# OTA testing (see pluto_tx/config.py's PSK31_PREAMBLE_CHARS comment).
PSK31_WORKING_RATE_HZ = 1_000.0
# psk31_tone_filter's low-pass cutoff/transition (Hz) -- narrow, matching
# PSK31's tiny ~50-60Hz occupied bandwidth. Real-hardware-verified: a
# WIDER passband (250/150Hz) was deliberately tried during Phase 0 OTA
# testing specifically to tolerate more frequency drift before the tone
# fell outside it -- this made results WORSE, not better (more admitted
# noise hurt Costas lock more than the wider margin helped). Do not widen
# this without re-verifying on real hardware; drift tolerance is instead
# handled by the AFC mechanism below, not by a wider static filter.
PSK31_XLATE_CUTOFF_HZ = 100.0
PSK31_XLATE_TRANS_HZ = 80.0
# Costas loop / symbol_sync_ff loop bandwidth (radians/sample), real-
# hardware-verified. A SLOWER loop (2*pi/300, mirroring the direction that
# fixed FILEBROADCAST_GAIN_MU's own GFSK cycle-slip problem) was tried
# during Phase 0 OTA testing and made results WORSE (more scattered
# errors) -- PSK31's loop-bandwidth-vs-jitter relationship is NOT a copy
# of GFSK's.
PSK31_LOOP_BW = 2 * math.pi / 100

# --- PSK31 AFC (continuous frequency-drift compensation) -----------------
# Real over-the-air testing during Phase 0 found a substantial,
# CONTINUOUSLY DRIFTING TX/RX frequency offset between this project's own
# Pluto+RTL-SDR pairing (observed drifting >150Hz over ~40 minutes, never
# settling -- see pluto_tx/config.py's PSK31_AUTO_UNKEY_WATCHDOG_S comment
# for the full finding) -- large enough that a fixed/hardcoded correction
# cannot track it, and (confirmed via an offline software test before this
# was wired into the real flowgraph, see flowgraph.py's own AFC comment)
# large enough to push the tone entirely outside psk31_tone_filter's own
# narrow +-100Hz passband, meaning digital.costas_loop_cc's own tracked
# residual (get_frequency()) is NOT a usable error signal on its own --
# it reads ~0 when there's no signal reaching it at all, not the true
# offset. Design: a SECOND, dedicated FftProbe (psk31_afc_probe, see
# flowgraph.py) taps if_filter's output at real frequency resolution
# (~12Hz/bin at DEMOD_IF_RATE=50kHz here) independent of the main
# waterfall's own zoom/display state; periodically,
# rade_autotune.estimate_signal_center() (REUSED, not reimplemented --
# this mode's own plan explicitly recommended reusing RADE's Auto
# Fine-Tune machinery) finds the tone's actual current position within
# PSK31_AFC_SEARCH_RADIUS_HZ of wherever it was last found, and
# AdvancedRxFlowgraph.psk31_afc_step() retunes psk31_tone_filter to match
# if the estimate differs from the current tuning by more than
# PSK31_AFC_DEADBAND_HZ -- an incremental, self-correcting search that
# tracks drift continuously across a whole receive session, confirmed
# (offline, synthetic +-180Hz-offset software test, a case the
# uncorrected/Costas-only approach could NOT recover) to find a tone's
# true center to well under 1Hz accuracy and recover a 100%-correct decode
# from a signal the static filter alone could not lock onto at all.
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
PSK31_AFC_DEADBAND_HZ = 15.0  # ignore sub-15Hz jitter in the estimate itself, only retune on a real drift-sized correction
