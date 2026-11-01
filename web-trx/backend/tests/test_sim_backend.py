"""Unit tests against SimBackend directly (no WebSocket, no asyncio server)
-- the fast layer of the debugging strategy from docs/PROJECT_PLAN.md: pure
Python/numpy, no GNU Radio/libiio required, runs anywhere including this
dev container and CI."""
import asyncio

import numpy as np
import pytest

from web_trx import modes
from web_trx.session import AudioFrame, SessionError
from web_trx.sim_backend import AUDIO_SAMPLE_RATE_HZ, SimBackend


def test_fake_ft8_slot_has_a_cq_and_a_reply_to_our_call():
    b = SimBackend()
    b.station = {"call": "DA2JH", "locator": "JO43"}
    slot = b.fake_ft8_slot(1790600115.0)
    texts = [d["text"] for d in slot["decodes"]]
    assert slot["utc"] == "125515"
    assert any(t.startswith("CQ ") for t in texts)
    assert any(t.startswith("DA2JH ") for t in texts)
    assert all(d["sender"] and d["sender"] in d["text"] for d in slot["decodes"])


def make_backend():
    b = SimBackend()
    events = []
    spectrum_frames = []
    audio_frames = []

    async def emit_event(name, fields):
        events.append((name, fields))

    async def on_spectrum(frame):
        spectrum_frames.append(frame)

    async def on_audio(frame):
        audio_frames.append(frame)

    b.bind(emit_event, on_spectrum, on_audio)
    return b, events, spectrum_frames, audio_frames


@pytest.mark.asyncio
async def test_connect_then_select_mode_then_tune():
    b, _events, _spectrum, _audio = make_backend()
    await b.connect("rx", "sim", "")
    await b.select_mode("rx", "fm", {})
    await b.tune("rx", 145_500_000.0)
    assert b.rx.mode == "fm"
    assert b.rx.freq_hz == 145_500_000.0


@pytest.mark.asyncio
async def test_select_mode_before_connect_is_refused():
    b, _events, _spectrum, _audio = make_backend()
    with pytest.raises(SessionError):
        await b.select_mode("rx", "fm", {})


@pytest.mark.asyncio
async def test_unsupported_mode_is_refused():
    b, _events, _spectrum, _audio = make_backend()
    await b.connect("tx", "sim", "")
    with pytest.raises(SessionError):
        await b.select_mode("tx", "not-a-real-mode", {})


@pytest.mark.asyncio
async def test_ptt_without_tx_mode_is_refused():
    b, _events, _spectrum, _audio = make_backend()
    with pytest.raises(SessionError):
        await b.ptt(True)


@pytest.mark.asyncio
async def test_fm_ptt_on_off_emits_keyed_unkeyed():
    b, events, _spectrum, _audio = make_backend()
    await b.connect("tx", "sim", "")
    await b.select_mode("tx", "fm", {})
    await b.ptt(True)
    assert b.keyed is True
    await b.ptt(False)
    assert b.keyed is False
    assert ("keyed", {"mode": "fm"}) in events
    assert ("unkeyed", {"mode": "fm"}) in events


@pytest.mark.asyncio
async def test_pocsag_ptt_auto_unkeys_and_emits_message():  # walking-skeleton mode, see PROJECT_PLAN.md
    b, events, _spectrum, _audio = make_backend()
    await b.connect("tx", "sim", "")
    await b.select_mode("tx", "pocsag", {"ric": 42, "text": "hi"})
    await b.ptt(True)
    assert any(name == "pocsag_message" and fields["ric"] == 42 for name, fields in events)
    assert b.keyed is True
    await b._pocsag_unkey_task  # await the simulated hold instead of sleeping in the test
    assert b.keyed is False


@pytest.mark.asyncio
async def test_estop_forces_unkey_even_mid_pocsag():
    b, events, _spectrum, _audio = make_backend()
    await b.connect("tx", "sim", "")
    await b.select_mode("tx", "pocsag", {"ric": 1, "text": "x"})
    await b.ptt(True)
    assert b.keyed is True
    await b.estop()
    assert b.keyed is False
    assert ("estop", {}) in events


@pytest.mark.asyncio
async def test_estop_is_idempotent_when_not_keyed():
    b, _events, _spectrum, _audio = make_backend()
    await b.estop()
    await b.estop()  # must not raise


@pytest.mark.asyncio
async def test_spectrum_row_shape_and_dtype():
    b, _events, _spectrum, _audio = make_backend()
    await b.connect("rx", "sim", "")
    row = b._synthetic_row()
    assert row.dtype == np.float32
    assert row.shape == (2048,)
    assert np.all(np.isfinite(row))


@pytest.mark.asyncio
async def test_snapshot_reflects_state():
    b, _events, _spectrum, _audio = make_backend()
    await b.connect("rx", "sim", "")
    await b.select_mode("rx", "ssb", {})
    snap = b.snapshot()
    assert snap["rx"]["mode"] == "ssb"
    assert snap["tx"]["mode"] is None


@pytest.mark.asyncio
async def test_audio_loop_silent_until_connected_and_moded():
    b, _events, _spectrum, audio = make_backend()
    task = asyncio.ensure_future(b._audio_loop())
    try:
        await asyncio.sleep(0.05)
        assert audio == []  # not connected yet -- no audio should be produced
        await b.connect("rx", "sim", "")
        await asyncio.sleep(0.05)
        assert audio == []  # connected but no mode selected yet
        await b.select_mode("rx", "fm", {})
        await asyncio.sleep(0.15)  # > AUDIO_CHUNK_S, one tick should land
    finally:
        task.cancel()
    assert len(audio) >= 1
    frame: AudioFrame = audio[0]
    assert frame.sample_rate_hz == AUDIO_SAMPLE_RATE_HZ
    assert len(frame.pcm16) > 0
    assert len(frame.pcm16) % 2 == 0  # whole int16 samples


@pytest.mark.asyncio
async def test_submit_tx_audio_is_counted():
    b, _events, _spectrum, _audio = make_backend()
    assert b.tx_audio_frames_received == 0
    await b.submit_tx_audio(AudioFrame(pcm16=b"\x00\x01\x02\x03", sample_rate_hz=48_000))
    assert b.tx_audio_frames_received == 1


async def test_ft8_loopback_shows_our_transmission_in_its_own_slot():
    b, _events, _spectrum, _audio = make_backend()
    b.station = {"call": "DA2JH", "locator": "JO43"}
    b._ft8_sent = [(1790600115.0, "CQ DA2JH JO43")]
    before = [d["text"] for d in b.fake_ft8_slot(1790600100.0)["decodes"]]
    during = [d["text"] for d in b.fake_ft8_slot(1790600115.0)["decodes"]]
    assert "CQ DA2JH JO43" not in before
    assert "CQ DA2JH JO43" in during
    assert b._ft8_sent == []


async def test_js8_neighbour_and_loopback_are_assembled_like_the_real_backend():
    b, _events, _spectrum, _audio = make_backend()
    b.station = {"call": "DA2JH", "locator": "JO43"}
    from web_trx import js8_series

    frames, text = js8_series.build_message(b.station, {"kind": "allcall", "text": "HELLO", "to": "",
                                                        "report_db": -10, "submode": "normal"})
    t0 = 1790600100.0                                     # n % 4 == 0 for 15 s periods? whatever: loopback check
    b._js8_sent = [(t0 + 15 * i, 0, f, fl) for i, (f, fl) in enumerate(frames)]
    events = []
    for i in range(len(frames)):
        events += b.fake_js8_period(t0 + 15 * i, 0)
    names = [n for n, _ in events]
    assert names.count("js8_frames") == len(frames)
    mine = [f for n, f in events if n == "js8_message" and f["sender"] == "DA2JH" and f["closed"]]
    assert [m["text"] for m in mine] == [text] and mine[0]["complete"]
    assert b._js8_sent == []
    other = [f for n, f in events if n == "js8_message" and f["sender"] == "DL1ABC"]
    assert other and all(m["text"].startswith("DL1ABC: ") for m in other)


@pytest.mark.asyncio
async def test_aprs_receive_and_send():
    b, events, _spectrum, _audio = make_backend()
    b.station = {"call": "DA2JH", "locator": "JO43"}
    frames = b.fake_aprs_frames()
    rows = [b._aprs().on_frame(f) for f in frames]
    assert {r["kind"] for r in rows} >= {"position", "weather", "object", "status", "message"}
    assert next(r for r in rows if r["kind"] == "message")["addressee"] == "DA2JH"
    assert any(s["label"] == "438.600FM" for s in b.aprs_snapshot()["stations"])
    await b.connect("tx", "sim", "")
    await b.select_mode("tx", "aprs", modes.normalize_params("tx", "aprs", {"kind": "message", "to": "DL1ABC", "text": "Hi"}))
    await b.ptt(True)
    sent = next(f for n, f in events if n == "aprs_sent")
    assert (sent["kind"], sent["addressee"], sent["message"]) == ("message", "DL1ABC", "Hi")
    await b.ptt(False)
    await b.select_mode("tx", "aprs", modes.normalize_params("tx", "aprs", {"kind": "message"}))
    with pytest.raises(SessionError):
        await b.ptt(True)
    assert not b.keyed


@pytest.mark.asyncio
async def test_meshcore_receive_channels_and_send():
    pytest.importorskip("cryptography")
    b, events, _spectrum, _audio = make_backend()
    b.station = {"call": "DA2JH", "locator": "JO43"}
    await b.meshcore_options({"decrypt_dm": True})
    state = b._meshcore()
    for raw in b.fake_meshcore_frames():
        state.on_frame(raw)
    rows, nodes = state.take_changed()
    assert sorted(n["name"] for n in nodes) == ["Sim Alice <b>", "Sim Repeater", "Sim ohne Position"]
    assert {r["kind"] for r in rows} == {"advert", "group_text", "direct_text"}
    snapshot = b.meshcore_snapshot()
    assert not any("hex" in r for r in snapshot["packets"])
    key = "00112233445566778899aabbccddeeff"
    await b.meshcore_channel_set("Privat", key)
    with pytest.raises(SessionError):
        await b.meshcore_channel_set("x", "zz")
    await b.connect("tx", "sim", "")
    params = modes.normalize_params("tx", "meshcore", {"kind": "group", "channel": "Privat", "text": "Hi", "name": "N"})
    before = b.tx.freq_hz
    await b.select_mode("tx", "meshcore", params)
    assert b.tx.freq_hz == 869_618_000.0
    with pytest.raises(SessionError):
        await b.tune("tx", before)
    await b.ptt(True)
    sent = next(f for n, f in events if n == "meshcore_sent")
    assert (sent["kind"], sent["channel"], sent["text"]) == ("group_text", "Privat", "Hi")
    await b.ptt(False)
    assert key not in repr(events) and key not in repr(b.meshcore_snapshot())
    await b.select_mode("tx", "meshcore", modes.normalize_params("tx", "meshcore", {"kind": "direct", "text": "Hi"}))
    with pytest.raises(SessionError):
        await b.ptt(True)
    assert not b.keyed
    await b.select_mode("tx", "fm", modes.normalize_params("tx", "fm", {}))
    assert b.tx.freq_hz == before


async def test_jtty_receive_grows_and_send():
    b, events, _spec, _audio = make_backend()
    now = 1_800_000_000.0
    for tick in range(4):
        for update in b.fake_jtty_updates(tick, now):
            b._jtty().on_update(update)
    rows = b.jtty_snapshot()["messages"]
    assert [(r["text"], r["state"]) for r in rows] == [("CQ DL1ABC HALLO 73", "complete")]
    b._jtty().take_changed()
    await b.connect("tx", "sim", "")
    await b.select_mode("tx", "jtty", modes.normalize_params("tx", "jtty", {"text": "cq da2jh"}))
    await b.ptt(True)
    sent = next(f for n, f in events if n == "jtty_sent")
    assert sent["text"] == "CQ DA2JH"
    assert sent["messages"][0]["direction"] == "tx"
    await b.ptt(False, "ptt_off")
    await b.jtty_clear()
    assert b.jtty_snapshot()["messages"] == []


async def test_jtty_empty_text_is_refused():
    b, _events, _spec, _audio = make_backend()
    await b.connect("tx", "sim", "")
    await b.select_mode("tx", "jtty", modes.normalize_params("tx", "jtty", {"text": "  "}))
    with pytest.raises(SessionError):
        await b.ptt(True)
    assert not b.keyed


async def test_jtty_preview_says_what_goes_out():
    b, _events, _spec, _audio = make_backend()
    assert (await b.jtty_preview("", 1500.0))["problem"]
    assert (await b.jtty_preview("x" * 81, 1500.0))["problem"]
    assert (await b.jtty_preview("hello", 1500.0))["frames"] == 1
