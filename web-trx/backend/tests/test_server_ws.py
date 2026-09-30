"""End-to-end test of the whole control/spectrum protocol through the real
FastAPI WebSocket layer, against SimBackend -- the second layer of the
debugging strategy from docs/PROJECT_PLAN.md (no browser, no asyncio
subtleties left to chance, still no GNU Radio/hardware required)."""
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web_trx import protocol, session
from web_trx.auth import AuthManager
from web_trx.server import create_app
from web_trx.sim_backend import SimBackend

TEST_PASSWORD = "test-password"


def make_client(client_addr: tuple[str, int] | None = None) -> TestClient:
    """Returns an un-entered TestClient -- every test uses `with
    make_client() as client:` so the ASGI lifespan (startup/shutdown, see
    server.py) actually runs on the same event loop that serves the
    WebSocket, which is what makes SimBackend's background spectrum task
    (asyncio.create_task in start_background_tasks()) work at all. A fixed
    password (rather than server.py's default "generate and print a
    random one") and an in-memory TX log keep tests deterministic and
    file-free."""
    app = create_app(SimBackend(), auth=AuthManager(password=TEST_PASSWORD), tx_log_path=":memory:")
    return TestClient(app, client=client_addr) if client_addr else TestClient(app)


def login(client: TestClient) -> None:
    """TestClient persists cookies across requests on the same instance
    (like a browser session) -- logging in here is enough for a
    subsequent websocket_connect()/request on the same `client` to carry
    the session cookie automatically."""
    resp = client.post("/login", json={"password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text


def test_health():
    with make_client() as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "backend": "SimBackend"}


def test_health_reports_devices_to_loopback_only():
    """The local TX/RX apps (pluto_tx/webtrx_control.py) read which SDRs the
    server holds; a remote client without a login does not get to see it."""
    with make_client(("127.0.0.1", 40000)) as client:
        body = client.get("/health").json()
        assert body["devices"] == {"rx": {"connected": False, "device_type": None},
                                   "tx": {"connected": False, "device_type": None}}
    with make_client(("::1", 40000)) as client:
        assert "devices" in client.get("/health").json()
    with make_client(("192.168.1.20", 40000)) as client:
        assert "devices" not in client.get("/health").json()


def test_health_devices_follow_connect_and_disconnect():
    with make_client(("127.0.0.1", 40000)) as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"request": "connect", "direction": "tx", "device_type": "sim"})
            assert ws.receive_json()["event"] == "connected"
            devices = client.get("/health").json()["devices"]
            assert devices["tx"] == {"connected": True, "device_type": "sim"}
            assert devices["rx"]["connected"] is False
            ws.send_json({"request": "disconnect", "direction": "tx"})
            assert ws.receive_json()["event"] == "disconnected"
            assert client.get("/health").json()["devices"]["tx"]["connected"] is False


def test_health_counts_a_pending_restore_as_connected():
    from web_trx.server import device_summary

    backend = SimBackend()
    backend.restoring = lambda: {"tx"}
    backend.tx.device_type = "pluto"
    assert device_summary(backend)["tx"] == {"connected": True, "device_type": "pluto"}
    assert device_summary(backend)["rx"]["connected"] is False


def test_health_requests_stay_out_of_the_access_log():
    import logging

    create_app(SimBackend(), auth=AuthManager(password=TEST_PASSWORD), tx_log_path=":memory:")
    create_app(SimBackend(), auth=AuthManager(password=TEST_PASSWORD), tx_log_path=":memory:")
    access = logging.getLogger("uvicorn.access")
    assert len([f for f in access.filters if type(f).__name__ == "_HealthAccessFilter"]) == 1

    def record(path):
        return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:1", "GET", path, "1.1", 200), None)

    assert not access.filters[0].filter(record("/health"))
    assert access.filters[0].filter(record("/tx-log?limit=5"))


def test_login_wrong_password_is_rejected():
    with make_client() as client:
        resp = client.post("/login", json={"password": "not-it"})
        assert resp.status_code == 401


def test_session_reflects_login_state():
    with make_client() as client:
        assert client.get("/session").json() == {"authenticated": False}
        login(client)
        assert client.get("/session").json() == {"authenticated": True}


def test_logout_ends_session():
    with make_client() as client:
        login(client)
        assert client.get("/session").json()["authenticated"] is True
        resp = client.post("/logout")
        assert resp.status_code == 200
        assert client.get("/session").json()["authenticated"] is False


def test_ws_connect_without_login_is_rejected():
    with make_client() as client, pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws"):
        pass  # rejected before accept() (HTTP 403, see server.py) -- raises on __enter__


def test_hello_on_connect():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["event"] == "hello"
            assert hello["backend"] == "SimBackend"
            assert hello["tx"]["mode"] is None
            assert hello["device_types"] == {"rx": [["sim", "Simulator"]], "tx": [["sim", "Simulator"]]}
            assert hello["mode_options"]["fm"]["deviation_choices_hz"] == [2500.0, 5000.0]


def test_fm_tx_params_are_normalized_and_stored():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"request": "connect", "direction": "tx", "device_type": "sim"})
            assert ws.receive_json()["event"] == "connected"
            ws.send_json({"request": "select_mode", "direction": "tx", "mode": "fm",
                          "params": {"deviation_hz": 5000, "ctcss_hz": 88.5}})
            mode_event = ws.receive_json()
            assert mode_event["event"] == "mode"
            assert mode_event["params"] == {"deviation_hz": 5000.0, "preemphasis": True, "ctcss_hz": 88.5}
        backend = client.app.state.manager.backend
        assert backend.snapshot()["tx"]["mode_params"]["deviation_hz"] == 5000.0


def test_invalid_fm_params_become_error_event_and_keep_previous_mode():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"request": "connect", "direction": "tx", "device_type": "sim"})
            ws.receive_json()  # connected
            ws.send_json({"request": "select_mode", "direction": "tx", "mode": "fm",
                          "params": {"deviation_hz": 3000}})
            err = ws.receive_json()
            assert err["event"] == "error"
            assert "deviation_hz" in err["message"]
        assert client.app.state.manager.backend.snapshot()["tx"]["mode"] is None


def test_full_pocsag_round_trip_over_ws():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello

            ws.send_json({"request": "connect", "direction": "tx", "device_type": "sim"})
            assert ws.receive_json()["event"] == "connected"

            ws.send_json({"request": "select_mode", "direction": "tx", "mode": "pocsag",
                          "params": {"ric": 99, "text": "DE DA2JH"}})
            assert ws.receive_json()["event"] == "mode"

            ws.send_json({"request": "ptt_on"})
            assert ws.receive_json()["event"] == "keyed"
            pocsag_event = ws.receive_json()
            assert pocsag_event["event"] == "pocsag_message"
            assert pocsag_event["ric"] == 99
            assert pocsag_event["text"] == "DE DA2JH"
            assert ws.receive_json()["event"] == "unkeyed"  # simulated auto-hold, see SimBackend.ptt()

        # The keyed/unkeyed pair above should have landed in the TX log
        # (see session.py's SessionManager._record_tx_log()).
        log_resp = client.get("/tx-log")
        assert log_resp.status_code == 200
        entries = log_resp.json()
        assert len(entries) == 1
        assert entries[0]["mode"] == "pocsag"
        assert entries[0]["params"] == {"ric": 99, "text": "DE DA2JH"}
        assert entries[0]["ended_at"] is not None


def test_unknown_request_becomes_error_event_not_a_crash():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"request": "nonsense"})
            err = ws.receive_json()
            assert err["event"] == "error"


def test_refused_request_becomes_error_event():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"request": "ptt_on"})  # tx never connected
            err = ws.receive_json()
            assert err["event"] == "error"
            assert "not connected" in err["message"] or "no mode" in err["message"]


def test_spectrum_binary_frame_after_rx_connect():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"request": "connect", "direction": "rx", "device_type": "sim"})
            assert ws.receive_json()["event"] == "connected"

            raw = ws.receive_bytes()
            assert protocol.peek_frame_type(raw) == protocol.BinaryFrameType.SPECTRUM_ROW
            decoded = protocol.decode_spectrum_row(raw)
            assert decoded["row"].shape == (2048,)


def test_rx_audio_binary_frame_after_connect_and_mode():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"request": "connect", "direction": "rx", "device_type": "sim"})
            assert ws.receive_json()["event"] == "connected"
            ws.send_json({"request": "select_mode", "direction": "rx", "mode": "fm", "params": {}})
            assert ws.receive_json()["event"] == "mode"

            # Spectrum rows (20 Hz) interleave with audio chunks (10 Hz) on
            # the same socket -- skip any spectrum frames until an
            # RX_AUDIO frame shows up, rather than assuming a fixed
            # arrival order.
            for _ in range(20):
                raw = ws.receive_bytes()
                if protocol.peek_frame_type(raw) == protocol.BinaryFrameType.RX_AUDIO:
                    assert len(raw) > 5
                    return
            raise AssertionError("no RX_AUDIO frame arrived")


def test_tx_audio_binary_frame_is_accepted_without_error():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"request": "connect", "direction": "tx", "device_type": "sim"})
            assert ws.receive_json()["event"] == "connected"

            tx_audio_frame = (
                bytes([protocol.BinaryFrameType.TX_AUDIO]) + (48_000).to_bytes(4, "little") + b"\x00\x01" * 100
            )
            ws.send_bytes(tx_audio_frame)
            # No response is expected for a binary frame -- proven by the
            # connection staying open and still answering a normal request.
            ws.send_json({"request": "tune", "direction": "tx", "freq_hz": 432_500_000})
            assert ws.receive_json()["event"] == "tuned"


def test_set_station_is_validated_and_broadcast():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            assert "station" in ws.receive_json()  # hello
            ws.send_json({"request": "set_station", "call": "da2jh", "locator": "jo43"})
            assert ws.receive_json() == {"event": "station", "call": "DA2JH", "locator": "JO43"}
            ws.send_json({"request": "set_station", "call": "not a call", "locator": "JO43"})
            err = ws.receive_json()
            assert err["event"] == "error" and "Rufzeichen" in err["message"]
        assert client.app.state.manager.backend.station == {"call": "DA2JH", "locator": "JO43"}


def _wait_until(cond, timeout=3.0):
    import time as _time

    end = _time.monotonic() + timeout
    while _time.monotonic() < end:
        if cond():
            return True
        _time.sleep(0.02)
    return False


def test_closing_the_last_browser_unkeys_at_once_and_disconnects_after_the_grace_time(monkeypatch):
    from web_trx import session

    monkeypatch.setattr(session, "NO_OPERATOR_GRACE_S", 0.3)
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            for direction in ("rx", "tx"):
                ws.send_json({"request": "connect", "direction": direction, "device_type": "sim"})
                ws.receive_json()
            ws.send_json({"request": "select_mode", "direction": "tx", "mode": "fm", "params": {}})
            ws.receive_json()
            ws.send_json({"request": "ptt_on"})
            assert _wait_until(lambda: backend.keyed)
        # browser gone: the transmission ends right away ...
        assert _wait_until(lambda: not backend.keyed, timeout=0.2)
        assert backend.snapshot()["rx"]["connection"] is not None  # ... devices still within the grace time
        # ... and after the grace time both directions are disconnected
        assert _wait_until(lambda: backend.snapshot()["rx"]["connection"] is None
                           and backend.snapshot()["tx"]["connection"] is None)


def test_a_browser_returning_within_the_grace_time_keeps_the_devices(monkeypatch):
    from web_trx import session

    monkeypatch.setattr(session, "NO_OPERATOR_GRACE_S", 0.5)
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"request": "connect", "direction": "rx", "device_type": "sim"})
            ws.receive_json()
        with client.websocket_connect("/ws") as ws:  # e.g. a page reload
            ws.receive_json()
            _wait_until(lambda: False, timeout=0.8)
            assert backend.snapshot()["rx"]["connection"] is not None


def _fast_ft8(monkeypatch, key_in_s=0.05):
    """SimBackend's FT8 series without waiting for real 15 s slots."""
    import time as _time

    from web_trx import sim_backend

    monkeypatch.setattr(sim_backend, "sim_plan_transmission",
                        lambda now, parity="any": (now + key_in_s, now + key_in_s + 0.05))
    monkeypatch.setattr(sim_backend, "FT8_FRAME_S", 0.1)
    return _time


def _ft8_tx_ready(ws, params=None):
    ws.receive_json()  # hello
    ws.send_json({"request": "set_station", "call": "DA2JH", "locator": "JO43"})
    assert ws.receive_json()["event"] == "station"
    ws.send_json({"request": "connect", "direction": "tx", "device_type": "sim"})
    assert ws.receive_json()["event"] == "connected"
    ws.send_json({"request": "select_mode", "direction": "tx", "mode": "ft8", "params": params or {}})
    return ws.receive_json()


def test_ft8_series_round_trip_over_ws(monkeypatch):
    _fast_ft8(monkeypatch)
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            mode = _ft8_tx_ready(ws, {"kind": "report", "dx_call": "dl1abc", "report_db": -7, "repeat_count": 2})
            assert mode["event"] == "mode"
            assert mode["text"] == "DL1ABC DA2JH -07"  # preview, built by pluto-tx' compose()
            assert mode["params"]["dx_call"] == "DL1ABC"
            ws.send_json({"request": "ptt_on"})
            names = []
            while "ft8_done" not in names:
                event = ws.receive_json()
                names.append(event["event"])
                if event["event"] == "keyed":
                    assert event["text"] == "DL1ABC DA2JH -07"
            assert names == ["ft8_armed", "keyed", "tx_text", "unkeyed"] * 2 + ["ft8_done"]
        entries = client.get("/tx-log").json()
        assert len(entries) == 2
        assert all(e["mode"] == "ft8" and e["params"]["text"] == "DL1ABC DA2JH -07" and e["ended_at"]
                   for e in entries)


def test_ft8_ptt_off_while_armed_cancels_without_keying(monkeypatch):
    _fast_ft8(monkeypatch, key_in_s=5.0)
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        with client.websocket_connect("/ws") as ws:
            _ft8_tx_ready(ws)
            ws.send_json({"request": "ptt_on"})
            assert ws.receive_json()["event"] == "ft8_armed"
            assert backend.snapshot()["tx"]["ft8_armed"] is True
            ws.send_json({"request": "select_mode", "direction": "tx", "mode": "fm", "params": {}})
            err = ws.receive_json()
            assert err["event"] == "error" and "armed" in err["message"]
            ws.send_json({"request": "ptt_off"})
            assert ws.receive_json() == {"event": "ft8_cancelled", "reason": "ptt_off"}
            assert not backend.keyed and backend.snapshot()["tx"]["ft8_armed"] is False
        assert client.get("/tx-log").json() == []


def test_ft8_without_station_callsign_is_refused():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"request": "connect", "direction": "tx", "device_type": "sim"})
            ws.receive_json()
            ws.send_json({"request": "select_mode", "direction": "tx", "mode": "ft8", "params": {}})
            assert ws.receive_json()["text"] == ""  # no station callsign: nothing to send
            ws.send_json({"request": "ptt_on"})
            err = ws.receive_json()
            assert err["event"] == "error" and "callsign" in err["message"]


def test_closing_the_last_browser_cancels_an_armed_ft8_series(monkeypatch):
    from web_trx import session

    _fast_ft8(monkeypatch, key_in_s=5.0)
    monkeypatch.setattr(session, "NO_OPERATOR_GRACE_S", 5.0)
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        with client.websocket_connect("/ws") as ws:
            _ft8_tx_ready(ws)
            ws.send_json({"request": "ptt_on"})
            assert ws.receive_json()["event"] == "ft8_armed"
        assert _wait_until(lambda: not backend.snapshot()["tx"]["ft8_armed"], timeout=0.5)
        assert not backend.keyed


def _fast_js8(monkeypatch, key_in_s=0.05):
    """SimBackend's JS8 message without waiting for real periods: frames 0.2 s apart, 0.05 s long."""
    from web_trx import pluto_path

    pluto_path.ensure_importable()
    from pluto_tx import js8

    real_info = js8.speed_info
    monkeypatch.setattr(js8, "plan_frames", lambda now, sm, n, **kw: [(now + key_in_s + 0.2 * i,
                                                                        now + key_in_s + 0.05 + 0.2 * i)
                                                                       for i in range(n)])
    monkeypatch.setattr(js8, "speed_info", lambda sm: dict(real_info(sm), data_duration_s=0.05))


def _js8_tx_ready(ws, params=None):
    ws.receive_json()  # hello
    ws.send_json({"request": "set_station", "call": "DA2JH", "locator": "JO43"})
    assert ws.receive_json()["event"] == "station"
    ws.send_json({"request": "connect", "direction": "tx", "device_type": "sim"})
    assert ws.receive_json()["event"] == "connected"
    ws.send_json({"request": "select_mode", "direction": "tx", "mode": "js8", "params": params or {}})
    return ws.receive_json()


def test_js8_message_round_trip_over_ws(monkeypatch):
    _fast_js8(monkeypatch)
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            mode = _js8_tx_ready(ws, {"kind": "allcall", "text": "js8 sequence test", "submode": "normal"})
            assert mode["event"] == "mode"
            assert mode["text"] == "DA2JH: @ALLCALL  JS8 SEQUENCE TEST"   # preview, pluto-tx' js8_message
            n = mode["frames"]
            assert n >= 2 and mode["problem"] == ""
            ws.send_json({"request": "ptt_on"})
            names = []
            while "js8_done" not in names:
                event = ws.receive_json()
                names.append(event["event"])
                if event["event"] == "keyed":
                    assert event["text"] == "DA2JH: @ALLCALL  JS8 SEQUENCE TEST" and event["of"] == n
            assert names == ["js8_armed", "keyed", "tx_text", "unkeyed", "js8_frame"] + \
                ["keyed", "unkeyed", "js8_frame"] * (n - 1) + ["js8_done"]
        entries = client.get("/tx-log").json()
        assert len(entries) == n                                   # one record per frame
        assert sorted(e["params"]["frame"] for e in entries) == list(range(1, n + 1))
        assert all(e["mode"] == "js8" and e["params"]["text"] == "DA2JH: @ALLCALL  JS8 SEQUENCE TEST"
                   and e["ended_at"] for e in entries)


def test_js8_ptt_off_and_mode_change_cancel_without_keying(monkeypatch):
    _fast_js8(monkeypatch, key_in_s=5.0)
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        with client.websocket_connect("/ws") as ws:
            _js8_tx_ready(ws)
            ws.send_json({"request": "ptt_on"})
            assert ws.receive_json()["event"] == "js8_armed"
            assert backend.snapshot()["tx"]["js8_armed"] is True
            ws.send_json({"request": "ptt_off"})
            assert ws.receive_json() == {"event": "js8_cancelled", "reason": "ptt_off", "sent": 0, "of": 1}
            ws.send_json({"request": "ptt_on"})
            assert ws.receive_json()["event"] == "js8_armed"
            ws.send_json({"request": "select_mode", "direction": "tx", "mode": "fm", "params": {}})
            assert ws.receive_json() == {"event": "js8_cancelled", "reason": "mode_change", "sent": 0, "of": 1}
            assert not backend.keyed and backend.snapshot()["tx"]["js8_armed"] is False
        assert client.get("/tx-log").json() == []


def test_js8_without_station_callsign_is_refused():
    with make_client() as client:
        login(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"request": "connect", "direction": "tx", "device_type": "sim"})
            ws.receive_json()
            ws.send_json({"request": "select_mode", "direction": "tx", "mode": "js8", "params": {}})
            mode = ws.receive_json()
            assert mode["frames"] == 0 and "callsign" in mode["problem"]
            ws.send_json({"request": "ptt_on"})
            err = ws.receive_json()
            assert err["event"] == "error" and "callsign" in err["message"]


def test_closing_the_last_browser_cancels_an_armed_js8_message(monkeypatch):
    from web_trx import session

    _fast_js8(monkeypatch, key_in_s=5.0)
    monkeypatch.setattr(session, "NO_OPERATOR_GRACE_S", 5.0)
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        with client.websocket_connect("/ws") as ws:
            _js8_tx_ready(ws)
            ws.send_json({"request": "ptt_on"})
            assert ws.receive_json()["event"] == "js8_armed"
        assert _wait_until(lambda: not backend.snapshot()["tx"]["js8_armed"], timeout=0.5)
        assert not backend.keyed


def test_js8_sim_loopback_reaches_the_browser_as_a_message():
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            backend.station = {"call": "DA2JH", "locator": "JO43"}
            from web_trx import js8_series

            frames, text = js8_series.build_message(backend.station, {"kind": "snr_query", "to": "DL1ABC",
                                                                      "text": "", "report_db": -10,
                                                                      "submode": "turbo"})
            t0 = 1790600102.0
            backend._js8_sent = [(t0, 2, *frames[0])]

            async def deliver():
                for name, fields in backend.fake_js8_period(t0, 2):
                    await backend._emit_event(name, fields)

            client.portal.call(deliver)
            events = []
            while not any(e["event"] == "js8_message" and e["sender"] == "DA2JH" for e in events):
                events.append(ws.receive_json())
            frames_ev = next(e for e in events if e["event"] == "js8_frames")
            assert frames_ev["speed"] == "turbo" and any(d["frame"] == frames[0][0] for d in frames_ev["decodes"])
            msg = next(e for e in events if e["event"] == "js8_message" and e["sender"] == "DA2JH")
            assert msg["text"] == text and msg["to"] == "DL1ABC" and msg["complete"]



# --- browser responsiveness (session.py heartbeat, docs/BROWSER_AUDIO.md) -------------------------------------


@pytest.fixture
def fast_heartbeat(monkeypatch):
    monkeypatch.setattr(session, "HB_INTERVAL_S", 0.1)
    monkeypatch.setattr(session, "CLIENT_TIMEOUT_S", 0.6)


def _fm_keyed(ws):
    ws.receive_json()  # hello
    ws.send_json({"request": "connect", "direction": "tx", "device_type": "sim"})
    assert ws.receive_json()["event"] == "connected"
    ws.send_json({"request": "select_mode", "direction": "tx", "mode": "fm", "params": {}})
    assert ws.receive_json()["event"] == "mode"
    ws.send_json({"request": "ptt_on"})


def _answer_for(ws, seconds):
    """Reads events for `seconds`, answering every heartbeat -> the events seen."""
    seen = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        event = ws.receive_json()
        seen.append(event)
        if event["event"] == "hb":
            ws.send_json({"request": "hb_ack", "seq": event["seq"]})
    return seen


def _closed_code(ws, max_events=500):
    for _ in range(max_events):
        try:
            ws.receive_json()
        except WebSocketDisconnect as e:
            return e.code
    return None


def test_an_answering_browser_keeps_the_transmission(fast_heartbeat):
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        with client.websocket_connect("/ws") as ws:
            _fm_keyed(ws)
            seen = _answer_for(ws, 1.5)
            assert [e["seq"] for e in seen if e["event"] == "hb"] == sorted(e["seq"] for e in seen if e["event"] == "hb")
            assert sum(e["event"] == "hb" for e in seen) >= 4           # checked every LIVENESS_CHECK_S
            assert backend.keyed
            ws.send_json({"request": "ptt_off"})
            assert _wait_until(lambda: not backend.keyed)


def test_a_silent_browser_ends_the_transmission_and_is_closed(fast_heartbeat):
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        with client.websocket_connect("/ws") as ws:
            _fm_keyed(ws)
            assert _wait_until(lambda: backend.keyed)
            t0 = time.monotonic()
            assert _wait_until(lambda: not backend.keyed, timeout=3.0)      # nobody answers any more
            assert time.monotonic() - t0 < 0.6 + session.LIVENESS_CHECK_S + 0.5
            assert _closed_code(ws) == session.UNRESPONSIVE_CLOSE_CODE


def test_a_second_answering_tab_keeps_the_transmission(fast_heartbeat):
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        with client.websocket_connect("/ws") as silent, client.websocket_connect("/ws") as ws:
            silent.receive_json()                                           # hello, then never again
            _fm_keyed(ws)
            _answer_for(ws, 1.5)
            assert backend.keyed                                            # one live browser is enough
            assert _closed_code(silent) == session.UNRESPONSIVE_CLOSE_CODE
            ws.send_json({"request": "ptt_off"})
            assert _wait_until(lambda: not backend.keyed)


def test_an_armed_js8_message_is_cancelled(fast_heartbeat, monkeypatch):
    _fast_js8(monkeypatch, key_in_s=5.0)
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        events = []
        manager = client.app.state.manager
        orig_backend, orig_manager = backend._emit_event, manager._emit_event

        def spy(orig):
            async def emit(name, fields):
                events.append((name, fields))
                await orig(name, fields)
            return emit
        backend._emit_event = spy(orig_backend)        # js8_cancelled comes from the backend's series
        manager._emit_event = spy(orig_manager)        # tx_aborted from the session
        with client.websocket_connect("/ws") as ws:
            _js8_tx_ready(ws)
            ws.send_json({"request": "ptt_on"})
            assert _wait_until(lambda: backend.snapshot()["tx"]["js8_armed"])
            assert _wait_until(lambda: not backend.snapshot()["tx"]["js8_armed"], timeout=3.0)
            assert not backend.keyed
        cancelled = [f for n, f in events if n == "js8_cancelled"]
        assert cancelled and cancelled[0]["reason"] == "client_unresponsive"
        assert any(n == "tx_aborted" and f["reason"] == "client_unresponsive" for n, f in events)


def test_hb_ack_is_not_operator_activity(fast_heartbeat, monkeypatch):
    calls = []
    with make_client() as client:
        login(client)
        backend = client.app.state.manager.backend
        monkeypatch.setattr(backend, "operator_activity", lambda: calls.append(1))
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()                                               # hello
            _answer_for(ws, 0.5)                                            # only heartbeats answered
            assert calls == []                                              # the JS8 idle watchdog keeps counting
            ws.send_json({"request": "tune", "direction": "rx", "freq_hz": 144_178_000})
            _answer_for(ws, 0.3)
            assert calls == [1]
