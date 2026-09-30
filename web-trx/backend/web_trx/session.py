"""Session-backend interface + the WebSocket-facing SessionManager.

Exactly one operator, exactly one physical device per direction (tx/rx) at
a time -- see docs/PROJECT_PLAN.md section 3. Two SessionBackend
implementations are planned: SimBackend (sim_backend.py, pure Python/numpy,
no GNU Radio/libiio needed -- what this dev container can actually run) and
a later GnuRadioBackend (radio_backend.py, only runs where pluto-tx's
GNU Radio/libiio dependencies are installed) that wires up
PlutoTxFlowgraph/AdvancedRxFlowgraph/FftProbe/PlutoSafety following the
exact pattern pluto_cli/runtime.py already proves works. SessionManager
itself never imports GNU Radio and is fully testable against SimBackend.
"""
from __future__ import annotations

import abc
import asyncio
import dataclasses
import logging
import os
import time
from collections.abc import Awaitable, Callable

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from . import ft8_series, js8_series, modes, protocol, station

logger = logging.getLogger("web_trx.session")

# Operator gone (last browser closed, network lost): every transmission ends
# at once; if nobody is back within NO_OPERATOR_GRACE_S (a page reload, a
# short network drop), RX and TX are disconnected -- the devices never keep
# running unattended. After a server start (connections may have been
# restored) the same happens if no browser connects within STARTUP_GRACE_S.
NO_OPERATOR_GRACE_S = float(os.environ.get("WEB_TRX_NO_OPERATOR_S", "10"))
STARTUP_GRACE_S = 60.0

Direction = str  # "tx" | "rx"


class SessionError(Exception):
    """A refused request (bad mode/frequency/device, wrong state, ...) --
    becomes an 'error' event to the requesting client, never a raw
    traceback. Mirrors pluto-cli's own 'error' event (see
    pluto_cli/README.md section 6)."""


@dataclasses.dataclass
class SpectrumFrame:
    row: np.ndarray  # float32 dB, low-to-high frequency
    center_hz: float
    span_hz: float
    generation: int


@dataclasses.dataclass
class AudioFrame:
    pcm16: bytes
    sample_rate_hz: int


EventSink = Callable[[str, dict], Awaitable[None]]
SpectrumSink = Callable[[SpectrumFrame], Awaitable[None]]
AudioSink = Callable[[AudioFrame], Awaitable[None]]


class SessionBackend(abc.ABC):
    """One radio session. `bind()` is called exactly once, before any other
    method, wiring the three sinks the backend uses to push events/data
    up to connected clients -- the backend never talks to a WebSocket
    directly, so it stays testable (and, for SimBackend, importable) with
    zero web/asyncio-server dependencies beyond these plain callables."""

    def bind(self, emit_event: EventSink, on_spectrum: SpectrumSink, on_audio: AudioSink) -> None:
        self._emit_event = emit_event
        self._on_spectrum = on_spectrum
        self._on_audio = on_audio

    def device_types(self) -> dict[str, list[list[str]]]:
        """{direction: [[device_type, label], ...]} -- what the frontend's
        device dropdowns offer. Sent with the 'hello' event."""
        return {"rx": [], "tx": []}

    # The operator's station data (web_trx/station.py). Every backend sets the
    # start value (station.from_environment()) in __init__; GnuRadioBackend
    # also persists it.
    station: dict

    def set_station(self, value: dict) -> None:
        """value is already validated by station.normalize()."""
        self.station = dict(value)

    # JS8 automation (pluto-tx J9, web_trx/js8_automation.py) -- optional; backends that have it override these.
    async def js8_automation_request(self, action: str, params: dict) -> None:
        raise SessionError("JS8 automation is not available")

    def operator_activity(self) -> None:
        """Any request from a browser: the operator is there (JS8 idle watchdog)."""

    def operator_gone(self) -> None:
        """No browser connected any more: automation switches itself off at once."""

    def features(self) -> dict:
        """Optional capabilities for the frontend, sent with 'hello'. E.g.
        {"fft_zoom_max": 64}: the backend zooms the spectrum itself
        (set_gain rx fft_zoom), so the frontend must not crop rows."""
        return {}

    @abc.abstractmethod
    async def scan(self, direction: Direction, device_type: str) -> dict[str, str]:
        """Returns {connection_string: human_label}."""

    @abc.abstractmethod
    async def connect(self, direction: Direction, device_type: str, connection: str) -> None:
        ...

    @abc.abstractmethod
    async def disconnect(self, direction: Direction) -> None:
        ...

    @abc.abstractmethod
    async def select_mode(self, direction: Direction, mode: str, params: dict) -> None:
        ...

    @abc.abstractmethod
    async def tune(self, direction: Direction, freq_hz: float) -> None:
        ...

    @abc.abstractmethod
    async def set_gain(self, direction: Direction, name: str, value: float) -> None:
        ...

    @abc.abstractmethod
    async def ptt(self, on: bool, reason: str = "ptt_off") -> None:
        """FT8/JS8: on arms a transmit series (ft8_series.py / js8_series.py), off
        cancels an armed one -- `reason` goes into its *_cancelled event."""

    @abc.abstractmethod
    async def estop(self) -> None:
        """Immediate, idempotent, must never raise -- mirrors PlutoSafety's
        force_safe_state() (pluto_tx/safety.py)."""

    @abc.abstractmethod
    async def submit_tx_audio(self, frame: AudioFrame) -> None:
        """Microphone audio from the browser, consumed while PTT is on and
        the active TX mode is audio-based. Silently dropped otherwise."""

    @abc.abstractmethod
    def snapshot(self) -> dict:
        """Current session state for a newly-connected client's 'hello'
        event (device/mode/frequency/keyed per direction)."""

    @abc.abstractmethod
    async def shutdown(self) -> None:
        """Safe teardown, idempotent -- called on server shutdown."""


class SessionManager:
    """Owns the single SessionBackend instance and every connected
    WebSocket. Multiple browser tabs from the same operator MAY connect
    concurrently (all receive the same broadcast events/spectrum/audio,
    and any of them can issue requests) -- 'never more than one user' is a
    deployment assumption (single shared login, see docs/PROJECT_PLAN.md),
    not a second lock enforced here."""

    REQUESTS = (
        "scan", "connect", "disconnect", "select_mode", "tune", "set_gain",
        "ptt_on", "ptt_off", "estop", "set_station", "js8_auto",
    )

    def __init__(self, backend: SessionBackend, tx_log=None):
        self.backend = backend
        self.backend.bind(self._emit_event, self._on_spectrum, self._on_audio)
        self._clients: set[WebSocket] = set()
        self.tx_log = tx_log  # TxLog | None, see txlog.py
        self._no_operator_task: asyncio.Task | None = None

    def start(self) -> None:
        """Called once the event loop runs (server startup)."""
        self._schedule_no_operator(STARTUP_GRACE_S, stop_tx_now=False)

    def _schedule_no_operator(self, grace_s: float, stop_tx_now: bool) -> None:
        if self._no_operator_task is not None:
            self._no_operator_task.cancel()
        self._no_operator_task = asyncio.create_task(self._operator_gone(grace_s, stop_tx_now))

    async def _operator_gone(self, grace_s: float, stop_tx_now: bool) -> None:
        try:
            tx = self.backend.snapshot()["tx"]
            if stop_tx_now and (tx.get("keyed") or tx.get("ft8_armed") or tx.get("js8_armed")):
                logger.warning("no browser connected -- ending the transmission")
                await self.backend.ptt(False, reason="no_operator")
            await asyncio.sleep(grace_s)
        except asyncio.CancelledError:
            return
        except Exception:  # never leave the TX up because of an error here
            logger.exception("ending the transmission failed, forcing E-STOP")
            await self.backend.estop()
        if self._clients:
            return
        self._no_operator_task = None
        snap = self.backend.snapshot()
        for direction in ("tx", "rx"):
            if snap[direction].get("connection") is None:
                continue
            logger.warning("no browser for %.0f s -- disconnecting %s", grace_s, direction)
            try:
                await self.backend.disconnect(direction)
                await self._emit_event("disconnected", {"direction": direction, "reason": "no_operator"})
            except Exception:
                logger.exception("disconnecting %s failed", direction)
                if direction == "tx":
                    await self.backend.estop()

    @property
    def backend_name(self) -> str:
        return type(self.backend).__name__

    async def handle_connection(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        if self._no_operator_task is not None:  # operator back in time: keep the devices
            self._no_operator_task.cancel()
            self._no_operator_task = None
        try:
            await ws.send_json({
                "event": "hello", "backend": self.backend_name,
                "device_types": self.backend.device_types(), "features": self.backend.features(),
                "mode_options": modes.client_options(), "station": self.backend.station,
                # FT8 slot clock in the browser: offset between its clock and ours.
                "server_time": time.time(),
                **self.backend.snapshot(),
            })
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                text = message.get("text")
                data = message.get("bytes")
                if text is not None:
                    await self._handle_text(text)
                elif data is not None:
                    await self._handle_binary(data)
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(ws)
            if not self._clients:
                try:
                    self.backend.operator_gone()  # JS8 automation off at once, before any grace period
                except Exception:
                    logger.exception("switching the JS8 automation off failed")
                self._schedule_no_operator(NO_OPERATOR_GRACE_S, stop_tx_now=True)

    async def _handle_text(self, text: str) -> None:
        import json

        try:
            payload = json.loads(text)
        except ValueError:
            await self._emit_event("error", {"message": "malformed JSON request"})
            return
        request = payload.get("request")
        if request not in self.REQUESTS:
            await self._emit_event("error", {"message": f"unknown request '{request}'"})
            return
        params = {k: v for k, v in payload.items() if k != "request"}
        try:
            self.backend.operator_activity()
            await self._dispatch(request, params)
        except SessionError as e:
            await self._emit_event("error", {"message": str(e), "request": request,
                                             "direction": params.get("direction")})
        except Exception as e:
            logger.exception("unhandled error handling request %r", request)
            await self._emit_event("error", {"message": f"internal error: {e}", "request": request,
                                             "direction": params.get("direction")})

    async def _dispatch(self, request: str, params: dict) -> None:
        b = self.backend
        if request == "scan":
            devices = await b.scan(params["direction"], params["device_type"])
            await self._emit_event("scanned", {"direction": params["direction"], "devices": devices})
        elif request == "connect":
            await b.connect(params["direction"], params["device_type"], params.get("connection") or "")
            await self._emit_event("connected", {"direction": params["direction"]})
        elif request == "disconnect":
            await b.disconnect(params["direction"])
            await self._emit_event("disconnected", {"direction": params["direction"]})
        elif request == "select_mode":
            try:
                mode_params = modes.normalize_params(params["direction"], params["mode"], params.get("params") or {})
            except ValueError as e:
                raise SessionError(str(e)) from e
            await b.select_mode(params["direction"], params["mode"], mode_params)
            await self._emit_mode(params["direction"], params["mode"], mode_params)
        elif request == "tune":
            await b.tune(params["direction"], float(params["freq_hz"]))
            await self._emit_event("tuned", {"direction": params["direction"], "freq_hz": params["freq_hz"]})
        elif request == "set_gain":
            value = params["value"]  # a number, or a string for choices like the RX AGC mode
            await b.set_gain(params["direction"], params["name"], value if isinstance(value, str) else float(value))
        elif request == "ptt_on":
            await b.ptt(True)
        elif request == "ptt_off":
            await b.ptt(False)
        elif request == "estop":
            await b.estop()
        elif request == "js8_auto":
            await b.js8_automation_request(str(params.get("action", "")), params)
        elif request == "set_station":
            try:
                value = station.normalize(params.get("call", ""), params.get("locator", ""))
            except ValueError as e:
                raise SessionError(str(e)) from e
            b.set_station(value)
            await self._emit_event("station", value)
            tx = b.snapshot()["tx"]
            if tx.get("mode") in ("ft8", "js8"):  # the message text depends on the station data
                await self._emit_mode("tx", tx["mode"], tx.get("mode_params") or {})

    async def _emit_mode(self, direction: str, mode: str, mode_params: dict) -> None:
        fields = {"direction": direction, "mode": mode, "params": mode_params}
        if direction == "tx" and mode == "ft8":
            # Preview of what will actually be sent (pluto-tx' compose()).
            try:
                fields["text"] = ft8_series.compose_text(self.backend.station, mode_params)
            except (RuntimeError, ImportError, ValueError):
                logger.exception("composing the FT8 message failed")
                fields["text"] = ""
        if direction == "tx" and mode == "js8":
            # Preview: what receivers will show, and how many frames (pluto-tx' js8_message).
            try:
                frames, fields["text"] = js8_series.build_message(self.backend.station, mode_params)
                fields["frames"] = len(frames)
                fields["duration_s"] = js8_series.transmission_time_s(len(frames), mode_params["submode"])
                fields["problem"] = ""
            except ValueError as e:
                fields.update(text="", frames=0, duration_s=0.0, problem=str(e))
            except (RuntimeError, ImportError):
                logger.exception("building the JS8 message failed")
                fields.update(text="", frames=0, duration_s=0.0, problem="JS8 codec not available")
        await self._emit_event("mode", fields)

    async def _handle_binary(self, data: bytes) -> None:
        if not data or protocol.peek_frame_type(data) != protocol.BinaryFrameType.TX_AUDIO:
            return
        sample_rate_hz, pcm16 = protocol.decode_tx_audio(data)
        await self.backend.submit_tx_audio(AudioFrame(pcm16=pcm16, sample_rate_hz=sample_rate_hz))

    async def _emit_event(self, name: str, fields: dict) -> None:
        if self.tx_log is not None:
            self._record_tx_log(name, fields)
        await self._broadcast_json({"event": name, **fields})

    def _record_tx_log(self, event_name: str, fields: dict) -> None:
        """keyed -> open a record (using the backend's own tx snapshot for
        mode/freq/device/params, not the event's fields -- keeps this
        working for ANY SessionBackend, not just fields SimBackend happens
        to put on its own 'keyed' event). unkeyed/estop -> close it;
        closing twice (e.g. estop after an already-unkeyed state) is a
        harmless no-op, see TxLog.record_unkeyed()."""
        if event_name == "keyed":
            tx = self.backend.snapshot()["tx"]
            params = dict(tx.get("mode_params") or {})
            if "text" in fields:  # FT8/JS8: the message actually sent, built from station data + params
                params["text"] = fields["text"]
            if "frame" in fields:  # JS8: one record per frame
                params["frame"], params["of"] = fields["frame"], fields.get("of")
            self.tx_log.record_keyed(
                mode=tx.get("mode"), freq_hz=tx.get("freq_hz"),
                device_type=tx.get("device_type"), connection=tx.get("connection"),
                params=params,
            )
        elif event_name in ("unkeyed", "estop"):
            self.tx_log.record_unkeyed()

    async def _on_spectrum(self, frame: SpectrumFrame) -> None:
        payload = protocol.encode_spectrum_row(frame.row, frame.center_hz, frame.span_hz, frame.generation)
        await self._broadcast_bytes(payload)

    async def _on_audio(self, frame: AudioFrame) -> None:
        payload = protocol.encode_rx_audio(frame.pcm16, frame.sample_rate_hz)
        await self._broadcast_bytes(payload)

    async def _broadcast_json(self, payload: dict) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 -- a dead client must not break the others
                self._clients.discard(ws)

    async def _broadcast_bytes(self, payload: bytes) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_bytes(payload)
            except Exception:  # noqa: BLE001
                self._clients.discard(ws)
