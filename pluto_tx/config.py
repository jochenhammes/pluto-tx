"""Shared constants for the PlutoSDR TX app."""

DEFAULT_URI = "ip:plutoplus.local"

_URI_SCHEMES = ("ip:", "usb:", "local:", "xml:")


def normalize_uri(text: str) -> str:
    """Accept a bare hostname/IP (e.g. 'plutoplus.local', '192.168.1.50') and
    turn it into a libiio network context URI ('ip:...'). An already explicit
    scheme (ip:/usb:/local:/xml:) is left untouched, so typing 'usb:1.5.5'
    still targets a specific USB device directly -- useful when more than one
    Pluto is reachable (e.g. two on the same LAN, or one on USB + one on
    Ethernet)."""
    text = text.strip()
    if not text or text.startswith(_URI_SCHEMES):
        return text
    return f"ip:{text}"

# AD9361 TX attenuation range (dB, 0 = max power, more negative = less power).
# Stay here (not in devices/pluto.py with the rest of the Pluto-specific TX
# device constants) because safety.py -- deliberately independent of the
# device abstraction layer, see devices/pluto.py's module docstring --
# already imports them from here, and devices/pluto.py importing safety.py
# means safety.py importing back from devices/pluto.py would be circular.
MIN_ATTEN = -89.75
MAX_ATTEN = 0.0

# Audio front end.
AUDIO_RATE = 48_000

DEFAULT_FREQUENCY = 432_150_000  # Hz, matches today's verified carrier test; valid for every device backend
FINE_TUNE_RANGE_HZ = 2_000  # +/- range of the fine-tune spinbox

# Displayed span of the TX waterfall (flowgraph.py's waterfall_zoom_resampler
# decimates the device's full quad_rate down to this before the qtgui
# waterfall sink) -- deliberately much narrower than any device's quad_rate,
# so the actual modulated signal (a few kHz to ~10kHz wide for any mode this
# app supports) fills a meaningful fraction of the display instead of being
# a barely visible sliver, with the resolution improvement coming from the
# narrower span (same 1024-point FFT, far fewer Hz/bin) rather than a bigger
# FFT computed across the full bandwidth.
WATERFALL_ZOOM_BANDWIDTH_HZ = 50_000

# NF (audio) band-pass filter presets, (f_lo, f_hi, trans_width) in Hz.
NF_FILTER_PRESETS = {
    "FM": (300.0, 3000.0, 300.0),
    "SSB": (300.0, 2700.0, 300.0),
}

FM_DEVIATION_HZ = 2500.0  # narrowband voice FM default
DEFAULT_NF_GAIN = 1.0  # manual audio drive multiplier, applied after the compressor

# --- NF dynamics processing: noise gate, compressor, smooth limiter -------
# Signal order: ptt_mute -> nf_filter -> gate -> agc -> compressor ->
# nf_gain -> limiter_smooth -> limiter (existing hard-clip safety backstop,
# unchanged). See pluto_tx/dynamics.py for the algorithm and pluto_tx/
# flowgraph.py for the wiring. Values below are starting points from general
# broadcast-audio convention, not yet re-validated against this project's
# actual mic/WAV levels -- treat as a reasonable default, not gospel.
GATE_THRESHOLD_DB = -50.0
GATE_ALPHA = 0.0001  # analog.pwr_squelch_ff's internal averaging filter gain
GATE_RAMP_SAMPLES = 480  # ~10ms at AUDIO_RATE, sinusoidal attack/release ramp
GATE_BYPASS_THRESHOLD_DB = -100.0  # pwr_squelch_ff has no enable/disable API; this
# floor threshold is the bypass trick -- effectively never gates (see set_gate_enabled)

COMPRESSOR_THRESHOLD_DB = -18.0
COMPRESSOR_RATIO = 4.0  # whole number: the GUI's ratio slider is integer-stepped
COMPRESSOR_KNEE_DB = 6.0
COMPRESSOR_ATTACK_MS = 8.0
COMPRESSOR_RELEASE_MS = 120.0

LIMITER_THRESHOLD_DB = -3.0
LIMITER_RATIO = 20.0
LIMITER_KNEE_DB = 1.0
LIMITER_ATTACK_MS = 1.0
LIMITER_RELEASE_MS = 60.0

# --- M17 digital voice (optional -- needs gr-m17, see install-m17.sh) ------
# Parameters below are taken from gr-m17's own real reference flowgraphs
# (examples/transmitterPLUTOSDR.grc), not re-derived from the M17 spec, and
# verified this session by an actual offline m17_coder->m17_decoder
# round-trip (encoded test bytes decoded correctly, including src/dst
# callsigns recovered from the LSF). The M17 branch deliberately bypasses
# nf_filter/gate/agc/compressor/nf_gain/limiter_smooth/limiter (the analog-
# modulation dynamics chain) -- it taps ptt_mute's output directly. Codec2
# has its own internal level handling; a broadcast-style compressor ahead of
# a low-bitrate vocoder is more likely to hurt intelligibility than help.
M17_CODEC2_RATE = 8_000  # codec2's fixed input rate
M17_SYMBOL_RATE = 4_800  # M17's native baud rate
M17_RRC_ALPHA = 0.5
M17_RRC_NTAPS = 81
M17_RRC_SPS = 10  # samples/symbol after RRC pulse shaping
M17_BASEBAND_RATE = M17_SYMBOL_RATE * M17_RRC_SPS  # 48,000 Hz, post-RRC/pre-FM-mod
M17_DEVIATION_HZ = 800.0  # FM deviation for the outer (+-1) symbol level

M17_DEFAULT_DST_CALLSIGN = "@ALL"  # standard M17 broadcast destination
M17_CALLSIGN_MAX_LEN = 9  # m17_coder truncates to this; GUI should validate the same

# unkey_ptt() in M17 mode can't cut RF instantly like FM/SSB -- the encoder
# needs a short tail (>= 1 final frame + eot_cnt EOT frames, ~80ms minimum
# with defaults) to actually transmit a clean EOT so the receiver doesn't
# hang. M17_EOT_HOLD_S is how long the GUI waits before lowering
# attenuation; M17_EOT_HOLD_WATCHDOG_S is a hard ceiling in case something
# goes wrong. Both are conservative placeholders -- gr-m17's real-hardware
# tail latency (scheduling/USB/IIOD buffering on top of the ~80ms
# theoretical minimum) is unmeasured; needs calibration against a real PTT
# release, which requires explicit operator approval to test (see README).
M17_EOT_HOLD_S = 0.4
M17_EOT_HOLD_WATCHDOG_S = 2.0

# --- FreeDV 2020/2020B digital voice (needs libcodec2 with LPCNet support --
# already a transitive dependency of the `gnuradio` apt package via
# libgnuradio-vocoder, confirmed this session; no separate install script
# needed, unlike M17). freedv_tx() outputs a plain modulated AUDIO waveform
# (not IQ symbols like M17), so the FreeDV branch reuses this app's existing
# Hilbert-based USB modulation technique (its own dedicated instances) --
# see pluto_tx/flowgraph.py. Like M17, bypasses the analog dynamics chain
# (nf_filter/gate/agc/compressor/nf_gain/limiter_smooth/limiter): compressing/
# AGC-ing an already-modulated OFDM waveform would corrupt it.
FREEDV_SPEECH_RATE = 16_000  # freedv_get_speech_sample_rate(), fixed for 2020/2020B
FREEDV_MODEM_RATE = 8_000  # freedv_get_modem_sample_rate(), fixed for 2020/2020B
# Mirrors freedv_ctypes.FREEDV_MODE_2020 (kept as a plain int here so config.py
# stays free of the gnuradio/ctypes import, matching M17's constants above).
FREEDV_DEFAULT_MODE = 8  # FREEDV_MODE_2020 (vs. FREEDV_MODE_2020B = 16)

FREEDV_CALLSIGN_MAX_LEN = 9  # matches M17_CALLSIGN_MAX_LEN; reliable_text itself allows more

# --- Digitext (waterfall-text digimode, pluto_tx/digitext.py) --------------
# Renders ASCII text to a pixel bitmap (Pillow) and encodes it via direct
# additive sine-tone synthesis so ANY receiver's FFT/waterfall shows the
# text, right-side up and readable -- see digitext.py's module docstring for
# the exact column->frequency/row->time mapping and the horizontal/vertical
# layout distinction. Like M17/FreeDV, bypasses the analog dynamics chain
# (nf_filter/gate/agc/compressor/nf_gain/limiter_smooth/limiter) and, unlike
# ALL of them, doesn't tap ptt_mute's mic/file audio at all -- the "source"
# is the typed text, not live audio, so there's nothing upstream to mute.
#
# Second real-hardware round this session: the FIRST implementation (an
# inverse-STFT/overlap-add encoding, with DIGITEXT_FRAME_LEN/_HOP_LEN/
# _ROW_HOLD_FRAMES -- since removed) turned out to only reconstruct cleanly
# when a receiver's OWN analysis FFT happened to match those internal
# parameters -- verified offline this session: the same signal looked
# reasonable under matching analysis parameters but degraded into
# unrecognizable noise under an independent, mismatched FFT size, which is
# exactly what any real, independent receiver (SDR++, GQRX, RTL-SDR) is.
# Replaced with direct additive tone synthesis (see digitext.py), which has
# no such dependency. A second finding: many simultaneous close-together
# tones (a solid letter stroke needs several adjacent columns) beat/
# interfere and can look patchy in a short observation window -- addressed
# by DIGITEXT_COL_DOWNSAMPLE (fewer, coarser columns) and generous
# DIGITEXT_HZ_PER_COL/_ROW_DWELL_S (wide separation, long per-row dwell, so
# a real continuously-scrolling waterfall gets many independent looks per
# row instead of depending on any single snapshot).
#
# All values below are again a first estimate for the NEW encoding,
# informed by this session's offline diagnosis (not yet a second real
# on-air confirmation) -- see README's Digitext section for the full
# writeup and what's still open.
DIGITEXT_DEFAULT_TEXT = "DA2JH"
DIGITEXT_MAX_TEXT_LEN = 40  # a sanity cap only (avoid a runaway-long message) -- there is
# deliberately no bandwidth cap/warning any more (removed by explicit request); the GUI's live
# estimate label still shows the projected bandwidth, it just no longer blocks or flags anything.
DIGITEXT_FONT_SIZE_PX = 16
DIGITEXT_SAMPLE_RATE = AUDIO_RATE  # reused -- goes through the same Hilbert/SSB chain as FreeDV
DIGITEXT_COL_DOWNSAMPLE = 1  # 1 = no downsampling -- a real-hardware test this session found that
# max-pooling adjacent columns (a previous value of 2) merges thin strokes/gaps together, destroying
# letter shapes outright (verified: a binary-thresholded render of the downsampled bitmap was
# unreadable even though the SAME text at native resolution was clearly "DA2JH") -- bandwidth is
# instead managed via DIGITEXT_HZ_PER_COL and the operator's own zoom-factor choice (GUI, 1x-8x).
DIGITEXT_HZ_PER_COL = 45.0  # Hz between adjacent logical columns -- wide enough to resist inter-tone beating
DIGITEXT_ROW_DWELL_S = 0.15  # seconds each image row is held -- real feedback this session: at the
# original 0.35s, letters looked horizontally stretched/squashed on a real waterfall (12 rows * 0.35s
# = 4.2s tall vs. 50 cols * 45Hz = 2250Hz wide for "DA2JH" -- a very flat aspect ratio) and the
# operator asked for rows to play faster. Still long enough to give a real, continuously-scrolling
# waterfall multiple independent FFT looks per row (the original anti-beating reason for holding each
# row at all, see encode_bitmap_to_audio's docstring) -- just less of a margin than before. If beating/
# patchiness reappears on real hardware, raise this back up rather than reducing DIGITEXT_HZ_PER_COL,
# which exists for the same reason.
# Lowest frequency bin used -- a REAL bug found and fixed on real hardware
# this session: this used to be ~23Hz (2 bins), deep in hilbert_fc(401,
# WIN_HAMMING)'s poor-response dead zone near DC (same transformer design
# as digitext_ssb_mod), which visibly collapsed the intended readable text
# into an unreadable, ultra-narrowband blob on a real receiver's waterfall.
# Raised from an initially-tried 300Hz to 4000Hz after a SECOND real-hardware
# finding: real over-the-air Pluto TX captures showed only ~13-18dB image
# (mirror) sideband suppression, vs. 60-94dB measured for the exact same
# digital signal at every stage up to the DAC in isolated chain tests. This
# proves the digital/software path is clean and the residual mirror image is
# an analog AD9361 TX IQ-imbalance characteristic of this hardware (already
# documented separately for FreeDV's OFDM signal in this project's history),
# not a Digitext bug -- so it cannot be fixed in software. 4000Hz instead
# pushes the real signal and its mirror far enough apart that they no longer
# visually overlap near the carrier; both real over-the-air tests (horizontal
# "DA2JH", vertical "DA2") confirmed the mirror is no longer visible even
# though the underlying ~13-18dB suppression ratio is unchanged.
DIGITEXT_MIN_FREQ_HZ = 4000.0  # default value AND the GUI offset slider's maximum (see below)
# Slider floor, by explicit request ("Slider... 4000Hz soll das Maximum sein, ich
# würde gerne kleinere Werte probieren") -- 300Hz is the lowest value this session
# already found safe from the DC dead zone above (bug 1), so it's a sensible floor
# for hands-on experimentation; going lower risks reintroducing that exact issue.
# The mirror-image trade-off (bug 5 above) is a spectrum of "how separated do the
# real signal and its mirror look", not a hard pass/fail line -- letting the
# operator slide down from the known-good 4000Hz and directly see the real result
# on their own receiver is more useful than a second guessed "safe" constant.
DIGITEXT_MIN_FREQ_HZ_FLOOR = 300.0
# True silence appended after the message -- belt-and-suspenders alongside
# the GUI's auto-unkey timer for a second REAL bug found this session: RF
# continued transmitting (looked like an unmodulated carrier) well past
# when the message should have ended. Guarantees the modulator's LAST
# input is genuine silence regardless of exact auto-unkey timing.
DIGITEXT_TAIL_S = 0.15
# Unconditional backstop, in addition to the primary GUI auto-unkey timer
# (digitext_duration_s, itself now includes DIGITEXT_TAIL_S) -- mirrors the
# already-established M17_EOT_HOLD_WATCHDOG_S pattern above ("a hard
# ceiling in case something goes wrong"). Forces unkey_ptt() this long
# AFTER the primary timer was supposed to have already fired, regardless of
# self.tb.keyed state -- calling unkey_ptt() again when already unkeyed is
# a harmless no-op.
DIGITEXT_AUTO_UNKEY_WATCHDOG_S = 2.0

# --- File Broadcast (repetitive file-broadcast mode, private-link only --
# see pluto-tx-file-broadcast-briefing.md and the plan at
# ~/.claude/plans/swirling-waddling-noodle.md). GFSK PHY parameters below
# are the exact, real-hardware-verified result of Phase 0's staged
# complexity/bandwidth test ladder (2026-09-11 session, direct-cable +
# 40dB-attenuator bench link, Pluto TX -> RTL-SDR RX): 100kbaud, h=1
# (deviation = symbol_rate/2), BT=0.35, with digital.gfsk_demod's OWN
# gain_mu explicitly set to 0.005 (NOT its default 0.175, which
# reproducibly cycle-slips mid-transmission at this rate -- see the plan's
# Phase 0 progress log) gave BER well under 0.2% on real hardware, twice,
# reproducibly. h=1 instead of the plan's originally-PLANNED h~=0.5: real
# testing found h=0.5 has a much tighter, not-yet-solved SNR/noise margin
# at this rate on this specific hardware pairing -- h~=0.5 was always
# flagged in the plan as a planning-stage estimate to be calibrated by the
# real test, not a fixed requirement.
#
# MUST STAY IN SYNC with pluto_advanced_rx/config.py's mirrored copy of
# these same PHY values -- this project's established pattern for TX/RX
# constants that must match exactly across the two independent app
# packages (see e.g. rade_ctypes.py's own per-package copies). A mismatch
# here silently breaks the real link (wrong deviation/rate assumed by the
# demodulator), not a loud error.
FILEBROADCAST_SYMBOL_RATE_HZ = 100_000.0
FILEBROADCAST_DEVIATION_HZ = 50_000.0  # h=1 (deviation = symbol_rate/2)
FILEBROADCAST_BT = 0.35
# samples/symbol: gives working_rate = SYMBOL_RATE*SPS = 500,000Hz, which
# divides Pluto's fixed 2.5MHz TX rate cleanly (interpolation=5) -- the
# EXACT sps/working_rate combination Phase 0's final real-hardware test
# verified (>=8 taps/branch in the polyphase TX resampler, the confirmed
# root cause of Stage 4's original failure -- see the plan). Do not change
# this without re-verifying taps/branch against whatever TX device's actual
# quad_rate is in use.
FILEBROADCAST_SPS = 5
FILEBROADCAST_WORKING_RATE_HZ = FILEBROADCAST_SYMBOL_RATE_HZ * FILEBROADCAST_SPS  # 500,000 Hz
# RX-only (digital.gfsk_demod's symbol_sync_ff loop bandwidth) -- listed
# here anyway (TX never reads it) purely so both config.py copies carry the
# complete, matched parameter set in one place, per the "must stay in sync"
# note above.
FILEBROADCAST_GAIN_MU = 0.005

# --- Frame format sizing: CHUNK_SIZE trades per-frame overhead (14 fixed
# bytes: SYNC_WORD not counted -- see filebroadcast.py -- + type/file_id/
# offset/length/crc16) against per-frame SURVIVAL PROBABILITY at the real
# measured link BER (a single bit error anywhere in a frame fails its
# CRC-16, discarding the whole frame -- expected/designed-for, since the
# round-robin rotation retries every chunk repeatedly, but a smaller chunk
# obviously fails less often per attempt). At CHUNK_SIZE=200 (an earlier,
# unconsidered default) and Phase 0's real measured BER range
# (0.06%-0.2%), a 214-byte/1712-bit frame's survival probability is only
# ~11-36% per rotation pass -- computed (not guessed) as
# (1-ber)**(frame_bytes*8). CHUNK_SIZE=64 (78-byte/624-bit frames) raises
# that to ~44-69% over the same BER range, a real, worthwhile improvement
# for how fast a file actually completes, at a modest overhead cost
# (14/64=22% vs 14/200=7%) -- picked deliberately, not a round number.
FILEBROADCAST_CHUNK_SIZE = 64
FILEBROADCAST_MAX_FILENAME_LEN = 64  # matches filebroadcast.MAX_FILENAME_LEN

# --- PSK31 (BPSK31 keyboard-to-keyboard chat digimode) -- see
# pluto_tx/psk31.py and the plan at ~/.claude/plans/swirling-waddling-noodle.md.
# Symbol rate is fixed by international PSK31 convention, not adjustable.
PSK31_SYMBOL_RATE_HZ = 31.25
# 1500Hz: a conventional PSK31 audio-tone offset, roughly centered in a
# typical SSB voice passband -- the real-hardware-verified value used
# throughout this mode's own Phase 0 PHY testing (2026-09-14 session).
PSK31_DEFAULT_TONE_HZ = 1500.0
# Sanity range for the operator's tone-offset control (mirrors
# DIGITEXT_MIN_FREQ_HZ_FLOOR/DIGITEXT_MIN_FREQ_HZ's own offset-slider
# idiom) -- a typical SSB voice passband.
PSK31_TONE_RANGE_HZ = (300.0, 2700.0)
PSK31_MAX_TEXT_LEN = 120  # a sanity cap only, mirrors DIGITEXT_MAX_TEXT_LEN
# Real, measured finding from this mode's own Phase 0 real-hardware
# testing: the receiver's Costas loop (carrier phase) and symbol_sync_ff
# (symbol timing) genuinely need real SETTLE TIME on real hardware to
# reach stable lock, not the tens-of-milliseconds scale GFSK's own
# preamble needed. Each idle preamble character costs exactly 3 bits
# (Varicode "1" for space + the "00" inter-character separator), i.e.
# `chars * 3 / PSK31_SYMBOL_RATE_HZ` real seconds -- Phase 0's own
# original write-up here MISCALCULATED this as ~1 bit/char (a 3x
# underestimate, caught and fixed during the Phase 4.5 rework below) --
# 16 chars is really ~1.54s (not "~0.5s"), 60 chars ~5.76s (not "~1.9s"),
# 150 chars ~14.4s (not "~4.8s").
#
# Phase 0 (original design: narrow +-100Hz static passband, small-deadband
# periodic external retune): 16 chars alone was NOT sufficient (looked
# adequate on one short test message, but a longer message revealed real,
# reproducible mid-transmission decode failure); 60 chars still failed;
# 150 chars worked reproducibly; shipped at 200 chars (~19.2s) as margin.
#
# Phase 4.5 (this rework: wide +-700Hz static passband, fast Costas loop,
# large-deadband coarse-only external retune -- see pluto_advanced_rx/
# config.py's own PSK31_AFC/PSK31_XLATE_CUTOFF_HZ/PSK31_LOOP_BW comments):
# the redesigned chain locks MUCH faster -- real over-the-air re-testing
# with the actual PlutoTxFlowgraph/AdvancedRxFlowgraph classes found even
# 16 chars (~1.54s) decoded a short message correctly, and 30 chars
# (~2.88s) AND 60 chars (~5.76s) both decoded a longer (70-char) message
# 100% correctly, reproducibly, using the real production AFC poll cadence
# (PSK31_AFC_POLL_INTERVAL_S=2.0s). Set to 60 chars -- 2x margin above the
# also-confirmed-working 30 chars, comfortably above one AFC poll interval
# so the coarse correction reliably lands during the preamble rather than
# the real payload -- a large reduction from the old 200 (19.2s), not a
# re-verification of that old value under the new design.
PSK31_PREAMBLE_CHARS = 60
PSK31_TAIL_S = 0.15  # mirrors DIGITEXT_TAIL_S's own real-hardware-motivated rationale
# Unconditional backstop watchdog, mirrors DIGITEXT_AUTO_UNKEY_WATCHDOG_S.
PSK31_AUTO_UNKEY_WATCHDOG_S = 2.0
# Real, measured, and important caveat: this specific Pluto+RTL-SDR
# pairing shows a REAL frequency offset between TX and RX that DRIFTS --
# both slowly between separate sessions (Phase 0: >150Hz over ~40 minutes)
# and, found later (Phase 4.5), continuously and much faster WITHIN a
# single message (~4-19Hz/s, ~400-500Hz over ~25s, never leveling off).
# PSK31's tiny occupied bandwidth makes this a serious problem a fixed/
# hardcoded correction cannot solve. There is deliberately NO frequency-
# correction constant here: pluto_advanced_rx (which actually receives)
# owns a real, working AFC -- see its own config.py's PSK31_AFC_*/
# PSK31_XLATE_CUTOFF_HZ/PSK31_LOOP_BW comments for the current design
# (reuses pluto_advanced_rx/rade_autotune.py's Auto Fine-Tune machinery
# for coarse acquisition, combined with a wide passband + fast Costas loop
# so the receiver's own carrier tracking absorbs ordinary continuous
# drift) and psk31_afc_step()'s docstring for the real-hardware findings
# that shaped it, including two earlier designs that were tried and
# rejected.

# German amateur radio band edges, used only for a non-blocking sanity
# warning in the GUI -- independent of which TX device backend is active.
DE_AMATEUR_BANDS_HZ = [
    ("2m", 144_000_000, 146_000_000),
    ("70cm", 430_000_000, 440_000_000),
    ("23cm", 1_240_000_000, 1_300_000_000),
]


def in_amateur_band(freq_hz: float):
    """Return the band name containing freq_hz, or None if out of all known bands."""
    for name, lo, hi in DE_AMATEUR_BANDS_HZ:
        if lo <= freq_hz <= hi:
            return name
    return None
