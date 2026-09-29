import importlib.util
from pathlib import Path

import pytest

from web_trx import modes

# web-trx/backend/tests/test_modes.py -> parents[3] is the pluto-tx repository root
PLUTO_TX_CONFIG = Path(__file__).resolve().parents[3] / "pluto_tx" / "config.py"


def test_fm_tx_defaults():
    assert modes.normalize_params("tx", "fm", {}) == {
        "deviation_hz": 2500.0, "preemphasis": True, "ctcss_hz": None,
    }


def test_fm_rx_defaults():
    assert modes.normalize_params("rx", "fm", {}) == {"deemphasis": True}


def test_fm_tx_wide_deviation_no_preemphasis():
    out = modes.normalize_params("tx", "fm", {"deviation_hz": 5000, "preemphasis": False})
    assert out["deviation_hz"] == 5000.0
    assert out["preemphasis"] is False


@pytest.mark.parametrize("bad", [3000, "5000", True, None])
def test_fm_tx_rejects_invalid_deviation(bad):
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "fm", {"deviation_hz": bad})


def test_fm_tx_ctcss_snaps_to_standard_tone():
    assert modes.normalize_params("tx", "fm", {"ctcss_hz": 88.5})["ctcss_hz"] == 88.5
    assert modes.normalize_params("tx", "fm", {"ctcss_hz": 88.4999})["ctcss_hz"] == 88.5


@pytest.mark.parametrize("bad", [88.0, 1000, "88.5"])
def test_fm_tx_rejects_non_standard_ctcss(bad):
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "fm", {"ctcss_hz": bad})


def test_fm_rejects_non_bool_switches():
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "fm", {"preemphasis": "yes"})
    with pytest.raises(ValueError):
        modes.normalize_params("rx", "fm", {"deemphasis": 1})


def test_fm_rejects_unknown_keys():
    # A frontend typo must fail loudly instead of being silently ignored.
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "fm", {"deviaton_hz": 5000})
    with pytest.raises(ValueError):
        modes.normalize_params("rx", "fm", {"deviation_hz": 5000})  # a TX-only option on RX


def test_other_modes_pass_through_unchanged():
    params = {"ric": 42, "text": "hi"}
    assert modes.normalize_params("tx", "pocsag", params) == params


@pytest.mark.skipif(not PLUTO_TX_CONFIG.exists(), reason="not inside a pluto-tx checkout")
def test_tables_match_pluto_tx():
    """Loaded by file path so the test needs no sys.path changes;
    pluto_tx/config.py is pure Python, no GNU Radio needed."""
    spec = importlib.util.spec_from_file_location("pluto_tx_config", PLUTO_TX_CONFIG)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    assert modes.FM_DEVIATION_CHOICES_HZ == tuple(cfg.FM_DEVIATION_CHOICES_HZ)
    assert modes.FM_DEVIATION_DEFAULT_HZ == cfg.FM_DEVIATION_HZ
    assert modes.FM_PREEMPHASIS_DEFAULT == cfg.FM_PREEMPH_DEFAULT
    assert modes.CTCSS_TONES_HZ == tuple(cfg.CTCSS_TONES_HZ)


def test_rtty_defaults_and_validation():
    assert modes.normalize_params("tx", "rtty", {"text": "CQ"}) == {
        "mark_hz": 2125.0, "shift_hz": 170.0, "baud": 45.45, "reverse": False, "text": "CQ",
    }
    assert "text" not in modes.normalize_params("rx", "rtty", {})
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "rtty", {"shift_hz": 200})
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "rtty", {"mark_hz": 5000})
    with pytest.raises(ValueError):
        modes.normalize_params("rx", "rtty", {"text": "not on RX"})
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "rtty", {"text": "x" * 121})


def test_digitext_defaults_and_validation():
    assert modes.normalize_params("tx", "digitext", {"text": "DA2JH"}) == {
        "text": "DA2JH", "layout": "horizontal", "zoom": 1, "min_freq_hz": 4000.0,
    }
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "digitext", {"zoom": 9})
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "digitext", {"layout": "diagonal"})
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "digitext", {"min_freq_hz": 100})


def test_rade_tx_eoo_flag():
    assert modes.normalize_params("tx", "rade", {}) == {"eoo": False}
    assert modes.normalize_params("tx", "rade", {"eoo": True}) == {"eoo": True}
    with pytest.raises(ValueError):
        modes.normalize_params("tx", "rade", {"callsign": "x"})


@pytest.mark.skipif(not PLUTO_TX_CONFIG.exists(), reason="not inside a pluto-tx checkout")
def test_rtty_and_digitext_tables_match_pluto_tx():
    spec = importlib.util.spec_from_file_location("pluto_tx_config", PLUTO_TX_CONFIG)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    assert modes.RTTY_MARK_HZ_DEFAULT == cfg.RTTY_MARK_HZ_DEFAULT
    assert modes.RTTY_MARK_HZ_RANGE == tuple(cfg.RTTY_MARK_HZ_RANGE)
    assert modes.RTTY_SHIFT_HZ_DEFAULT == cfg.RTTY_SHIFT_HZ_DEFAULT
    assert modes.RTTY_SHIFT_HZ_PRESETS == tuple(cfg.RTTY_SHIFT_HZ_PRESETS)
    assert modes.RTTY_BAUD_RATE_DEFAULT == cfg.RTTY_BAUD_RATE_DEFAULT
    assert modes.RTTY_BAUD_RATE_PRESETS == tuple(cfg.RTTY_BAUD_RATE_PRESETS)
    assert modes.RTTY_MAX_TEXT_LEN == cfg.RTTY_MAX_TEXT_LEN
    assert modes.DIGITEXT_MAX_TEXT_LEN == cfg.DIGITEXT_MAX_TEXT_LEN
    assert modes.DIGITEXT_MIN_FREQ_HZ == cfg.DIGITEXT_MIN_FREQ_HZ
    assert modes.DIGITEXT_MIN_FREQ_HZ_FLOOR == cfg.DIGITEXT_MIN_FREQ_HZ_FLOOR


def test_ft8_rx_decoder_choice():
    assert modes.normalize_params("rx", "ft8", {}) == {"decoder": "auto"}
    assert modes.normalize_params("rx", "ft8", {"decoder": "jt9"}) == {"decoder": "jt9"}
    with pytest.raises(ValueError):
        modes.normalize_params("rx", "ft8", {"decoder": "wsjtx"})
    with pytest.raises(ValueError):
        modes.normalize_params("rx", "ft8", {"offset_hz": 1500})  # a TX option


PLUTO_RX_CONFIG = PLUTO_TX_CONFIG.parents[1] / "pluto_advanced_rx" / "config.py"
PLUTO_CLI_RX = PLUTO_TX_CONFIG.parents[1] / "pluto_cli" / "rx.py"


@pytest.mark.skipif(not PLUTO_RX_CONFIG.exists(), reason="not inside a pluto-tx checkout")
def test_ft8_rx_tables_match_pluto_tx():
    """pluto_advanced_rx/config.py imports other pluto-tx modules, so the
    values are read as text rather than imported."""
    import ast
    import re

    cfg = PLUTO_RX_CONFIG.read_text()

    def value(name):
        return ast.literal_eval(re.search(rf"^{name} = (.+?)(\s+#.*)?$", cfg, re.MULTILINE).group(1))

    assert value("FT8_DECODER_BACKEND") == modes.FT8_DECODER_DEFAULT
    assert value("FT8_BAND_HZ") == modes.FT8_BAND_HZ
    cli = PLUTO_CLI_RX.read_text()
    choices = re.search(r'"--ft8-decoder", choices=(\(.*?\))', cli).group(1)
    assert ast.literal_eval(choices) == modes.FT8_DECODERS


def test_ft8_tx_defaults_and_validation():
    assert modes.normalize_params("tx", "ft8", {}) == {
        "kind": "cq", "dx_call": "", "report_db": -10, "free_text": "", "offset_hz": 1500.0,
        "slot": "any", "drift_comp": True, "repeat_count": 1,
    }
    out = modes.normalize_params("tx", "ft8", {"kind": "free", "free_text": "tnx 73", "dx_call": " dl1abc "})
    assert out["free_text"] == "TNX 73" and out["dx_call"] == "DL1ABC"
    for bad in ({"kind": "qrz"}, {"dx_call": "not a call"}, {"report_db": 31}, {"report_db": -9.5},
                {"free_text": "x" * 14}, {"offset_hz": 100}, {"slot": "first"}, {"repeat_count": 21},
                {"repeat_count": 0}, {"drift_comp": "yes"}, {"decoder": "jt9"}):
        with pytest.raises(ValueError):
            modes.normalize_params("tx", "ft8", bad)


@pytest.mark.skipif(not PLUTO_TX_CONFIG.exists(), reason="not inside a pluto-tx checkout")
def test_ft8_tx_tables_match_pluto_tx():
    """pluto_tx/ft8.py has relative imports: imported as a package (repo root
    on sys.path); it needs only numpy/ctypes, no GNU Radio or ft8_lib."""
    from web_trx import ft8_series, pluto_path

    pluto_path.ensure_importable()
    from pluto_tx import config, ft8

    assert modes.FT8_MESSAGE_KINDS == tuple(kind for kind, _label in ft8.MESSAGE_KINDS)
    assert modes.FT8_FREE_TEXT_MAX == ft8.FREE_TEXT_MAX
    assert modes.FT8_TONE_RANGE_HZ == tuple(config.FT8_TONE_RANGE_HZ)
    assert modes.FT8_TONE_DEFAULT_HZ == config.FT8_DEFAULT_TONE_HZ
    assert modes.FT8_MAX_REPEATS == ft8_series.FT8_MAX_REPEATS


def test_js8_rx_choices():
    assert modes.normalize_params("rx", "js8", {}) == {"submode": "normal", "decoder": "auto"}
    assert modes.normalize_params("rx", "js8", {"submode": "all", "decoder": "own"}) == {"submode": "all",
                                                                                           "decoder": "own"}
    for bad in ({"submode": "ludicrous"}, {"decoder": "wsjtx"}, {"offset_hz": 1500}):
        with pytest.raises(ValueError):
            modes.normalize_params("rx", "js8", bad)


def test_js8_tx_defaults_and_validation():
    assert modes.normalize_params("tx", "js8", {}) == {
        "kind": "cq", "to": "", "text": "", "report_db": -10, "submode": "normal", "offset_hz": 1500.0,
        "drift_comp": True}
    out = modes.normalize_params("tx", "js8", {"kind": "directed", "to": " dl1abc ", "text": "hi", "submode": "turbo",
                                               "offset_hz": 2840})
    assert (out["to"], out["submode"], out["offset_hz"]) == ("DL1ABC", "turbo", 2840.0)
    assert modes.normalize_params("tx", "js8", {"to": "@allcall"})["to"] == "@ALLCALL"
    for bad in ({"kind": "relay"}, {"to": "not a call"}, {"report_db": 99}, {"submode": "ultra"},
                {"offset_hz": 2900, "submode": "turbo"}, {"offset_hz": 100}, {"text": "x" * 401},
                {"drift_comp": "yes"}, {"unknown": 1}):
        with pytest.raises(ValueError):
            modes.normalize_params("tx", "js8", bad)


def test_js8_tables_match_pluto_tx():
    import ast
    import re

    from web_trx import js8_series, pluto_path

    pluto_path.ensure_importable()
    from pluto_advanced_rx import config as rx_config
    from pluto_tx import config, js8, js8_message, js8_phy

    assert modes.JS8_MESSAGE_KINDS == tuple(kind for kind, _label in js8_message.MESSAGE_KINDS)
    assert modes.JS8_SUBMODES == tuple(js8.SPEEDS)
    for name, sm in js8.SPEEDS.items():
        info = js8_phy.submode_info(sm)
        assert modes.JS8_PERIOD_S[name] == info["period_s"]
        assert modes.JS8_BANDWIDTH_HZ[name] == info["bandwidth_hz"]
    assert modes.JS8_TONE_RANGE_HZ == tuple(config.JS8_TONE_RANGE_HZ)
    assert modes.JS8_TONE_DEFAULT_HZ == config.JS8_DEFAULT_TONE_HZ
    assert modes.JS8_MAX_FRAMES == js8.MAX_FRAMES == js8_series.JS8_MAX_FRAMES
    assert modes.JS8_DECODER_DEFAULT == rx_config.JS8_DECODER_BACKEND
    assert modes.JS8_BAND_HZ == tuple(rx_config.JS8_BAND_HZ)
    cli = (PLUTO_TX_CONFIG.parents[1] / "pluto_cli" / "rx.py").read_text()
    choices = re.search(r'"--js8-decoder", choices=(\(.*?\))', cli).group(1)
    assert ast.literal_eval(choices) == modes.JS8_DECODERS
