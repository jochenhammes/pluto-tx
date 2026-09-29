"""JS8 transmit side: frames -> waveform and slot planning. The message layer (text -> frames) is
js8_message.py, the frame -> tones step js8_phy.py; both are ports of JS8Call v2.5.2 (docs/js8/SPEC.md).

Waveform: plain continuous-phase 8-FSK like JS8Call's Modulator (SPEC 1.2) -- ft8.gfsk_iq(bt=None), tone 0
at +tone_hz, tone spacing = 1 / symbol period of the speed.

Timing: a message of N frames goes out in N consecutive periods of its speed (SPEC 3.1), each frame
starting start_delay_ms after the UTC-aligned period boundary (SPEC 1.3). Every frame is its own key-up /
key-down: the transmitter is keyed only while a frame is actually sent (no carrier between frames), so
plan_frames() returns one (key_at, start_at) pair per frame."""
import numpy as np

from . import ft8, js8_message, js8_phy
from .js8_phy import FAST, NORMAL, SLOW, TURBO  # noqa: F401  (re-exported)

SPEEDS = {"normal": NORMAL, "fast": FAST, "turbo": TURBO, "slow": SLOW}
MAX_FRAMES = 20          # docs/JS8_PLAN.md section 0: at most 20 transmissions in a row


def submode_from_name(name):
    try:
        return SPEEDS[name.strip().lower()]
    except KeyError:
        raise ValueError(f"unknown JS8 speed {name!r} (normal, fast, turbo, slow)") from None


def speed_info(submode):
    return js8_phy.submode_info(submode)


def encode_frame_iq(frame, flags, submode, tone_hz, sample_rate, amplitude=0.7, tail_s=0.0):
    """One frame -> (iq complex64, duration_s): the 79-symbol signal plus tail_s of silence."""
    info = speed_info(submode)
    tones = js8_phy.encode(frame, flags, submode)
    iq = ft8.gfsk_iq(tones, tone_hz, info["symbol_period_s"], None, sample_rate, amplitude)
    n_tail = int(round(tail_s * sample_rate))
    if n_tail > 0:
        iq = np.concatenate([iq, np.zeros(n_tail, dtype=np.complex64)])
    return iq.astype(np.complex64), len(iq) / sample_rate


def encode_frames_iq(frames, submode, tone_hz, sample_rate, amplitude=0.7, tail_s=0.0):
    """-> [(iq, duration_s)], one entry per frame (each sent in its own period, see plan_frames())."""
    return [encode_frame_iq(f, b, submode, tone_hz, sample_rate, amplitude, tail_s) for f, b in frames]


def frame_count(my_call, my_grid, text, submode, selected_call=""):
    return len(js8_message.build_frames(my_call, my_grid, text, submode, selected_call))


def transmission_time_s(n_frames, submode):
    """From the first frame's period start to the end of the last frame."""
    info = speed_info(submode)
    return (n_frames - 1) * info["period_s"] + info["start_delay_ms"] / 1000 + info["data_duration_s"]


def plan_frames(now, submode, n_frames, key_early_s=3.0, late_max_s=1.0):
    """-> [(key_at, start_at)] for n_frames in consecutive periods. The first period is the current one if
    its nominal start (period + start delay) is at most late_max_s ago, else the next one."""
    if not 1 <= n_frames <= MAX_FRAMES:
        raise ValueError(f"a JS8 transmission has 1..{MAX_FRAMES} frames, not {n_frames}")
    info = speed_info(submode)
    period = info["period_s"]
    _, first = ft8.plan_transmission(now, "any", key_early_s=key_early_s,
                                     start_in_slot_s=info["start_delay_ms"] / 1000, late_max_s=late_max_s,
                                     slot_s=period)
    slot0 = ft8.current_slot_start(first, period)
    delay = info["start_delay_ms"] / 1000
    plan = []
    for i in range(n_frames):
        start = first if i == 0 else slot0 + i * period + delay     # later frames on time even if the first was late
        plan.append((max(now, start - key_early_s), start))
    return plan


def can_encode(my_call, my_grid, text, submode):
    try:
        return 1 <= frame_count(my_call, my_grid, text, submode) <= MAX_FRAMES
    except ValueError:
        return False
