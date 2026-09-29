"""JS8 transmit series: the frames of one message in consecutive periods of its speed
(pluto-tx docs/js8/SPEC.md 3.1), each keyed and unkeyed on its own -- the same loop as
pluto_cli.runtime._run_js8_series, as an asyncio task that can be cancelled at any point.

Pure Python, no GNU Radio: clock, sleep, the slot plan (pluto_tx.js8.plan_frames) and the key / unkey /
abort actions are injected, so both backends share it and the tests run it with a simulated clock.

The hard rules (as in ft8_series.py): after a cancel, key() is never called again -- and a cancel that
arrives while key() is in flight (the flowgraph keys in a worker thread, it can't be interrupted) unkeys
right after it. A cancel also calls abort() (PlutoTxFlowgraph.js8_cancel()), so the flowgraph itself
never sends another frame of the message, keyed or not.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger("web_trx.js8")

JS8_MAX_FRAMES = 20  # pluto_tx.js8.MAX_FRAMES; the operator limit in pluto-tx docs/JS8_PLAN.md section 0

Plan = Callable[[float], list]  # now -> [(key_at, start_at)] per frame
Emit = Callable[[str, dict], Awaitable[None]]


def build_message(station: dict, params: dict) -> tuple[list, str]:
    """-> (frames, text as a receiver shows it) for the (already normalised) JS8 TX parameters, built by
    pluto-tx' js8_message exactly as JS8Call builds it. Raises ValueError with a readable reason.
    pluto_tx.js8* need only numpy (no GNU Radio), so this works with the sim backend too; imported late
    so plain imports of this module touch no sys.path (docs/DEBUGGING.md)."""
    from . import pluto_path

    pluto_path.ensure_importable()
    from pluto_tx import js8, js8_message

    call = station.get("call", "")
    if not call:
        raise ValueError("no station callsign set")
    submode = js8.submode_from_name(params["submode"])
    text = js8_message.compose(params["kind"], call, station.get("locator", ""), params.get("to", ""),
                               params.get("text", ""), params.get("report_db", -10))
    if not text:
        raise ValueError("no message (the addressed station or the text is missing)")
    frames = js8_message.build_frames(call, station.get("locator", ""), text, submode)
    if len(frames) > JS8_MAX_FRAMES:
        raise ValueError(f"message too long: {len(frames)} frames (max {JS8_MAX_FRAMES})")
    return frames, js8_message.frames_text(frames, submode).strip()


def transmission_time_s(n_frames: int, speed: str) -> float:
    """From the first frame's period start to the end of the last frame (pluto_tx.js8.transmission_time_s)."""
    from . import pluto_path

    pluto_path.ensure_importable()
    from pluto_tx import js8

    return round(js8.transmission_time_s(n_frames, js8.submode_from_name(speed)), 2)


class Js8Series:
    """One armed message. key(i, start_at) keys the transmitter for frame i (starting at start_at) and
    returns how long to stay keyed; unkey() ends one frame; abort() drops the rest in the flowgraph.
    Events: js8_armed once, js8_frame after each frame, js8_done after the last, js8_cancelled when it
    ends early."""

    def __init__(self, *, plan: Plan, key: Callable[[int, float], Awaitable[float]],
                 unkey: Callable[[], Awaitable[None]], abort: Callable[[], Awaitable[None]], emit: Emit,
                 count: int, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, info: dict | None = None):
        if not 1 <= count <= JS8_MAX_FRAMES:
            raise ValueError(f"a JS8 message has 1..{JS8_MAX_FRAMES} frames")
        self._plan, self._key, self._unkey, self._abort, self._emit = plan, key, unkey, abort, emit
        self._now, self._sleep = now, sleep
        self.count = count
        self.info = dict(info or {})
        self.frame = 0
        self.sent = 0
        self.keyed = False
        self._keying = False
        self._cancelled = False
        self._skip_unkey = False
        self._task: asyncio.Task | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def armed(self) -> bool:
        return self._task is not None and not self._task.done() and not self._cancelled

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def cancel(self, reason: str, *, device_safe: bool = False) -> bool:
        """End the message now. device_safe=True (E-STOP: the device was already forced safe) skips the
        unkey and never waits. Returns False if there was nothing to cancel."""
        task = self._task
        if task is None or task.done() or self._cancelled:
            return False
        self._cancelled = True
        self._skip_unkey = device_safe
        try:
            await self._abort()
        except Exception:  # the flowgraph may already be gone (E-STOP, disconnect)
            logger.exception("JS8 abort failed")
        if not self._keying:
            task.cancel()
        if not device_safe and task is not asyncio.current_task():
            await asyncio.wait({task})
        await self._emit("js8_cancelled", {"reason": reason, "sent": self.sent, "of": self.count})
        return True

    async def _run(self) -> None:
        try:
            now = self._now()
            plan = self._plan(now)
            if len(plan) != self.count:
                raise ValueError(f"plan has {len(plan)} frames, expected {self.count}")
            first_key, first_start = plan[0]
            await self._emit("js8_armed", {
                **self.info, "frames": self.count, "start_at": first_start,
                "starts": [round(s, 3) for _, s in plan],
                "slot_utc": time.strftime("%H:%M:%S", time.gmtime(first_start)),
                "key_in_s": round(max(0.0, first_key - now), 1),
            })
            for i, (key_at, start_at) in enumerate(plan):
                self.frame = i + 1
                await self._sleep(max(0.0, key_at - self._now()))
                if self._cancelled:
                    return
                self._keying = True
                try:
                    hold_s = await self._key(i, start_at)
                    self.keyed = True
                finally:
                    self._keying = False
                if self._cancelled:  # cancelled while key() was running: unkey at once (finally)
                    return
                await self._sleep(hold_s)
                self.keyed = False
                await asyncio.shield(self._unkey())
                self.sent = i + 1
                await self._emit("js8_frame", {"frame_no": i + 1, "of": self.count})
            await self._emit("js8_done", {"of": self.count})
        except asyncio.CancelledError:
            pass
        except Exception as e:  # a refusal from the flowgraph (band, message, ...)
            if not self._cancelled:
                logger.exception("JS8 series failed")
                self._cancelled = True
                try:
                    await self._abort()
                except Exception:
                    logger.exception("JS8 abort failed")
                await self._emit("js8_cancelled", {"reason": "error", "message": str(e),
                                                   "sent": self.sent, "of": self.count})
        finally:
            if self.keyed and not self._skip_unkey:
                self.keyed = False
                await self._unkey()
            self.keyed = False
