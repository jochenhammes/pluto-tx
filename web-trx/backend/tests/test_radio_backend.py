"""GnuRadioBackend tests. Skipped wherever GNU Radio or a pluto-tx checkout
isn't available (CI, the cloud dev container). The hardware-touching test
additionally needs WEB_TRX_HW_TESTS=1 and a reachable PlutoSDR -- it only
RECEIVES, it never keys a transmitter."""
import asyncio
import json
import os

import pytest

pytest.importorskip("gnuradio")
try:
    from web_trx import radio_backend
except RuntimeError as e:  # no pluto-tx checkout, see pluto_path.py
    pytest.skip(str(e), allow_module_level=True)

from web_trx.session import AudioFrame, SessionError


def make_backend():
    b = radio_backend.GnuRadioBackend()
    events, spectrum, audio = [], [], []

    async def emit_event(name, fields):
        events.append((name, fields))

    async def on_spectrum(frame):
        spectrum.append(frame)

    async def on_audio(frame):
        audio.append(frame)

    b.bind(emit_event, on_spectrum, on_audio)
    return b, events, spectrum, audio


def test_device_types_only_lists_registered_hardware():
    b, *_ = make_backend()
    types = b.device_types()
    assert ["pluto", "PlutoSDR"] in types["rx"]
    assert ["pluto", "PlutoSDR"] in types["tx"]
    assert all(t != "sim" for t, _ in types["rx"] + types["tx"])


async def test_rejects_unknown_device_type():
    b, *_ = make_backend()
    with pytest.raises(SessionError):
        await b.connect("rx", "sim", "")


async def test_unknown_tx_mode_is_refused():
    b, *_ = make_backend()
    b.tx.connection = "ip:unused"  # pretend-connected; the mode check happens before any hardware access
    b.tx.device_type = "pluto"
    with pytest.raises(SessionError, match="unsupported"):
        await b.select_mode("tx", "psk31", {})


async def test_mic_audio_is_ignored_while_unkeyed():
    b, *_ = make_backend()
    b.tx.mode = "fm"
    await b.submit_tx_audio(AudioFrame(pcm16=b"\x00\x10" * 4800, sample_rate_hz=48000))
    assert b._mic_buffer.buffered_s == 0


async def test_ptt_without_mode_is_refused():
    b, *_ = make_backend()
    with pytest.raises(SessionError):
        await b.ptt(True)


async def test_estop_without_flowgraph_is_harmless():
    b, events, *_ = make_backend()
    await b.estop()
    await b.estop()
    assert [name for name, _ in events] == ["estop", "estop"]


@pytest.mark.skipif(os.environ.get("WEB_TRX_HW_TESTS") != "1", reason="needs WEB_TRX_HW_TESTS=1 and a PlutoSDR")
async def test_pluto_rx_delivers_spectrum_and_audio():
    b, _events, spectrum, audio = make_backend()
    b.start_background_tasks()
    try:
        await b.connect("rx", "pluto", "")
        await b.select_mode("rx", "fm", {})
        await asyncio.sleep(2.0)
        assert len(spectrum) > 10
        assert spectrum[-1].row.shape[0] > 0
        assert spectrum[-1].center_hz == b.rx.freq_hz
        assert len(audio) > 0
        assert audio[-1].sample_rate_hz == radio_backend.AUDIO_RATE_HZ
    finally:
        await b.shutdown()


async def test_tx_settings_are_validated_and_broadcast_without_flowgraph():
    b, events, *_ = make_backend()
    assert b.snapshot()["tx"]["settings"]["compressor_enabled"] is True
    await b.set_gain("tx", "compressor_ratio", 6)
    await b.set_gain("tx", "limiter_enabled", 0)
    settings = b.snapshot()["tx"]["settings"]
    assert settings["compressor_ratio"] == 6.0 and settings["limiter_enabled"] is False
    assert events[-1][0] == "tx_settings"
    with pytest.raises(SessionError):
        await b.set_gain("tx", "compressor_ratio", 50)
    with pytest.raises(SessionError):
        await b.set_gain("tx", "fm_deviation_hz", 5000)  # an FM mode parameter now, see modes.py
    with pytest.raises(SessionError):
        await b.set_gain("tx", "nonsense", 1)
    with pytest.raises(SessionError):
        await b.set_gain("rx", "nonsense", 1)


async def test_settings_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_TRX_SETTINGS_PATH", str(tmp_path / "settings.json"))
    b, *_ = make_backend()
    await b.tune("tx", 145_150_000)  # allowed while disconnected
    b.tx.device_type = "pluto"
    await b.set_gain("tx", "power", -30.0)
    await b.set_gain("tx", "compressor_ratio", 3)
    b2, *_ = make_backend()
    assert b2.tx.freq_hz == 145_150_000
    assert b2.tx.power_by_device == {"pluto": -30.0}
    assert b2.snapshot()["tx"]["settings"]["compressor_ratio"] == 3.0


def test_open_connections_are_scheduled_for_restore(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "rx": {"freq_hz": 145e6, "mode": "fm", "device_type": "rtlsdr", "connected": True, "connection": ""},
        "tx": {"freq_hz": 145e6, "mode": None, "device_type": "pluto", "connected": True,
               "connection": "ip:x"},  # e.g. after E-STOP
    }))
    monkeypatch.setenv("WEB_TRX_SETTINGS_PATH", str(path))
    b, *_ = make_backend()
    assert b._restore == {"rx": ""}  # "" = auto-detect; TX without a mode is never re-armed automatically
    assert b.rx.connection is None  # nothing is opened before start_background_tasks()


def test_one_shot_and_voice_modes_are_disjoint_and_cover_all_tx_modes():
    voice = set(radio_backend.TX_MODES) - set(radio_backend.ONE_SHOT_TX_MODES)
    assert voice == {"fm", "ssb", "lsb", "m17", "rade"}
    assert set(radio_backend.ONE_SHOT_TX_MODES) <= set(radio_backend.TX_MODES)


async def test_rtty_characters_are_batched():
    b, events, *_ = make_backend()
    b.start_background_tasks()
    b.rx.tb = object()  # any flowgraph; RTTY AFC only runs when rx.mode == "rtty"
    try:
        for ch in "CQ CQ":
            b._on_rtty_char(ch)
        await asyncio.sleep(radio_backend.RTTY_FLUSH_S * 2)
        assert [f["text"] for name, f in events if name == "rtty_text"] == ["CQ CQ"]
    finally:
        b.rx.tb = None
        await b.shutdown()


async def test_sample_rate_and_ppm_are_validated():
    b, *_ = make_backend()
    b.rx.device_type = "rtlsdr"
    await b.set_gain("rx", "sample_rate", 960_000)  # an RTL-SDR choice; no flowgraph -> just stored
    assert b.rx.gains["sample_rate"] == 960_000
    with pytest.raises(SessionError):
        await b.set_gain("rx", "sample_rate", 123_456)
    await b.set_gain("tx", "freq_correction_ppm", -0.4)
    with pytest.raises(SessionError):
        await b.set_gain("rx", "freq_correction_ppm", 500)


async def test_rx_gain_is_validated_against_the_device():
    b, *_ = make_backend()
    b.rx.device_type = "rtlsdr"
    await b.set_gain("rx", "gain_mode", "agc")
    await b.set_gain("rx", "stage:TUNER", 30.0)
    with pytest.raises(SessionError):
        await b.set_gain("rx", "gain_mode", "slow_attack")  # a Pluto mode, not an RTL-SDR one
    with pytest.raises(SessionError):
        await b.set_gain("rx", "stage:TUNER", 80.0)
    with pytest.raises(SessionError):
        await b.set_gain("rx", "stage:LNA", 8.0)  # a HackRF stage


def test_rx_gain_build_args_per_device():
    pluto = radio_backend.rx_devices.DEVICE_REGISTRY["pluto"]
    hackrf = radio_backend.rx_devices.DEVICE_REGISTRY["hackrf"]
    assert radio_backend._rx_gain_build_args(pluto, {"gain_mode": "manual", "stage:gain": 40.0}) == ("manual", 40.0, None)
    _, _, values = radio_backend._rx_gain_build_args(hackrf, {"stage:LNA": 16.0, "stage:AMP": True, "gain_mode": "agc"})
    assert values == {"LNA": 16.0, "AMP": True}
    # a mode saved for another device falls back to this device's default
    assert radio_backend._rx_gain_build_args(pluto, {"gain_mode": "agc"})[0] == pluto.default_gain_mode


@pytest.mark.parametrize(("saved", "expected"), [(0.0, -20.0), (-10.0, -20.0), (-48.5, -48.5)])
def test_tx_power_is_capped_to_the_safe_default_after_restart(tmp_path, monkeypatch, saved, expected):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"tx": {"freq_hz": 145e6, "device_type": "pluto",
                                       "gains": {"power": saved, "freq_correction_ppm": -0.4}}}))
    monkeypatch.setenv("WEB_TRX_SETTINGS_PATH", str(path))
    b, *_ = make_backend()
    assert b.tx.power_by_device == {"pluto": expected}  # an old plain "power" belongs to the saved device
    assert b.tx.gains["freq_correction_ppm"] == -0.4  # other TX settings are kept


async def test_squelch_opens_at_threshold_and_closes_with_hysteresis_and_hang(monkeypatch):
    monkeypatch.setattr(radio_backend, "SQUELCH_HANG_S", 0.1)
    level = {"db": -40.0}

    class Probe:
        def level(self):
            return 10 ** (level["db"] / 10)

    class Tb:
        web_trx_level_probe = Probe()

    b, events, *_ = make_backend()
    b.rx.tb = Tb()
    b.rx.gains["squelch_db"] = -50.0
    b.start_background_tasks()
    try:
        await asyncio.sleep(0.15)
        assert b._squelch_open is True           # -40 dB >= -50 dB threshold
        level["db"] = -52.0                        # below threshold, inside hysteresis
        await asyncio.sleep(0.3)
        assert b._squelch_open is True
        level["db"] = -60.0                        # below threshold - 3 dB, after hang time
        await asyncio.sleep(0.3)
        assert b._squelch_open is False
        assert any(name == "rx_level" and f["squelch_open"] is False for name, f in events)
        b.rx.gains["squelch_db"] = radio_backend.SQUELCH_OFF_DB  # off
        await asyncio.sleep(0.15)
        assert b._squelch_open is True
    finally:
        b.rx.tb = None
        await b.shutdown()


def test_station_start_value_from_environment_then_saved_value_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_TRX_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("WEB_TRX_STATION_CALL", "DA2JH")
    monkeypatch.setenv("WEB_TRX_STATION_LOCATOR", "JO43")
    b, *_ = make_backend()
    assert b.station == {"call": "DA2JH", "locator": "JO43"}
    b.set_station({"call": "DA2JH/P", "locator": "JO43AB"})
    b2, *_ = make_backend()  # "restart": the saved value beats the environment
    assert b2.station == {"call": "DA2JH/P", "locator": "JO43AB"}


async def test_direct_sampling_only_for_rtl_sdr():
    b, *_ = make_backend()
    b.rx.device_type = "rtlsdr"
    await b.set_gain("rx", "direct_sampling", "q")  # no flowgraph -> just stored
    assert b.rx.gains["direct_sampling"] == "q"
    with pytest.raises(SessionError):
        await b.set_gain("rx", "direct_sampling", "x")
    b.rx.device_type = "pluto"
    with pytest.raises(SessionError):
        await b.set_gain("rx", "direct_sampling", "i")
    await b.set_gain("rx", "direct_sampling", "off")  # always allowed
    assert b.features()["rx_direct_sampling"] == ["rtlsdr"]


async def test_ft8_decodes_become_one_slot_event_with_sender():
    from pluto_advanced_rx.ft8_decoder import Ft8Decode

    b, events, *_ = make_backend()
    b.start_background_tasks()
    try:
        b._on_ft8_decodes(1790600115.0, [
            Ft8Decode(text="CQ DL1ABC JO62", snr_db=-12.4, dt_s=0.13, freq_hz=1234.5),
            Ft8Decode(text="DA2JH DL1ABC -10", snr_db=3.0, dt_s=-0.2, freq_hz=800.0),
            Ft8Decode(text="TNX 73 GL", snr_db=-20.0, dt_s=0.0, freq_hz=2000.0),
        ])
        await asyncio.sleep(0.05)
        b._on_ft8_decodes(1790600115.0, [])  # the same slot again: dropped
        await asyncio.sleep(0.05)
        assert [name for name, _ in events].count("ft8_slot") == 1
        slot = next(f for name, f in events if name == "ft8_slot")
        assert slot["utc"] == "125515"  # 1790600115 = 12:55:15 UTC
        assert [d["sender"] for d in slot["decodes"]] == ["DL1ABC", "DL1ABC", None]
        assert slot["decodes"][0] == {"snr_db": -12, "dt_s": 0.1, "freq_hz": 1234, "text": "CQ DL1ABC JO62",
                                      "sender": "DL1ABC"}
        assert "ft8" in b.features()["rx_modes"]  # jt9 or ft8_lib installed on this machine
    finally:
        await b.shutdown()


async def test_hackrf_rf_amp_is_switchable_off_by_default_and_not_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_TRX_SETTINGS_PATH", str(tmp_path / "settings.json"))
    from pluto_tx.devices.hackrf import HackRFDevice

    class Tb:  # just enough of PlutoTxFlowgraph for the power-stage logic
        def __init__(self):
            self.device = HackRFDevice.__new__(HackRFDevice)
            self.power_ceiling, self.target_power = 47.0, 10.0
            self.web_trx_secondary_power = {}
            self.applied = []

        def set_secondary_power(self, name, value):
            self.applied.append((name, value))

    b, events, *_ = make_backend()
    with pytest.raises(SessionError):
        await b.set_gain("tx", "stage:AMP", 1)  # no TX flowgraph yet
    b.tx.device_type = "hackrf"
    b.tx.tb = Tb()
    info = radio_backend._power_info(b.tx.tb)
    assert info["secondary"][0]["name"] == "AMP" and info["secondary"][0]["value"] is False
    await b.set_gain("tx", "stage:AMP", 1)
    assert b.tx.tb.applied == [("AMP", True)]
    assert events[-1][0] == "tx_power" and events[-1][1]["secondary"][0]["value"] is True
    assert "stage:AMP" not in b.tx.gains  # never persisted
    with pytest.raises(SessionError):
        await b.set_gain("tx", "stage:VGA", 10)  # the primary stage goes through "power"
    b.tx.tb = None


async def test_tx_power_is_kept_per_device_so_a_hackrf_gain_never_reaches_the_pluto(tmp_path, monkeypatch):
    """HackRF power is VGA gain (20 = 20 dB gain), Pluto power is attenuation
    (20 would be clamped to 0 dB = full power): one shared value would start
    the Pluto at full power after switching devices."""
    monkeypatch.setenv("WEB_TRX_SETTINGS_PATH", str(tmp_path / "settings.json"))
    b, *_ = make_backend()
    b.tx.device_type = "hackrf"
    await b.set_gain("tx", "power", 20.0)
    b.tx.device_type = "pluto"
    await b.set_gain("tx", "power", -35.0)
    assert b.tx.power_by_device == {"hackrf": 20.0, "pluto": -35.0}
    b2, *_ = make_backend()  # restart: both kept (below their safe defaults? HackRF 20 > 0 -> capped)
    hackrf_safe = radio_backend.tx_devices.DEVICE_REGISTRY["hackrf"].default_power_ceiling
    assert b2.tx.power_by_device == {"hackrf": min(20.0, hackrf_safe), "pluto": -35.0}
    with pytest.raises(SessionError):
        b2.tx.device_type = None
        await b2.set_gain("tx", "power", -30.0)  # no device: nothing to attach the value to
