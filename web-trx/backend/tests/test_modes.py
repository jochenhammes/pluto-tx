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
