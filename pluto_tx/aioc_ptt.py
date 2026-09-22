"""Serial DTR/RTS PTT control for an AIOC (All-In-One-Cable) adapter, shared by
AiocDevice (pluto_tx/devices/aioc.py) and the standalone tx-stdin.py script -- one
implementation, so the PTT sequencing can't drift between the two callers.

HARDWARE DETAIL (confirmed by testing on real AIOC + Quansheng UV-K5 hardware): the
AIOC's legacy serial PTT scheme asserts PTT only when DTR=1 AND RTS=0 AT THE SAME
TIME -- DTR alone is not enough. pyserial opens a port with RTS defaulting to True,
so RTS must be explicitly cleared here or the radio never keys up.

The serial connection must stay open for as long as PTT might be needed: closing the
port makes the USB CDC-ACM stack drop DTR/RTS immediately, un-keying PTT right away.
Callers open one AiocPtt per session (not one per transmission) and keep it open --
see AiocDevice.prepare_for_start()/tx-stdin.py's main() for the two call sites.
"""
import time

import serial


class AiocPtt:
    """Wraps one open serial.Serial to the AIOC's PTT interface.

    key()/unkey() each block briefly (lead-in/tail-out settle time) -- the same
    "may block briefly" contract TxDevice.pre_key()/post_unkey() already document
    (see devices/base.py; PlutoDevice's LO-relock sleep is the existing precedent).
    force_release() is the immediate, non-blocking, never-raising counterpart for a
    safety-critical path (E-STOP/signal handler) that cannot wait out a tail."""

    def __init__(self, port: str, lead_in_s: float = 0.3, tail_out_s: float = 0.2):
        self.port = port
        self.lead_in_s = lead_in_s
        self.tail_out_s = tail_out_s
        self._ser = serial.Serial(port)
        self._ser.rts = False  # DTR=1 AND RTS=0 simultaneously -- see module docstring
        self._ser.dtr = False

    def key(self, on_keyed=None):
        """Asserts PTT, then holds it for lead_in_s before returning so the
        transmitter can settle before modulation begins. `on_keyed`, if given, runs
        right after DTR is set but before the settle sleep -- lets a caller log/act
        at the exact moment PTT actually went active, not after the whole delay."""
        self._ser.dtr = True
        if on_keyed is not None:
            on_keyed()
        time.sleep(self.lead_in_s)

    def unkey(self):
        """Holds PTT for tail_out_s (so the last bit of already-buffered audio isn't
        clipped), then releases it. Not the safety-net path -- see force_release()."""
        time.sleep(self.tail_out_s)
        self.force_release()

    def force_release(self):
        """Immediate, non-blocking PTT release. Idempotent and never raises on
        purpose: called from both the normal shutdown path (via unkey()) and a
        signal/E-STOP handler, so it must be safe to run at any time, including
        twice in a row."""
        try:
            self._ser.dtr = False
        except Exception:
            pass

    def close(self):
        try:
            self._ser.close()
        except Exception:
            pass
