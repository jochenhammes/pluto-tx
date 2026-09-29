"""Per-mode parameter schemas -- the web counterpart of pluto-cli's per-mode
flags (pluto_cli/README.md section 4). SessionManager runs
every select_mode request through normalize_params() once, at the protocol
boundary, so each SessionBackend receives the same checked dict with all
defaults filled in: SimBackend just stores it, GnuRadioBackend will map it
onto pluto-tx's live setters (set_fm_deviation(), set_fm_preemphasis(),
set_subtone("ctcss", ...), set_fm_deemphasis() -- none needs a rebuild).

The value tables mirror pluto-tx's pluto_tx/config.py;
tests/test_modes.py checks them against pluto-tx in this repository, so a
change there fails loudly instead of drifting silently. Raises
ValueError (SessionManager turns it into an 'error' event).
"""
from __future__ import annotations

from . import station

FM_DEVIATION_CHOICES_HZ = (2500.0, 5000.0)
FM_DEVIATION_DEFAULT_HZ = 2500.0
FM_PREEMPHASIS_DEFAULT = True
FM_DEEMPHASIS_DEFAULT = True
CTCSS_TONES_HZ = (
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2,
    110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2,
    165.5, 167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5,
    206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
)

RTTY_MARK_HZ_DEFAULT = 2125.0
RTTY_MARK_HZ_RANGE = (300.0, 2700.0)
RTTY_SHIFT_HZ_DEFAULT = 170.0
RTTY_SHIFT_HZ_PRESETS = (170.0, 425.0, 850.0)
RTTY_BAUD_RATE_DEFAULT = 45.45
RTTY_BAUD_RATE_PRESETS = (45.45, 50.0, 75.0, 100.0)
RTTY_MAX_TEXT_LEN = 120

# Waterfall Writer ("digitext" in pluto-tx): text drawn into the receiver's
# waterfall. zoom 1..8 mirrors pluto-tx's GUI; min_freq_hz is the audio
# offset of the lettering above the carrier (USB).
DIGITEXT_MAX_TEXT_LEN = 40
DIGITEXT_LAYOUTS = ("horizontal", "vertical")
DIGITEXT_ZOOM_RANGE = (1, 8)
DIGITEXT_MIN_FREQ_HZ = 4000.0
DIGITEXT_MIN_FREQ_HZ_FLOOR = 300.0

# FT8 receive (pluto_advanced_rx: decoder next to USB). "auto" = jt9 (WSJT-X's
# decoder, finds the most) if installed, else ft8_lib.
FT8_DECODERS = ("auto", "jt9", "ft8lib")
FT8_DECODER_DEFAULT = "auto"
FT8_BAND_HZ = (100.0, 3100.0)  # audio band the RX decoder looks at, above the USB dial frequency

# FT8 transmit (pluto_tx/ft8.py, pluto_tx/config.py). The message text itself is
# built by pluto-tx' compose() from the station data and these parameters; only
# ft8_lib knows whether it packs (GnuRadioBackend checks with ft8_problem()).
FT8_MESSAGE_KINDS = ("cq", "reply", "report", "r_report", "rrr", "rr73", "73", "free")
FT8_FREE_TEXT_MAX = 13
FT8_REPORT_RANGE_DB = (-30, 30)
FT8_TONE_RANGE_HZ = (200.0, 2900.0)
FT8_TONE_DEFAULT_HZ = 1500.0
FT8_SLOTS = ("any", "even", "odd")  # next slot / 1st :00 :30 / 2nd :15 :45
FT8_MAX_REPEATS = 20  # same as ft8_series.FT8_MAX_REPEATS


def client_options() -> dict:
    """Choice lists the frontend builds its dropdowns from (sent in the
    'hello' event) -- keeps these tables in exactly one place."""
    return {
        "fm": {
            "deviation_choices_hz": list(FM_DEVIATION_CHOICES_HZ),
            "deviation_default_hz": FM_DEVIATION_DEFAULT_HZ,
            "preemphasis_default": FM_PREEMPHASIS_DEFAULT,
            "deemphasis_default": FM_DEEMPHASIS_DEFAULT,
            "ctcss_tones_hz": list(CTCSS_TONES_HZ),
        },
        "rtty": {
            "mark_hz_default": RTTY_MARK_HZ_DEFAULT, "mark_hz_range": list(RTTY_MARK_HZ_RANGE),
            "shift_hz_default": RTTY_SHIFT_HZ_DEFAULT, "shift_hz_choices": list(RTTY_SHIFT_HZ_PRESETS),
            "baud_default": RTTY_BAUD_RATE_DEFAULT, "baud_choices": list(RTTY_BAUD_RATE_PRESETS),
            "max_text_len": RTTY_MAX_TEXT_LEN,
        },
        "ft8": {
            "decoders": list(FT8_DECODERS), "band_hz": list(FT8_BAND_HZ),
            "message_kinds": list(FT8_MESSAGE_KINDS), "free_text_max": FT8_FREE_TEXT_MAX,
            "report_range_db": list(FT8_REPORT_RANGE_DB), "tone_range_hz": list(FT8_TONE_RANGE_HZ),
            "tone_default_hz": FT8_TONE_DEFAULT_HZ, "slots": list(FT8_SLOTS), "max_repeats": FT8_MAX_REPEATS,
        },
        "digitext": {
            "layouts": list(DIGITEXT_LAYOUTS), "zoom_range": list(DIGITEXT_ZOOM_RANGE),
            "min_freq_hz_range": [DIGITEXT_MIN_FREQ_HZ_FLOOR, DIGITEXT_MIN_FREQ_HZ],
            "min_freq_hz_default": DIGITEXT_MIN_FREQ_HZ, "max_text_len": DIGITEXT_MAX_TEXT_LEN,
        },
    }


def normalize_params(direction: str, mode: str, params: dict) -> dict:
    if mode == "fm":
        return _fm_tx(params) if direction == "tx" else _fm_rx(params)
    if mode == "rtty":
        return _rtty(params, with_text=direction == "tx")
    if mode == "digitext" and direction == "tx":
        return _digitext(params)
    if mode == "ft8" and direction == "rx":
        _reject_unknown(params, {"decoder"}, "ft8")
        decoder = params.get("decoder", FT8_DECODER_DEFAULT)
        if decoder not in FT8_DECODERS:
            raise ValueError(f"ft8: decoder must be one of {FT8_DECODERS}")
        return {"decoder": decoder}
    if mode == "ft8" and direction == "tx":
        return _ft8_tx(params)
    if mode == "rade" and direction == "tx":
        _reject_unknown(params, {"eoo"}, "rade")
        return {"eoo": _bool(params, "eoo", False)}
    return dict(params)


def _rtty(params: dict, with_text: bool) -> dict:
    allowed = {"mark_hz", "shift_hz", "baud", "reverse"} | ({"text"} if with_text else set())
    _reject_unknown(params, allowed, "rtty")
    mark = _number(params, "mark_hz", RTTY_MARK_HZ_DEFAULT, "rtty")
    lo, hi = RTTY_MARK_HZ_RANGE
    if not lo <= mark <= hi:
        raise ValueError(f"rtty: mark_hz must be within {lo:g}..{hi:g} Hz")
    shift = _choice(params, "shift_hz", RTTY_SHIFT_HZ_DEFAULT, RTTY_SHIFT_HZ_PRESETS, "rtty")
    baud = _choice(params, "baud", RTTY_BAUD_RATE_DEFAULT, RTTY_BAUD_RATE_PRESETS, "rtty")
    out = {"mark_hz": mark, "shift_hz": shift, "baud": baud, "reverse": _bool(params, "reverse", False)}
    if with_text:
        out["text"] = _text(params, RTTY_MAX_TEXT_LEN, "rtty")
    return out


def _ft8_tx(params: dict) -> dict:
    _reject_unknown(params, {"kind", "dx_call", "report_db", "free_text", "offset_hz", "slot", "drift_comp",
                             "repeat_count"}, "ft8")
    kind = params.get("kind", "cq")
    if kind not in FT8_MESSAGE_KINDS:
        raise ValueError(f"ft8: kind must be one of {FT8_MESSAGE_KINDS}")
    dx_call = params.get("dx_call", "")
    if not isinstance(dx_call, str):
        raise ValueError("ft8: dx_call must be a string")  # noqa: TRY004 -- one error type for all bad client input
    dx_call = dx_call.strip().upper()
    if dx_call:
        station.normalize(dx_call, "")  # same callsign rule as the station's own
    report = params.get("report_db", -10)
    lo, hi = FT8_REPORT_RANGE_DB
    if not isinstance(report, int) or isinstance(report, bool) or not lo <= report <= hi:
        raise ValueError(f"ft8: report_db must be an integer {lo}..{hi}")
    offset = _number(params, "offset_hz", FT8_TONE_DEFAULT_HZ, "ft8")
    lo_hz, hi_hz = FT8_TONE_RANGE_HZ
    if not lo_hz <= offset <= hi_hz:
        raise ValueError(f"ft8: offset_hz must be within {lo_hz:g}..{hi_hz:g} Hz")
    slot = params.get("slot", "any")
    if slot not in FT8_SLOTS:
        raise ValueError(f"ft8: slot must be one of {FT8_SLOTS}")
    repeats = params.get("repeat_count", 1)
    if not isinstance(repeats, int) or isinstance(repeats, bool) or not 1 <= repeats <= FT8_MAX_REPEATS:
        raise ValueError(f"ft8: repeat_count must be an integer 1..{FT8_MAX_REPEATS}")
    return {
        "kind": kind, "dx_call": dx_call, "report_db": report,
        "free_text": _text(params, FT8_FREE_TEXT_MAX, "ft8", key="free_text").upper(),
        "offset_hz": offset, "slot": slot, "drift_comp": _bool(params, "drift_comp", True),
        "repeat_count": repeats,
    }


def _digitext(params: dict) -> dict:
    _reject_unknown(params, {"text", "layout", "zoom", "min_freq_hz"}, "digitext")
    layout = params.get("layout", "horizontal")
    if layout not in DIGITEXT_LAYOUTS:
        raise ValueError(f"digitext: layout must be one of {DIGITEXT_LAYOUTS}")
    zoom = params.get("zoom", 1)
    if not isinstance(zoom, int) or isinstance(zoom, bool) or not DIGITEXT_ZOOM_RANGE[0] <= zoom <= DIGITEXT_ZOOM_RANGE[1]:
        raise ValueError(f"digitext: zoom must be an integer {DIGITEXT_ZOOM_RANGE[0]}..{DIGITEXT_ZOOM_RANGE[1]}")
    min_freq = _number(params, "min_freq_hz", DIGITEXT_MIN_FREQ_HZ, "digitext")
    if not DIGITEXT_MIN_FREQ_HZ_FLOOR <= min_freq <= DIGITEXT_MIN_FREQ_HZ:
        raise ValueError(f"digitext: min_freq_hz must be within {DIGITEXT_MIN_FREQ_HZ_FLOOR:g}..{DIGITEXT_MIN_FREQ_HZ:g}")
    return {"text": _text(params, DIGITEXT_MAX_TEXT_LEN, "digitext"), "layout": layout, "zoom": zoom,
            "min_freq_hz": min_freq}


def _text(params: dict, max_len: int, mode: str, key: str = "text") -> str:
    text = params.get(key, "")
    if not isinstance(text, str):
        raise ValueError(f"{mode}: {key} must be a string")  # noqa: TRY004 -- one error type for all bad client input
    if len(text) > max_len:
        raise ValueError(f"{mode}: {key} is longer than {max_len} characters")
    return text


def _number(params: dict, key: str, default: float, mode: str) -> float:
    value = params.get(key, default)
    if not _is_number(value):
        raise ValueError(f"{mode}: {key} must be a number, got {value!r}")
    return float(value)


def _choice(params: dict, key: str, default: float, choices: tuple, mode: str) -> float:
    value = _number(params, key, default, mode)
    match = next((c for c in choices if abs(c - value) < 1e-6), None)
    if match is None:
        raise ValueError(f"{mode}: {key} must be one of {choices}, got {value:g}")
    return match


def _fm_tx(params: dict) -> dict:
    _reject_unknown(params, {"deviation_hz", "preemphasis", "ctcss_hz"})
    deviation = params.get("deviation_hz", FM_DEVIATION_DEFAULT_HZ)
    if not _is_number(deviation) or float(deviation) not in FM_DEVIATION_CHOICES_HZ:
        raise ValueError(f"fm: deviation_hz must be one of {FM_DEVIATION_CHOICES_HZ}, got {deviation!r}")
    ctcss = params.get("ctcss_hz")
    if ctcss is not None:
        if not _is_number(ctcss):
            raise ValueError(f"fm: ctcss_hz must be a number or null, got {ctcss!r}")
        # Match within rounding so 88.5 / 88.50 / 88.4999 from a UI all land on the standard tone.
        match = next((t for t in CTCSS_TONES_HZ if abs(t - float(ctcss)) < 0.05), None)
        if match is None:
            raise ValueError(f"fm: {ctcss} Hz is not a standard CTCSS tone")
        ctcss = match
    return {
        "deviation_hz": float(deviation),
        "preemphasis": _bool(params, "preemphasis", FM_PREEMPHASIS_DEFAULT),
        "ctcss_hz": ctcss,
    }


def _fm_rx(params: dict) -> dict:
    _reject_unknown(params, {"deemphasis"})
    return {"deemphasis": _bool(params, "deemphasis", FM_DEEMPHASIS_DEFAULT)}


def _reject_unknown(params: dict, allowed: set[str], mode: str = "fm") -> None:
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"{mode}: unknown parameter(s) {sorted(unknown)}")


def _bool(params: dict, key: str, default: bool) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false, got {value!r}")  # noqa: TRY004 -- one error type for all bad client input
    return value


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
