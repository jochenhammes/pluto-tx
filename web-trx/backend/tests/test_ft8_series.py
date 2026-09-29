"""FT8 transmit series (web_trx/ft8_series.py) with a simulated clock: slot
timing, repetitions in the same parity, and every way to cancel -- above all,
no key() after a cancel."""
import asyncio
from pathlib import Path

import pytest

from web_trx import ft8_series
from web_trx.ft8_series import Ft8Series
from web_trx.sim_backend import sim_plan_transmission

FRAME_S = 12.74
T0 = 1_790_600_100.0  # an "even" slot boundary (:00/:30), the next one is "odd"


class Clock:
    """now() returns simulated time; every sleep() parks until the test calls
    step(), which advances the time by the requested amount."""

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
        """Sleeps still waiting (a cancelled series leaves none)."""
        return [seconds for seconds, fut in self.waiters if not fut.done()]

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


class Radio:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.log: list[tuple] = []
        self.events: list[tuple[str, dict]] = []
        self.keyed = False

    async def key(self, start_at: float) -> float:
        self.keyed = True
        self.log.append(("key", self.clock.t, start_at))
        return (start_at - self.clock.t) + FRAME_S

    async def unkey(self) -> None:
        self.keyed = False
        self.log.append(("unkey", self.clock.t))

    async def emit(self, name: str, fields: dict) -> None:
        self.events.append((name, fields))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]


def make(t: float = T0 + 5.0, **kw):
    clock = Clock(t)
    radio = Radio(clock)
    series = Ft8Series(plan=sim_plan_transmission, key=radio.key, unkey=radio.unkey, emit=radio.emit,
                       now=clock.now, sleep=clock.sleep, **kw)
    return clock, radio, series


async def run_to_end(clock: Clock, series: Ft8Series) -> None:
    while series.armed:
        await clock.step()


async def test_keys_three_seconds_before_the_slot_and_starts_half_a_second_in():
    clock, radio, series = make(T0 + 5.0)
    series.start()
    await clock.step()  # armed -> key time
    assert radio.log == [("key", T0 + 15.0 + 0.5 - 3.0, T0 + 15.5)]
    armed = radio.events[0][1]
    assert radio.events[0][0] == "ft8_armed"
    assert armed["start_at"] == T0 + 15.5 and armed["repetition"] == 1 and armed["of"] == 1
    assert armed["parity"] == "odd"
    await run_to_end(clock, series)
    assert radio.log[-1] == ("unkey", T0 + 15.5 + FRAME_S)
    assert radio.names() == ["ft8_armed", "ft8_done"]


async def test_late_press_in_a_matching_slot_still_uses_it():
    clock, radio, series = make(T0 + 0.8)  # 0.8 s into the slot: within late_max
    series.start()
    await clock.step()
    assert radio.log[0][2] == T0 + 0.8


@pytest.mark.parametrize("parity,start", [("odd", T0 + 15.5), ("even", T0 + 30.5)])
async def test_fixed_parity(parity, start):
    clock, radio, series = make(T0 + 5.0, parity=parity)
    series.start()
    await clock.step()
    assert radio.log[0][2] == start


async def test_repetitions_every_30_s_in_the_parity_of_the_first():
    clock, radio, series = make(T0 + 5.0, count=3)
    series.start()
    await run_to_end(clock, series)
    starts = [entry[2] for entry in radio.log if entry[0] == "key"]
    assert starts == [T0 + 15.5, T0 + 45.5, T0 + 75.5]
    armed = [f for n, f in radio.events if n == "ft8_armed"]
    assert [(a["repetition"], a["of"]) for a in armed] == [(1, 3), (2, 3), (3, 3)]
    assert radio.names()[-1] == "ft8_done"
    assert not radio.keyed


def test_repeat_count_is_capped():
    with pytest.raises(ValueError):
        make(count=ft8_series.FT8_MAX_REPEATS + 1)
    with pytest.raises(ValueError):
        make(count=0)


@pytest.mark.parametrize("reason", ["ptt_off", "disconnect", "mode_change", "no_operator"])
async def test_cancel_while_armed_never_keys(reason):
    clock, radio, series = make(count=5)
    series.start()
    await asyncio.sleep(0)
    assert series.armed
    assert await series.cancel(reason)
    assert not series.armed
    assert radio.log == []
    assert radio.events[-1] == ("ft8_cancelled", {"reason": reason})
    assert clock.pending() == []  # nothing left sleeping that could key later


async def test_cancel_while_keyed_unkeys_and_stops_the_repetitions():
    clock, radio, series = make(count=5)
    series.start()
    await clock.step()  # keyed
    assert radio.keyed
    await series.cancel("ptt_off")
    assert not radio.keyed
    assert [e[0] for e in radio.log] == ["key", "unkey"]
    assert radio.names() == ["ft8_armed", "ft8_cancelled"]
    await asyncio.sleep(0)
    assert clock.pending() == []


async def test_cancel_between_repetitions():
    clock, radio, series = make(count=3)
    series.start()
    await clock.step()  # key
    await clock.step()  # hold -> unkey, armed for #2
    assert radio.names()[-1] == "ft8_armed"
    await series.cancel("ptt_off")
    assert [e[0] for e in radio.log] == ["key", "unkey"]


async def test_estop_cancel_skips_the_unkey_and_does_not_wait():
    """E-STOP forced the device safe already: the series must not touch it again."""
    clock, radio, series = make()
    series.start()
    await clock.step()  # keyed
    await series.cancel("estop", device_safe=True)
    await asyncio.sleep(0)
    assert [e[0] for e in radio.log] == ["key"]  # no unkey from the series
    assert radio.events[-1] == ("ft8_cancelled", {"reason": "estop"})


async def test_cancel_during_key_unkeys_right_after_and_keys_no_more():
    """key() runs in a worker thread in the real backend and can't be
    interrupted: the series waits for it and unkeys at once."""
    clock = Clock(T0 + 5.0)
    radio = Radio(clock)
    release = asyncio.Event()

    async def slow_key(start_at):
        await release.wait()
        return await radio.key(start_at)

    series = Ft8Series(plan=sim_plan_transmission, key=slow_key, unkey=radio.unkey, emit=radio.emit,
                       now=clock.now, sleep=clock.sleep, count=5)
    series.start()
    await clock.step()  # now inside slow_key
    cancel = asyncio.create_task(series.cancel("ptt_off"))
    await asyncio.sleep(0)
    assert not cancel.done()  # waits for key() to return
    release.set()
    await cancel
    assert [e[0] for e in radio.log] == ["key", "unkey"]
    assert not radio.keyed and clock.pending() == []


async def test_a_refusing_key_ends_the_series_with_an_error():
    clock = Clock(T0 + 5.0)
    radio = Radio(clock)

    async def refuse(start_at):
        raise ValueError("message can't be packed as FT8")

    series = Ft8Series(plan=sim_plan_transmission, key=refuse, unkey=radio.unkey, emit=radio.emit,
                       now=clock.now, sleep=clock.sleep, count=3)
    series.start()
    await clock.step()
    assert radio.events[-1] == ("ft8_cancelled", {"reason": "error", "message": "message can't be packed as FT8"})
    assert not series.armed
    assert await series.cancel("ptt_off") is False  # nothing left to cancel


async def test_cancel_twice_is_harmless():
    _clock, radio, series = make()
    series.start()
    await asyncio.sleep(0)
    assert await series.cancel("ptt_off") is True
    assert await series.cancel("estop") is False
    assert radio.names().count("ft8_cancelled") == 1


PLUTO_FT8 = Path(__file__).resolve().parents[3] / "pluto_tx" / "ft8.py"


@pytest.mark.skipif(not PLUTO_FT8.exists(), reason="not inside a pluto-tx checkout")
def test_sim_planner_matches_pluto_tx():
    """SimBackend's planner is a test double: it must agree with pluto-tx'
    plan_transmission() (with pluto-tx' own timing constants)."""
    from web_trx import pluto_path

    pluto_path.ensure_importable()
    from pluto_tx import config, ft8

    for now in [T0 + x / 4 for x in range(4 * 65)]:
        for parity in ("any", "even", "odd"):
            expected = ft8.plan_transmission(now, parity, config.FT8_KEY_EARLY_S, config.FT8_START_IN_SLOT_S,
                                             config.FT8_LATE_START_MAX_S)
            assert sim_plan_transmission(now, parity) == pytest.approx(expected), (now, parity)
            assert ft8_series.slot_parity(now) == ft8.slot_parity(now)
