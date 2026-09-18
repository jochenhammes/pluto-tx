"""LoRa airtime + duty-cycle bookkeeping -- pure Python, no GNU Radio/Qt, so
the TX flowgraph, the GUI, the CLI and the tests can all share it.

The airtime formula is the standard Semtech one (SX126x datasheet, "LoRa
time on air"); it was verified against gr-lora_sdr's real output by counting
the samples `modulate` emits for a 221-byte frame at SF11/BW250k/CR4:5
(1845.2 ms, identical to the formula to the decimal). Do NOT measure airtime
via wall-clock through blocks.throttle -- that block is only an average-rate
limiter and gives wrong numbers for a single burst.
"""
import collections
import math


def cr_index(coding_rate) -> int:
    """LoRa coding-rate index 1..4 (4/5 .. 4/8) from a LoraPreset.coding_rate
    string: "4/5" -> 1, "4/8" -> 4, a bare "8" (MeshCore's notation) -> 4."""
    text = str(coding_rate).strip()
    denom = int(text.split("/")[-1])
    if not 5 <= denom <= 8:
        raise ValueError(f"invalid LoRa coding rate {coding_rate!r} (denominator must be 5..8)")
    return denom - 4


def symbol_time_s(sf: int, bw_hz: float) -> float:
    return (2 ** sf) / float(bw_hz)


def lora_airtime_s(payload_len: int, sf: int, bw_hz: float, cr: int, preamble_len: int,
                   has_crc: bool = True, implicit_header: bool = False) -> float:
    """Time on air (seconds) of one LoRa frame carrying `payload_len` bytes.
    `cr` is the coding-rate INDEX 1..4 (see cr_index()). Low-data-rate
    optimisation follows the usual automatic rule (symbol time > 16 ms),
    which is also what the encoders here use (ldro=2 = auto)."""
    t_sym = symbol_time_s(sf, bw_hz)
    de = 1 if t_sym > 0.016 else 0
    ih = 1 if implicit_header else 0
    crc = 1 if has_crc else 0
    numerator = 8 * payload_len - 4 * sf + 28 + 16 * crc - 20 * ih
    n_payload = 8 + max(math.ceil(numerator / (4 * (sf - 2 * de))) * (cr + 4), 0)
    return (preamble_len + 4.25 + n_payload) * t_sym


class DutyCycleLimiter:
    """Rolling-window airtime budget, e.g. 10 % of one hour = 36 s -- what the
    868 MHz SRD sub-band (869.4-869.65 MHz) and Meshtastic's own EU regions
    require. `limit` is a fraction (0.10); None or 0 disables the check."""

    def __init__(self, limit, window_s: float = 3600.0):
        self.limit = limit
        self.window_s = float(window_s)
        self._log = collections.deque()  # (t_start, airtime_s)

    def _prune(self, now: float):
        while self._log and self._log[0][0] + self.window_s <= now:
            self._log.popleft()

    def used_s(self, now: float) -> float:
        self._prune(now)
        return sum(a for _, a in self._log)

    def budget_s(self) -> float:
        return (self.limit or 0.0) * self.window_s

    def check(self, airtime_s: float, now: float):
        """(allowed, wait_s): whether a transmission of `airtime_s` starting
        at `now` fits the budget, and if not how long until it would."""
        if not self.limit:
            return True, 0.0
        self._prune(now)
        budget = self.budget_s()
        if airtime_s > budget:
            return False, float("inf")  # can never fit
        used = sum(a for _, a in self._log)
        if used + airtime_s <= budget:
            return True, 0.0
        # wait until enough old entries have aged out of the window
        excess = used + airtime_s - budget
        freed = 0.0
        for t0, a in self._log:
            freed += a
            if freed >= excess:
                return False, max(0.0, t0 + self.window_s - now)
        return False, self.window_s

    def record(self, airtime_s: float, now: float):
        self._prune(now)
        self._log.append((now, float(airtime_s)))
