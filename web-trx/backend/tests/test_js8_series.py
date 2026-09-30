"""JS8 transmit series (web_trx/js8_series.py) with a simulated clock: frames in consecutive periods, each
keyed on its own, and every way to cancel -- above all, no key() after a cancel, and abort() reaches the
flowgraph."""
import asyncio

import pytest

from web_trx import js8_series
from web_trx.js8_series import Js8Series

T0 = 1_790_600_100.0          # a period boundary for all four speeds
FRAME_S = 3.95 + 0.1          # TURBO: 79 x 50 ms + tail


class Clock:
    def __init__(self, t: float):
        self.t = t
        self.waiters: list[tuple[float, asyncio.Future]] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self.waiters.append((seconds, fut))
        await fut

    def pending(self) -> list[float]:
        return [s for s, fut in self.waiters if not fut.done()]

    async def step(self) -> float:
        for _ in range(100):
            if self.waiters:
                break
            await asyncio.sleep(0)
        assert self.waiters, "series is not sleeping"
        seconds, fut = self.waiters.pop(0)
        self.t += seconds
        fut.set_result(None)
        for _ in range(5):
            await asyncio.sleep(0)
        return seconds


def turbo_plan(now: float, n: int = 3):
    """Like pluto_tx.js8.plan_frames(TURBO): first frame 3 s early, later ones 0.5 s early."""
    slot = (now // 6.0 + 1) * 6.0
    starts = [slot + 0.1 + 6.0 * i for i in range(n)]
    return [(max(now, s - (3.0 if i == 0 else 0.5)), s) for i, s in enumerate(starts)]


class Radio:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.log: list[tuple] = []
        self.events: list[tuple[str, dict]] = []
        self.keyed = False
        self.aborted = 0
        self.block_key: asyncio.Event | None = None

    async def key(self, i: int, start_at: float) -> float:
        if self.block_key is not None:
            await self.block_key.wait()
        self.keyed = True
        self.log.append(("key", i, round(self.clock.t, 3), start_at))
        return (start_at - self.clock.t) + FRAME_S

    async def unkey(self) -> None:
        self.keyed = False
        self.log.append(("unkey", round(self.clock.t, 3)))

    async def abort(self) -> None:
        self.aborted += 1

    async def emit(self, name: str, fields: dict) -> None:
        self.events.append((name, fields))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def keys(self) -> list[int]:
        return [e[1] for e in self.log if e[0] == "key"]


def make(n: int = 3, t: float = T0 + 1.0):
    clock = Clock(t)
    radio = Radio(clock)
    series = Js8Series(plan=lambda now: turbo_plan(now, n), key=radio.key, unkey=radio.unkey, abort=radio.abort,
                       emit=radio.emit, count=n, now=clock.now, sleep=clock.sleep, info={"text": "X"})
    return clock, radio, series


async def run_to_end(clock, series):
    while series.armed:
        await clock.step()


async def test_frames_in_consecutive_periods_each_keyed_once():
    clock, radio, series = make(3)
    series.start()
    await run_to_end(clock, series)
    assert radio.names()[0] == "js8_armed"
    armed = radio.events[0][1]
    assert armed["frames"] == 3 and armed["text"] == "X"
    assert [round(s - armed["starts"][0], 3) for s in armed["starts"]] == [0.0, 6.0, 12.0]
    keys = [e for e in radio.log if e[0] == "key"]
    assert [k[1] for k in keys] == [0, 1, 2]
    assert [round(k[3] - k[2], 3) for k in keys] == [3.0, 0.5, 0.5]           # lead before each frame
    assert [e[0] for e in radio.log] == ["key", "unkey"] * 3                  # dark between the frames
    assert [f["frame_no"] for n, f in radio.events if n == "js8_frame"] == [1, 2, 3]
    assert radio.names()[-1] == "js8_done"
    assert radio.aborted == 0 and not radio.keyed


async def test_cancel_while_waiting_for_the_first_frame():
    clock, radio, series = make(3)
    series.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert await series.cancel("ptt_off")
    assert radio.keys() == [] and radio.aborted == 1
    assert radio.names()[-1] == "js8_cancelled"
    assert radio.events[-1][1]["reason"] == "ptt_off"
    assert clock.pending() == []


async def test_cancel_during_a_frame_unkeys_and_never_keys_again():
    clock, radio, series = make(3)
    series.start()
    await clock.step()                      # wait until the first key
    assert radio.keyed
    assert await series.cancel("estop_like")
    assert not radio.keyed and radio.aborted == 1
    for _ in range(10):
        await asyncio.sleep(0)
    assert radio.keys() == [0]
    assert radio.events[-1] == ("js8_cancelled", {"reason": "estop_like", "sent": 0, "of": 3})


async def test_cancel_between_frames():
    clock, radio, series = make(3)
    series.start()
    await clock.step()                      # key frame 0
    await clock.step()                      # hold frame 0 -> unkey, then waits for frame 1
    assert not radio.keyed and radio.keys() == [0]
    assert await series.cancel("ptt_off")
    assert radio.keys() == [0] and radio.aborted == 1
    assert radio.events[-1][1]["sent"] == 1


async def test_cancel_while_key_is_in_flight_unkeys_right_after():
    clock, radio, series = make(3)
    radio.block_key = asyncio.Event()
    series.start()
    await clock.step()                      # now inside key(), blocked
    for _ in range(3):
        await asyncio.sleep(0)
    cancel = asyncio.create_task(series.cancel("ptt_off"))
    await asyncio.sleep(0)
    radio.block_key.set()                   # key() returns after the cancel
    assert await cancel
    assert radio.log[-1][0] == "unkey" and not radio.keyed
    assert radio.keys() == [0]              # and never again
    for _ in range(10):
        await asyncio.sleep(0)
    assert radio.keys() == [0]


async def test_device_safe_cancel_skips_unkey_and_does_not_wait():
    clock, radio, series = make(2)
    series.start()
    await clock.step()
    assert await series.cancel("estop", device_safe=True)
    await asyncio.sleep(0)
    assert radio.log == [radio.log[0]]       # the key only; the E-STOP already made the device safe
    assert radio.aborted == 1


async def test_refusal_from_the_flowgraph_ends_the_series():
    clock, radio, series = make(2)

    async def refuse(i, start_at):
        raise ValueError("outside the amateur bands")

    series._key = refuse
    series.start()
    await clock.step()
    for _ in range(5):
        await asyncio.sleep(0)
    assert radio.names()[-1] == "js8_cancelled"
    assert radio.events[-1][1]["reason"] == "error"
    assert "amateur" in radio.events[-1][1]["message"]
    assert radio.aborted == 1 and not series.armed


async def test_second_cancel_is_a_no_op():
    _clock, _radio, series = make(2)
    series.start()
    await asyncio.sleep(0)
    assert await series.cancel("a")
    assert not await series.cancel("b")


def test_limits():
    with pytest.raises(ValueError):
        Js8Series(plan=lambda now: [], key=None, unkey=None, abort=None, emit=None, count=21)
    assert js8_series.JS8_MAX_FRAMES == 20


def test_build_message_like_js8call():
    frames, text = js8_series.build_message({"call": "DA2JH", "locator": "JO31"},
                                            {"kind": "snr_query", "to": "DL1ABC", "text": "", "report_db": -10,
                                             "submode": "turbo"})
    assert frames == [("QpYbXBUUeS00", 3)]
    assert text == "DA2JH: DL1ABC SNR?"
    with pytest.raises(ValueError, match="callsign"):
        js8_series.build_message({"call": "", "locator": ""}, {"kind": "cq", "submode": "normal"})
    with pytest.raises(ValueError, match="no message"):
        js8_series.build_message({"call": "DA2JH", "locator": ""}, {"kind": "ack", "to": "", "submode": "normal"})


async def test_actions_are_released_when_the_series_ends():
    """The key/abort closures hold the flowgraph; a finished series must not keep them (reference cycle
    series <-> closure kept a stopped Pluto flowgraph and its TX buffer alive -> EBUSY on reconnect)."""
    clock, _radio, series = make(1)
    series.start()
    await run_to_end(clock, series)
    assert series._key is None and series._abort is None and series._unkey is None
    clock, _radio, series = make(3)
    series.start()
    await clock.step()
    assert await series.cancel("estop", device_safe=True)
    for _ in range(5):
        await asyncio.sleep(0)
    assert series._key is None and series._abort is None

