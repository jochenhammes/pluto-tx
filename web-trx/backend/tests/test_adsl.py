"""ADS-L in Web-TRX: mode handling, simulated traffic and the WebSocket protocol, against SimBackend."""
import json

import pytest

from web_trx import modes, station
from web_trx.session import SessionError
from web_trx.sim_backend import ADSL_LO_HZ, SimBackend

from .test_server_ws import login, make_client


def make_backend():
    b = SimBackend()
    events = []

    async def emit(name, fields):
        events.append((name, fields))

    async def ignore(_frame):
        pass

    b.bind(emit, ignore, ignore)
    return b, events


async def connected_backend():
    b, events = make_backend()
    await b.connect("rx", "sim", "sim:0")
    await b.tune("rx", 433_500_000.0)
    await b.select_mode("rx", "fm", {})
    return b, events


def test_mode_parameters():
    assert modes.normalize_params("rx", "adsl", {}) == {}
    with pytest.raises(ValueError):
        modes.normalize_params("rx", "adsl", {"anything": 1})


@pytest.mark.parametrize(("locator", "expected"), [
    ("JO43", (53.5, 9.0)), ("JO31", (51.5, 7.0)), ("AA00", (-89.5, -179.0)), ("RR99", (89.5, 179.0)),
    ("JO43AA", (53 + 0.5 / 24, 8 + 0.5 / 12)), ("", None),
])
def test_locator_center(locator, expected):
    assert station.locator_center(locator) == (pytest.approx(expected) if expected else None)


async def test_mode_tunes_to_the_lo_and_leaving_restores():
    b, events = await connected_backend()
    await b.select_mode("rx", "adsl", {})
    assert b.rx.freq_hz == ADSL_LO_HZ
    assert ("tuned", {"direction": "rx", "freq_hz": ADSL_LO_HZ}) in events
    await b.select_mode("rx", "adsl", {})  # selected again: the operator's frequency must not become the LO
    await b.select_mode("rx", "fm", {})
    assert b.rx.freq_hz == 433_500_000.0
    assert events[-1] == ("tuned", {"direction": "rx", "freq_hz": 433_500_000.0})


async def test_tuning_is_refused_while_connected_and_remembered_while_not():
    b, _ = await connected_backend()
    await b.select_mode("rx", "adsl", {})
    with pytest.raises(SessionError, match="tunes itself"):
        await b.tune("rx", 145_000_000.0)
    assert b.rx.freq_hz == ADSL_LO_HZ
    await b.disconnect("rx")
    await b.tune("rx", 145_000_000.0)
    await b.connect("rx", "sim", "sim:0")
    await b.select_mode("rx", "fm", {})
    assert b.rx.freq_hz == 145_000_000.0


async def test_simulated_packets_are_complete_and_json_ready():
    b, _ = await connected_backend()
    packets = b.fake_adsl_packets()
    assert {p["address_table_name"] for p in packets} == {"FLARM", "ICAO", "OGN", "RND"}
    assert any(p["emergency"] == "general" for p in packets)
    assert any(p["lat"] is None for p in packets)
    assert all(p["channel_hz"] in (868_200_000, 868_400_000) for p in packets)
    json.dumps(packets)


async def test_simulation_circles_around_the_station():
    b, _ = await connected_backend()
    station_data = {"call": "", "locator": "JO62"}
    b.set_station(station_data)
    glider = next(p for p in b.fake_adsl_packets() if p["category"] == "glider")
    assert abs(glider["lat"] - 52.5) < 0.5
    assert abs(glider["lon"] - 13.0) < 0.5


async def test_snapshot_keeps_the_trail_without_invalid_positions():
    b, _ = await connected_backend()
    for _ in range(3):
        for info in b.fake_adsl_packets():
            b._adsl_state.on_packet(info)
    aircraft = {a["label"]: a for a in b.adsl_snapshot()["aircraft"]}
    json.dumps(aircraft)
    assert len(aircraft["FLARM:DD1234"]["trail"]) == 3
    assert aircraft["FLARM:DD1234"]["kinds"] == ["traffic"]
    assert aircraft["RND:51F0AA"]["trail"] == []
    assert aircraft["RND:51F0AA"]["packets"] == 3
    await b.adsl_clear()
    assert b.adsl_snapshot() == {"aircraft": []}


def receive_until(ws, name, limit=2000):
    for _ in range(limit):
        message = ws.receive()
        if message.get("text") and json.loads(message["text"]).get("event") == name:
            return json.loads(message["text"])
    raise AssertionError(f"no {name} event")


def test_protocol_over_websocket_and_reload():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            hello = receive_until(ws, "hello")
            assert hello["adsl"] == {"aircraft": []}
            assert "adsl" in hello["features"]["rx_modes"]
            assert "map_tile_url" in hello["features"]
            ws.send_json({"request": "connect", "direction": "rx", "device_type": "sim", "connection": "sim:0"})
            ws.send_json({"request": "select_mode", "direction": "rx", "mode": "adsl", "params": {}})
            assert receive_until(ws, "tuned")["freq_hz"] == ADSL_LO_HZ
            packet = receive_until(ws, "adsl_packet")
            assert packet["address_label"].split(":")[0] in ("FLARM", "ICAO", "OGN", "RND")
            assert "time" in packet and "inverted" not in packet
            status = receive_until(ws, "adsl_status")
            assert [c["channel_hz"] for c in status["channels"]] == [868_200_000, 868_400_000]
            ws.send_json({"request": "tune", "direction": "rx", "freq_hz": 145_000_000})
            assert "tunes itself" in receive_until(ws, "error")["message"]
        with client.websocket_connect("/ws") as ws:  # the page was reloaded
            hello = receive_until(ws, "hello")
            assert len(hello["adsl"]["aircraft"]) >= 1
            ws.send_json({"request": "adsl_clear"})
            receive_until(ws, "adsl_cleared")
            ws.send_json({"request": "select_mode", "direction": "rx", "mode": "fm", "params": {}})
            assert receive_until(ws, "tuned")["freq_hz"] != ADSL_LO_HZ


# -- ADS-B, same handling --

async def test_adsb_tunes_and_restores():
    b, events = await connected_backend()
    await b.select_mode("rx", "adsb", {})
    assert b.rx.freq_hz == 1_090_000_000
    with pytest.raises(SessionError, match="ADSB tunes itself"):
        await b.tune("rx", 145e6)
    await b.select_mode("rx", "adsl", {})
    assert b.rx.freq_hz == ADSL_LO_HZ
    await b.select_mode("rx", "fm", {})
    assert b.rx.freq_hz == 433_500_000.0
    assert events[-1] == ("tuned", {"direction": "rx", "freq_hz": 433_500_000.0})


def test_adsb_mode_parameters():
    assert modes.normalize_params("rx", "adsb", {}) == {}
    with pytest.raises(ValueError):
        modes.normalize_params("rx", "adsb", {"anything": 1})


async def test_adsb_simulation():
    b, _ = await connected_backend()
    changed = b.fake_adsb_step(100.0)
    json.dumps(changed)
    assert all("trail" not in a for a in changed)
    assert {a["callsign"] for a in changed} >= {"DLH4KA", "BAW912"}
    assert any(a["lat"] is None for a in changed)
    b.fake_adsb_step(101.0)
    snapshot = {a["icao"]: a for a in b.adsb_snapshot()["aircraft"]}
    json.dumps(snapshot)
    assert len(snapshot["3C6444"]["trail"]) == 2
    assert snapshot["4B1A22"]["trail"] == []
    await b.adsb_clear()
    assert b.adsb_snapshot() == {"aircraft": []}


def test_adsb_protocol_over_websocket_and_reload():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            hello = receive_until(ws, "hello")
            assert hello["adsb"] == {"aircraft": []}
            assert "adsb" in hello["features"]["rx_modes"]
            ws.send_json({"request": "connect", "direction": "rx", "device_type": "sim", "connection": "sim:0"})
            ws.send_json({"request": "select_mode", "direction": "rx", "mode": "adsb", "params": {}})
            assert receive_until(ws, "tuned")["freq_hz"] == 1_090_000_000
            update = receive_until(ws, "adsb_update")
            assert update["count"] >= 1
            assert all("trail" not in a for a in update["aircraft"])
        with client.websocket_connect("/ws") as ws:
            hello = receive_until(ws, "hello")
            assert len(hello["adsb"]["aircraft"]) >= 1
            assert "trail" in hello["adsb"]["aircraft"][0]
            ws.send_json({"request": "adsb_clear"})
            receive_until(ws, "adsb_cleared")

