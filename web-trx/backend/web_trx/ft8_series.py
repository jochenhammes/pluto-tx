"""FT8 transmit series: armed -> keyed -> pause -> armed ... (docs/FT8_PLAN.md 4.3).

PTT in FT8 does not key at once: each transmission waits for its UTC slot
(pluto_tx.ft8.plan_transmission(): keyed FT8_KEY_EARLY_S ahead, the flowgraph
pads with silence up to the signal start), repetitions every 30 s stay in the
parity of the first one -- the same loop as pluto_cli.runtime._run_ft8_series,
as an asyncio task that can be cancelled at any point.

Pure Python, no GNU Radio: clock, sleep, slot planner and the key/unkey
actions are injected, so both backends share it and the tests run it with a
simulated clock. The one hard rule: after a cancel, key() is never called
again -- and a cancel that arrives while key() is in flight (the flowgraph
keys in a worker thread, it can't be interrupted) unkeys right after it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger("web_trx.ft8")

FT8_SLOT_S = 15.0
FT8_MAX_REPEATS = 20  # 10 min of CQ at most (docs/FT8_PLAN.md, decision 1)

Plan = Callable[[float, str], tuple[float, float]]  # (now, parity) -> (key_at, start_at)
Emit = Callable[[str, dict], Awaitable[None]]


def slot_parity(t: float) -> str:
    """Same rule as pluto_tx.ft8.slot_parity(): "even" = :00/:30, "odd" = :15/:45."""
    return "even" if int(t // FT8_SLOT_S) % 2 == 0 else "odd"


def compose_text(station: dict, params: dict) -> str:
    """The message text the series sends, built by pluto-tx' compose() from the
    station data and the (already normalised) FT8 TX parameters; "" if a
    required field is missing. pluto_tx.ft8 needs only numpy/ctypes, so this
    works with the sim backend too; imported late so plain imports of this
    module touch no sys.path (docs/DEBUGGING.md)."""
    from . import pluto_path

    pluto_path.ensure_importable()
    from pluto_tx import ft8

    return ft8.compose(params["kind"], station.get("call", ""), station.get("locator", ""),
                       params.get("dx_call", ""), params.get("report_db", -10), params.get("free_text", ""))


class Ft8Series:
    """One armed series. key(start_at) keys the transmitter so the signal starts
    at start_at and returns how long to stay keyed; unkey() ends one
    transmission. Events: ft8_armed before each transmission, ft8_done after
    the last one, ft8_cancelled when it ends early."""

    def __init__(self, *, plan: Plan, key: Callable[[float], Awaitable[float]],
                 unkey: Callable[[], Awaitable[None]], emit: Emit, parity: str = "any", count: int = 1,
                 now: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 parity_of: Callable[[float], str] = slot_parity):
        if not 1 <= count <= FT8_MAX_REPEATS:
            raise ValueError(f"repeat_count must be 1..{FT8_MAX_REPEATS}")
        self._plan, self._key, self._unkey, self._emit = plan, key, unkey, emit
        self._now, self._sleep, self._parity_of = now, sleep, parity_of
        self.parity = parity
        self.count = count
        self.repetition = 0
        self.keyed = False
        self._keying = False
        self._cancelled = False
        self._skip_unkey = False
        self._task: asyncio.Task | None = None

    @property
    def cancelled(self) -> bool:
        """key() implementations that wait (e.g. for a lock) check this right
        before keying: a cancel may have arrived in between."""
        return self._cancelled

    @property
    def armed(self) -> bool:
        """True from start() until the series has finished or was cancelled."""
        return self._task is not None and not self._task.done() and not self._cancelled

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def cancel(self, reason: str, *, device_safe: bool = False) -> bool:
        """End the series now. device_safe=True (E-STOP: the device was already
        forced into its safe state) skips the unkey and never waits. Returns
        False if there was nothing to cancel."""
        task = self._task
        if task is None or task.done() or self._cancelled:
            return False
        self._cancelled = True
        self._skip_unkey = device_safe
        if not self._keying:
            # Sleeping (armed) or holding (keyed): stop right there; _run's
            # finally unkeys. While key() runs, the task finishes on its own
            # and unkeys as soon as key() returns (see _run).
            task.cancel()
        if not device_safe and task is not asyncio.current_task():
            await asyncio.wait({task})
        await self._emit("ft8_cancelled", {"reason": reason})
        return True

    async def _run(self) -> None:
        parity = self.parity
        try:
            for i in range(self.count):
                self.repetition = i + 1
                now = self._now()
                key_at, start_at = self._plan(now, parity)
                parity = self._parity_of(start_at)  # repetitions stay in the parity of the first one
                await self._emit("ft8_armed", {
                    "slot_utc": time.strftime("%H:%M:%S", time.gmtime(start_at // FT8_SLOT_S * FT8_SLOT_S)),
                    "key_in_s": round(max(0.0, key_at - now), 1), "start_at": start_at,
                    "parity": parity, "repetition": i + 1, "of": self.count,
                })
                await self._sleep(max(0.0, key_at - self._now()))
                if self._cancelled:
                    return
                self._keying = True
                try:
                    hold_s = await self._key(start_at)
                    self.keyed = True
                finally:
                    self._keying = False
                if self._cancelled:  # cancelled while key() was running: unkey at once (finally)
                    return
                await self._sleep(hold_s)
                self.keyed = False
                # Shielded: a cancel arriving now must not cut the unkey (and
                # its 'unkeyed' event, which closes the TX-log record) short.
                await asyncio.shield(self._unkey())
            await self._emit("ft8_done", {"of": self.count})
        except asyncio.CancelledError:
            pass
        except Exception as e:  # a refusal from the flowgraph (message can't be packed, ...)
            if not self._cancelled:  # after a cancel, key() refusing is expected
                logger.exception("FT8 series failed")
                self._cancelled = True
                await self._emit("ft8_cancelled", {"reason": "error", "message": str(e)})
        finally:
            if self.keyed and not self._skip_unkey:
                self.keyed = False
                await self._unkey()
            self.keyed = False
