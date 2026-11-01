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

from web_trx import modes
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


async def test_js8_features_follow_the_codec_self_test(monkeypatch):
    b, *_ = make_backend()
    features = b.features()
    assert features["js8"]["tx"] and "js8" in features["tx_modes"] and "js8" in features["rx_modes"]
    assert "own" in features["js8"]["rx_backends"]
    monkeypatch.setattr(radio_backend, "JS8_AVAILABLE", False)  # e.g. a broken js8_tables.py
    features = b.features()
    assert not features["js8"]["tx"] and features["js8"]["rx_backends"] == []
    assert "js8" not in features["tx_modes"] and "js8" not in features["rx_modes"]
    with pytest.raises(SessionError):
        await b.select_mode("tx", "js8", {"kind": "cq", "submode": "normal"})


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


def test_rx_bands_match_the_rx_apps_demod_band_shading():
    b, *_ = make_backend()
    bands = b.features()["rx_bands"]
    assert bands["fm"] == [-6250.0, 6250.0]            # 12.5 kHz NBFM channel
    assert bands["ssb"] == [300.0, 3300.0]             # USB above the carrier
    assert bands["lsb"] == [-3300.0, -300.0]           # mirrored below it
    assert bands["m17"] == [-5600.0, 5600.0]           # deviation + symbol rate
    assert bands["rade"] == [750.0, 2200.0]
    assert "ft8" not in bands                          # FT8 keeps its own audio-band marker
    assert bands["adsl"] == [-750_000.0, -350_000.0]   # both M-band channels, below the fixed LO
    assert set(bands) <= set(radio_backend.RX_DEMOD_MODES)


async def test_rx_display_setting_during_a_rebuild_reaches_the_new_flowgraph():
    """A zoom change sent while a mode change rebuilds the receiver (which holds _rx_lock and has
    already read the settings) must end up on the rebuilt flowgraph, not be dropped."""
    b, *_ = make_backend()

    class Tb:
        zoom = None

        def set_fft_zoom(self, z):
            self.zoom = z

    new_tb = Tb()
    async with b._rx_lock:                     # a rebuild in progress: old flowgraph already gone
        b.rx.tb = None
        task = asyncio.create_task(b.set_gain("rx", "fft_zoom", 4))
        await asyncio.sleep(0.05)
        assert not task.done()                 # waits for the rebuild instead of returning
        b.rx.tb = new_tb                       # rebuild finished
    await task
    assert new_tb.zoom == 4
    assert b.rx.gains["fft_zoom"] == 4
    b.rx.tb = None


# -- ADS-L through the real flowgraph, on a fake device replaying synthetic IQ --

def _repo_module(name):
    """web-trx/backend/tests shadows the repository's own tests package, so load these by path."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "tests"
    spec = importlib.util.spec_from_file_location(f"pluto_{name}", root / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _adsl_iq(rate, packets, lo_hz=868_850_000):
    import numpy as np

    gen = _repo_module("adsl_testgen")
    pad = 0.05
    x = np.zeros(int((max(p[1] for p in packets) + 0.02 + pad) * rate), np.complex128)
    for channel_hz, start_s, address in packets:
        burst = gen.modulate(gen.air_chips(gen.frame(gen.traffic_clear(address=address))), rate)
        start = int((start_s + pad) * rate)
        t = np.arange(start, start + len(burst))
        x[start:start + len(burst)] += burst * np.exp(2j * np.pi * (channel_hz - lo_hz) * t / rate)
    return x.astype(np.complex64)


async def _adsl_backend(tmp_path, monkeypatch, rate, iq):
    import numpy as np
    from gnuradio import blocks
    from pluto_advanced_rx.devices.base import RxDevice
    from pluto_advanced_rx.devices.pluto import PlutoDevice

    monkeypatch.setenv("WEB_TRX_SETTINGS_PATH", str(tmp_path / "settings.json"))
    built = []

    class FakeRx(PlutoDevice):
        device_type = "fake"
        display_name = "fake"
        DEFAULT_CONNECTION = "x"
        default_sample_rate_hz = rate
        sample_rate_hz_choices = (1_000_000, rate)
        _source = None

        def __init__(self, *a, **k):
            RxDevice.__init__(self, *a, **k)
            built.append(self)

        def build_source(self):
            return blocks.vector_source_c(np.asarray(iq).tolist(), False)

        def set_frequency(self, hz):
            pass

        def set_gain(self, name, value):
            pass

        def set_gain_mode(self, mode):
            pass

        def read_hw_state(self):
            return {}

    monkeypatch.setitem(radio_backend.rx_devices.DEVICE_REGISTRY, "fake", FakeRx)
    monkeypatch.setitem(radio_backend.DEVICE_LABELS, "fake", "fake")
    b, events, _spectrum, audio = make_backend()
    b._loop = asyncio.get_running_loop()  # start_background_tasks() would do this
    b.rx.device_type, b.rx.connection = "fake", "x"
    b.rx.freq_hz = 433_500_000.0
    return b, events, audio, built


def _read_json(path):
    with open(path) as f:
        return json.load(f)


async def _settle(b, events, name, count, seconds=5.0):
    for _ in range(int(seconds / 0.05)):
        if sum(1 for n, _f in events if n == name) >= count:
            return
        await asyncio.sleep(0.05)


async def test_adsl_packets_reach_events_state_and_stay_silent(tmp_path, monkeypatch):
    rate = 2_400_000
    iq = _adsl_iq(rate, [(868_200_000, 0.0, 0x111111), (868_400_000, 0.05, 0x222222)])
    b, events, audio, _built = await _adsl_backend(tmp_path, monkeypatch, rate, iq)
    b._rx_local_audio = True  # even a server with local audio enabled must stay quiet in this mode
    muted = []
    original = radio_backend.AdvancedRxFlowgraph.set_rx_muted
    monkeypatch.setattr(radio_backend.AdvancedRxFlowgraph, "set_rx_muted", lambda self, m: (muted.append(m), original(self, m))[1])
    await b.select_mode("rx", "adsl", {})
    assert b.rx.tb.nominal_freq_hz == 868_850_000
    assert b.rx.tb.active_digimode == "adsl"
    await asyncio.to_thread(b.rx.tb.wait)
    for channel in b.rx.tb.adsl_channels:
        channel[3]._detector.flush()
    await _settle(b, events, "adsl_packet", 2)
    packets = [f for n, f in events if n == "adsl_packet"]
    assert {(p["channel_hz"], p["address"]) for p in packets} == {(868_200_000, "111111"), (868_400_000, "222222")}
    assert all(json.dumps(p) and "time" in p for p in packets)
    assert audio == []
    assert muted == [True]
    assert {a["label"] for a in b.adsl_snapshot()["aircraft"]} == {"FANET:111111", "FANET:222222"}
    assert ("tuned", {"direction": "rx", "freq_hz": 868_850_000}) in events
    await b.disconnect("rx")


async def test_adsl_keeps_the_operators_frequency_in_the_saved_settings(tmp_path, monkeypatch):
    rate = 2_400_000
    b, _events, _audio, _built = await _adsl_backend(tmp_path, monkeypatch, rate, _adsl_iq(rate, [(868_200_000, 0.0, 1)]))
    b._settings_path = str(tmp_path / "settings.json")
    await b.select_mode("rx", "adsl", {})
    assert b.rx.freq_hz == 868_850_000
    saved = _read_json(b._settings_path)
    assert saved["rx"]["freq_hz"] == 433_500_000.0
    assert saved["rx"]["mode"] == "adsl"
    await b.select_mode("rx", "fm", {})
    assert b.rx.freq_hz == 433_500_000.0
    await b.disconnect("rx")


async def test_adsl_tuning_and_too_low_rates(tmp_path, monkeypatch):
    rate = 2_400_000
    b, _events, _audio, _built = await _adsl_backend(tmp_path, monkeypatch, rate, _adsl_iq(rate, [(868_200_000, 0.0, 1)]))
    b.rx.gains["sample_rate"] = 1_000_000  # saved for another mode: stays saved, ADS-L takes the device default
    await b.select_mode("rx", "adsl", {})
    assert b.rx.tb.sample_rate == rate
    assert b.rx.gains["sample_rate"] == 1_000_000
    with pytest.raises(SessionError, match="tunes itself"):
        await b.tune("rx", 145e6)
    with pytest.raises(SessionError, match="needs at least"):
        await b.set_gain("rx", "sample_rate", 1_000_000)
    await b.disconnect("rx")


def _as_rtl(monkeypatch):
    fake = radio_backend.rx_devices.DEVICE_REGISTRY["fake"]
    real = radio_backend.rx_devices.DEVICE_REGISTRY["rtlsdr"]
    monkeypatch.setattr(fake, "gain_stages", real.gain_stages)
    monkeypatch.setattr(fake, "agc_modes", real.agc_modes)
    monkeypatch.setattr(fake, "default_gain_mode", real.default_gain_mode)
    monkeypatch.setitem(radio_backend.rx_devices.DEVICE_REGISTRY, "rtlsdr", fake)


async def _rtl_adsl_backend(tmp_path, monkeypatch):
    rate = 2_400_000
    b, events, _audio, _built = await _adsl_backend(tmp_path, monkeypatch, rate, _adsl_iq(rate, [(868_200_000, 0.0, 1)]))
    _as_rtl(monkeypatch)
    b.rx.device_type = "rtlsdr"
    b._settings_path = str(tmp_path / "settings.json")
    return b, events


async def test_adsl_raises_an_untouched_rtl_gain_and_undoes_it(tmp_path, monkeypatch):
    b, events = await _rtl_adsl_backend(tmp_path, monkeypatch)
    await b.select_mode("rx", "adsl", {})
    assert b.rx.gains["stage:TUNER"] == radio_backend.rx_config.ADSL_RTL_TUNER_GAIN_DB
    assert ("rx_gain", {"name": "TUNER", "value": 40.0}) in events
    assert radio_backend._rx_gain_build_args(radio_backend.rx_devices.DEVICE_REGISTRY["rtlsdr"], b.rx.gains)[:2] == ("manual", 40.0)
    assert "stage:TUNER" not in _read_json(b._settings_path)["rx"]["gains"]
    await b.select_mode("rx", "adsl", {})  # a rebuild in the mode changes nothing
    assert b.rx.gains["stage:TUNER"] == 40.0
    await b.select_mode("rx", "fm", {})
    assert "stage:TUNER" not in b.rx.gains
    assert ("rx_gain", {"name": "TUNER", "value": 20.0}) in events
    await b.disconnect("rx")


async def test_adsl_keeps_a_gain_the_operator_chose(tmp_path, monkeypatch):
    b, events = await _rtl_adsl_backend(tmp_path, monkeypatch)
    b.rx.gains["stage:TUNER"] = 12.5
    await b.select_mode("rx", "adsl", {})
    assert b.rx.gains["stage:TUNER"] == 12.5
    assert not [e for e in events if e[0] == "rx_gain"]
    await b.select_mode("rx", "fm", {})
    assert b.rx.gains["stage:TUNER"] == 12.5
    await b.disconnect("rx")


async def test_adsl_leaves_agc_alone(tmp_path, monkeypatch):
    b, _events = await _rtl_adsl_backend(tmp_path, monkeypatch)
    b.rx.gains["gain_mode"] = "agc"
    await b.select_mode("rx", "adsl", {})
    assert "stage:TUNER" not in b.rx.gains
    await b.disconnect("rx")


async def test_a_gain_changed_during_adsl_becomes_the_operators(tmp_path, monkeypatch):
    b, _events = await _rtl_adsl_backend(tmp_path, monkeypatch)
    await b.select_mode("rx", "adsl", {})
    await b.set_gain("rx", "stage:TUNER", 30.0)
    assert _read_json(b._settings_path)["rx"]["gains"]["stage:TUNER"] == 30.0
    await b.select_mode("rx", "fm", {})
    assert b.rx.gains["stage:TUNER"] == 30.0
    await b.disconnect("rx")


async def test_adsl_picks_its_view_and_leaves_the_saved_zoom_alone(tmp_path, monkeypatch):
    b, events = await _rtl_adsl_backend(tmp_path, monkeypatch)
    b.rx.gains["fft_zoom"] = 16
    await b.select_mode("rx", "adsl", {})
    assert b.rx.tb.fft_probe.zoom == 4
    assert abs(b.rx.tb.fft_probe.view_offset_hz + 550_000) < 1000  # to a whole FFT bin
    assert ("rx_fft_zoom", {"value": 4}) in events
    await b.set_gain("rx", "fft_zoom", 8)
    assert b.rx.tb.fft_probe.zoom == 8
    assert b.rx.gains["fft_zoom"] == 16
    await b.select_mode("rx", "fm", {})
    assert b.rx.tb.fft_probe.zoom == 16
    assert b.rx.tb.fft_probe.view_offset_hz == 0
    assert ("rx_fft_zoom", {"value": 16}) in events
    await b.disconnect("rx")


async def test_spectrum_rows_carry_the_center_of_the_view(tmp_path, monkeypatch):
    monkeypatch.setattr(radio_backend, "SPECTRUM_POLL_HZ", 200)
    b, _events = await _rtl_adsl_backend(tmp_path, monkeypatch)
    frames = []

    async def on_spectrum(frame):
        frames.append(frame)

    b._on_spectrum = on_spectrum
    task = asyncio.create_task(b._spectrum_loop())
    await b.select_mode("rx", "adsl", {})
    for _ in range(200):
        if frames:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    assert frames, "no spectrum rows"
    assert abs(frames[-1].center_hz - 868_300_000) < 1000
    assert frames[-1].span_hz == 600_000
    await b.disconnect("rx")


# -- ADS-B through the real flowgraph --

async def test_adsb_through_the_real_flowgraph_stays_silent_and_batches(tmp_path, monkeypatch):
    import numpy as np

    gen = _repo_module("adsb_testgen")
    rate = 2_400_000
    rng = np.random.default_rng(1)
    parts = [np.zeros(int(0.03 * rate), np.complex128)]
    for m in (gen.callsign_message(0x3C6444, "DLH123"), gen.position_message(0x3C6444, 53.5, 9.5, 35000, 0),
              gen.position_message(0x3C6444, 53.5, 9.5, 35000, 1)):
        parts += [gen.iq(m, rate, delay_us=float(rng.random())), np.zeros(int(0.001 * rate), np.complex128)]
    x = np.concatenate(parts + [np.zeros(int(0.03 * rate), np.complex128)])
    x = (x + 0.03 * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))).astype(np.complex64)
    b, events, audio, _built = await _adsl_backend(tmp_path, monkeypatch, rate, x)
    b.station = {"call": "", "locator": "JO43"}
    await b.select_mode("rx", "adsb", {})
    assert b.rx.tb.nominal_freq_hz == 1_090_000_000
    assert b.rx.tb.fft_probe.zoom == 1
    assert ("tuned", {"direction": "rx", "freq_hz": 1_090_000_000}) in events
    await asyncio.to_thread(b.rx.tb.wait)
    b.rx.tb.adsb_deframer.detector.flush()
    await b._emit_adsb_update(b.rx.tb)
    update = next(f for n, f in events if n == "adsb_update")
    assert update["count"] == 1
    a = update["aircraft"][0]
    assert (a["icao"], a["callsign"], a["alt_ft"]) == ("3C6444", "DLH123", 35000)
    assert abs(a["lat"] - 53.5) < 0.01
    assert "trail" not in a
    json.dumps(update)
    assert audio == []
    assert b.adsb_snapshot()["aircraft"][0]["trail"]
    assert b._adsb_state.reference == (53.5, 9.0)
    await b.adsb_clear()
    assert b.adsb_snapshot() == {"aircraft": []}
    await b.disconnect("rx")


async def test_adsb_refuses_a_too_low_rate_and_retuning(tmp_path, monkeypatch):
    rate = 2_400_000
    b, _e, _a, _b = await _adsl_backend(tmp_path, monkeypatch, rate, _adsl_iq(rate, [(868_200_000, 0.0, 1)]))
    b.rx.gains["sample_rate"] = 1_000_000
    await b.select_mode("rx", "adsb", {})
    assert b.rx.tb.sample_rate == rate
    with pytest.raises(SessionError, match="ADSB tunes itself"):
        await b.tune("rx", 145e6)
    with pytest.raises(SessionError, match="needs at least"):
        await b.set_gain("rx", "sample_rate", 1_000_000)
    await b.disconnect("rx")


# -- APRS --

def _aprs_iq(rate, info, source="DL1ABC-9"):
    import numpy as np
    from pluto_tx import aprs, aprs_codec

    audio = aprs.encode_frame(aprs_codec.build_frame(source, "APRS", ("WIDE1-1",), info), sample_rate=rate)
    iq = np.exp(2j * np.pi * 3000 / aprs.AMPLITUDE * np.cumsum(audio) / rate).astype(np.complex64)
    pad = np.zeros(int(0.1 * rate), np.complex64)
    return np.concatenate([pad, iq, pad])


async def test_aprs_through_the_real_flowgraph(tmp_path, monkeypatch):
    rate = 1_000_000
    iq = _aprs_iq(rate, b"!5330.00N/00900.00E-TEST <b>")
    b, events, _audio, _built = await _adsl_backend(tmp_path, monkeypatch, rate, iq)
    await b.select_mode("rx", "aprs", {})
    assert b.rx.tb.nominal_freq_hz == 144_800_000
    assert ("tuned", {"direction": "rx", "freq_hz": 144_800_000}) in events
    await asyncio.to_thread(b.rx.tb.wait)
    await b._emit_aprs_update(b.rx.tb)
    update = next(f for n, f in events if n == "aprs_update")
    json.dumps(update)
    assert update["frames_ok"] == 1
    assert update["packets"][0]["comment"] == "TEST <b>"
    station = update["stations"][0]
    assert (station["label"], station["lat"], "trail" in station) == ("DL1ABC-9", 53.5, False)
    assert b.aprs_snapshot()["stations"][0]["trail"]
    await b.aprs_clear()
    assert b.aprs_snapshot()["stations"] == []
    await b.disconnect("rx")


async def test_aprs_frequency_is_only_a_starting_point(tmp_path, monkeypatch):
    rate = 1_000_000
    b, *_ = await _adsl_backend(tmp_path, monkeypatch, rate, _aprs_iq(rate, b">x"))
    await b.select_mode("rx", "aprs", {})
    await b.tune("rx", 432_150_000.0)
    assert b.rx.freq_hz == 432_150_000.0
    await b.select_mode("rx", "aprs", {})  # again: must keep what the operator chose
    assert b.rx.freq_hz == 432_150_000.0
    await b.select_mode("rx", "fm", {})
    assert b.rx.freq_hz == 433_500_000.0  # leaving brings back the frequency from before
    await b.disconnect("rx")


async def _aprs_tx_backend(monkeypatch, freq_hz=144_800_000.0):
    fakes = _repo_module("fakes")
    monkeypatch.setitem(radio_backend.tx_devices.DEVICE_REGISTRY, "fake", fakes.FakeTxDevice)
    monkeypatch.setitem(radio_backend.DEVICE_LABELS, "fake", "fake")
    radio_backend.PlutoTxFlowgraph._aprs_last_sent = -1e9
    b, events, *_ = make_backend()
    b._loop = asyncio.get_running_loop()
    b.station = {"call": "DA2JH", "locator": "JO43"}
    b.tx.device_type, b.tx.connection, b.tx.freq_hz = "fake", "x", freq_hz
    b.rx.freq_hz = 144_800_000.0
    params = modes.normalize_params("tx", "aprs", {"comment": "TEST", "path": "none"})
    await b.select_mode("tx", "aprs", params)
    return b, events


MESSAGE_TO_US = {"kind": "message", "source": "DL1ABC", "addressee": "DA2JH", "message": "Hi", "message_id": "7"}


async def _aprs_unkey(b):
    if b.keyed:
        await b.ptt(False)
    await b.disconnect("tx")


async def test_aprs_beacon_from_station_data(monkeypatch):
    b, events = await _aprs_tx_backend(monkeypatch)
    await b.ptt(True)
    sent = next(f for n, f in events if n == "aprs_sent")
    assert (sent["source"], sent["path"], sent["comment"], sent["lat"]) == ("DA2JH", [], "TEST", 53.5)
    assert sent["automatic"] is False
    await _aprs_unkey(b)
    with pytest.raises(SessionError):
        await b.ptt(True)  # disconnected
    assert not b.keyed


async def test_aprs_sending_early_warns_but_goes_out(monkeypatch):
    b, events = await _aprs_tx_backend(monkeypatch)
    await b.ptt(True)
    await b.ptt(False)
    await b.ptt(True)
    assert b.keyed
    first, second = [f for n, f in events if n == "aprs_sent"]
    assert first["early_s"] == 0
    assert second["early_s"] > 25
    await _aprs_unkey(b)


async def test_aprs_ack_respects_the_interval_a_person_may_ignore(monkeypatch):
    b, events = await _aprs_tx_backend(monkeypatch)
    await b.ptt(True)
    await b.ptt(False)
    await b.aprs_auto_ack(True)
    await b._aprs_maybe_ack(MESSAGE_TO_US)
    assert [n for n, _ in events].count("aprs_sent") == 1
    result = next(f for n, f in events if n == "aprs_ack_result")
    assert result["sent"] is False and "30 s" in result["reason"]
    await _aprs_unkey(b)


async def test_aprs_refuses_outside_the_amateur_bands(monkeypatch):
    b, events = await _aprs_tx_backend(monkeypatch, freq_hz=868_200_000.0)
    with pytest.raises(SessionError, match="amateur"):
        await b.ptt(True)
    assert "keyed" not in [n for n, _ in events]
    await _aprs_unkey(b)


async def test_aprs_never_acks_with_the_switch_off(monkeypatch):
    b, events = await _aprs_tx_backend(monkeypatch)
    assert b.aprs_snapshot()["auto_ack"] is False
    await b._aprs_maybe_ack(MESSAGE_TO_US)
    assert "keyed" not in [n for n, _ in events]
    assert "aprs_ack_result" not in [n for n, _ in events]
    await _aprs_unkey(b)


async def test_aprs_ack_goes_out_once_on_the_heard_frequency(monkeypatch):
    b, events = await _aprs_tx_backend(monkeypatch)
    await b.aprs_auto_ack(True)
    await b._aprs_maybe_ack({**MESSAGE_TO_US, "addressee": "DL9XYZ"})  # someone else's
    assert "keyed" not in [n for n, _ in events]
    await b._aprs_maybe_ack(MESSAGE_TO_US)
    sent = next(f for n, f in events if n == "aprs_sent")
    assert (sent["kind"], sent["addressee"], sent["message_id"], sent["automatic"]) == ("ack", "DL1ABC", "7", True)
    assert ("aprs_ack_result", {"to": "DL1ABC", "message_id": "7", "sent": True, "reason": ""}) in events
    await b._aprs_maybe_ack(MESSAGE_TO_US)  # its digipeated copy
    assert [n for n, _ in events].count("aprs_sent") == 1
    assert [n for n, _ in events].count("aprs_ack_result") == 1
    await _aprs_unkey(b)


async def test_aprs_ack_stays_home_on_another_frequency(monkeypatch):
    b, events = await _aprs_tx_backend(monkeypatch, freq_hz=432_150_000.0)
    await b.aprs_auto_ack(True)
    await b._aprs_maybe_ack(MESSAGE_TO_US)
    assert "keyed" not in [n for n, _ in events]
    result = next(f for n, f in events if n == "aprs_ack_result")
    assert result["sent"] is False and "Empfangsfrequenz" in result["reason"]
    await _aprs_unkey(b)


async def test_aprs_ack_does_not_change_the_operators_next_message(monkeypatch):
    b, events = await _aprs_tx_backend(monkeypatch)
    await b.aprs_auto_ack(True)
    await b._aprs_maybe_ack(MESSAGE_TO_US)
    await b.ptt(False)
    radio_backend.PlutoTxFlowgraph._aprs_last_sent = -1e9
    await b.ptt(True)
    last = [f for n, f in events if n == "aprs_sent"][-1]
    assert (last["kind"], last["automatic"]) == ("position", False)
    await _aprs_unkey(b)


async def test_aprs_ack_switch_goes_off_with_the_operator_and_estop(monkeypatch):
    b, events = await _aprs_tx_backend(monkeypatch)
    await b.aprs_auto_ack(True)
    b.operator_gone()
    await asyncio.sleep(0)
    assert b.aprs_snapshot()["auto_ack"] is False
    assert ("aprs_auto_ack", {"enabled": False, "reason": "operator_gone"}) in events
    await b.aprs_auto_ack(True)
    await b.estop()
    assert b.aprs_snapshot()["auto_ack"] is False
    await _aprs_unkey(b)


def test_aprs_params():
    assert modes.normalize_params("rx", "aprs", {}) == {}
    defaults = modes.normalize_params("tx", "aprs", {})
    assert (defaults["kind"], defaults["path"], defaults["symbol"]) == ("beacon", "WIDE1-1,WIDE2-1", "/-")
    assert modes.normalize_params("tx", "aprs", {"to": " dl1abc "})["to"] == "DL1ABC"
    for bad in ({"kind": "object"}, {"path": "WIDE7-7"}, {"symbol": "x"}, {"text": "x" * 68}, {"freq": 1}):
        with pytest.raises(ValueError):
            modes.normalize_params("tx", "aprs", bad)
    assert modes.APRS_MAX_TEXT_LEN == radio_backend.aprs_codec.MAX_MESSAGE_TEXT
    assert modes.APRS_PATH_DEFAULT == ",".join(radio_backend.tx_config.APRS_DEFAULT_PATH)


async def test_aprs_web_transmit_produces_a_decodable_signal(monkeypatch):
    """Not only the events: what actually reaches the device has to decode."""
    import numpy as np
    from pluto_advanced_rx.aprs_demod import AprsFrameDetector

    b, _events = await _aprs_tx_backend(monkeypatch)
    await b.ptt(True)
    await asyncio.sleep(b.tx.tb.aprs_hold_s + 0.3)
    iq = np.array(b.tx.tb.device.sink.data(), dtype=np.complex64)
    await _aprs_unkey(b)
    assert len(iq) > 0
    on = np.flatnonzero(np.abs(iq) > 0.01)
    assert len(on) > 0, "nothing but silence reached the device"
    iq = iq[on[0]:on[-1]]
    rate = 2_500_000
    audio = np.angle(iq[1:] * np.conj(iq[:-1])) * rate / (2 * np.pi * 3000)
    decim = 50
    audio = np.convolve(audio, np.ones(decim) / decim, "same")[::decim]
    got = []
    AprsFrameDetector(rate / decim, got.append).feed(audio.astype(np.float32))
    assert [f["source"] for f in got] == ["DA2JH"]
