"""GNU Radio sink block that decodes BPSK31/Varicode text straight out of a
differentially-decoded bit stream (1 bit per byte, unpacked, values 0/1 --
the output of digital.diff_decoder_bb, itself fed by a symbol-sliced,
Costas-loop-locked BPSK31 demod chain -- see pluto_advanced_rx/flowgraph.py's
always-on PSK31 branch) and invokes a plain Python callback per decoded
character. Same "gr.sync_block with out_sig=None, Python callback/poll
surface" idiom already proven by fft_probe.py's FftProbe and
filebroadcast_deframer.py's FileBroadcastDeframer -- event-driven (a
callback per character) rather than poll-driven, matching
FileBroadcastDeframer's own per-frame callback shape.

Varicode table and the "00"-boundary self-synchronization property are a
SEPARATE, independent copy of pluto_tx/psk31.py's encoder-side table --
matches this project's established convention for protocol/PHY logic that
must stay byte-for-byte in sync across the two independent app packages
(see pluto_advanced_rx/filebroadcast_deframer.py's own docstring, and e.g.
rade_ctypes.py's per-package copies). If you change the table, change BOTH
copies.

No preamble-skip logic exists here on purpose: pluto_tx/psk31.py's
transmitted preamble (a run of alternating phase-reversal states, emitted
BEFORE the real Varicode-encoded text, to give the receiver's Costas loop/
symbol timing recovery time to settle -- see its own docstring for the
real-hardware/software-round-trip finding that motivated this) needs no
special handling on this side: an alternating 1/0/1/0... bit pattern will
harmlessly produce its own "00" boundaries and get scanned like any other
bit run, matched against the reverse table (silently failing to match
anything meaningful, exactly like any other noise/resync run), and
discarded -- by the time the real message starts, this block has already
naturally resynced to real character boundaries with no explicit
preamble-length knowledge needed on the RX side at all."""
import threading

from gnuradio import gr
import numpy as np

# MUST match pluto_tx/psk31.py's _VARICODE_TABLE exactly -- see module
# docstring above. Index = ASCII/Latin-1 code point, value = the Varicode
# bit pattern as a '0'/'1' string. Sourced from fldigi's own
# pskvaricode.cxx via ckoval7/pydigi's psk_varicode.py (see pluto_tx/
# psk31.py's own docstring for the full provenance note).
_VARICODE_TABLE = [
    "1010101011", "1011011011", "1011101101", "1101110111", "1011101011",
    "1101011111", "1011101111", "1011111101", "1011111111", "11101111",
    "11101", "1101101111", "1011011101", "11111", "1101110101",
    "1110101011", "1011110111", "1011110101", "1110101101", "1110101111",
    "1101011011", "1101101011", "1101101101", "1101010111", "1101111011",
    "1101111101", "1110110111", "1101010101", "1101011101", "1110111011",
    "1011111011", "1101111111", "1", "111111111", "101011111",
    "111110101", "111011011", "1011010101", "1010111011", "101111111",
    "11111011", "11110111", "101101111", "111011111", "1110101",
    "110101", "1010111", "110101111", "10110111", "10111101",
    "11101101", "11111111", "101110111", "101011011", "101101011",
    "110101101", "110101011", "110110111", "11110101", "110111101",
    "111101101", "1010101", "111010111", "1010101111", "1010111101",
    "1111101", "11101011", "10101101", "10110101", "1110111",
    "11011011", "11111101", "101010101", "1111111", "111111101",
    "101111101", "11010111", "10111011", "11011101", "10101011",
    "11010101", "111011101", "10101111", "1101111", "1101101",
    "101010111", "110110101", "101011101", "101110101", "101111011",
    "1010101101", "111110111", "111101111", "111111011", "1010111111",
    "101101101", "1011011111", "1011", "1011111", "101111",
    "101101", "11", "111101", "1011011", "101011",
    "1101", "111101011", "10111111", "11011", "111011",
    "1111", "111", "111111", "110111111", "10101",
    "10111", "101", "110111", "1111011", "1101011",
    "11011111", "1011101", "111010101", "1010110111", "110111011",
    "1010110101", "1011010111", "1110110101", "1110111101", "1110111111",
    "1111010101", "1111010111", "1111011011", "1111011101", "1111011111",
    "1111101011", "1111101101", "1111101111", "1111110101", "1111110111",
    "1111111011", "1111111101", "1111111111", "10101010101", "10101010111",
    "10101011011", "10101011101", "10101011111", "10101101011", "10101101101",
    "10101101111", "10101110101", "10101110111", "10101111011", "10101111101",
    "10101111111", "10110101011", "10110101101", "10110101111", "10110110101",
    "10110110111", "10110111011", "10110111101", "10110111111", "10111010101",
    "10111010111", "10111011011", "10111011101", "10111011111", "10111101011",
    "10111101101", "10111101111", "10111110101", "10111110111", "10111111011",
    "10111111101", "10111111111", "11010101011", "11010101101", "11010101111",
    "11010110101", "11010110111", "11010111011", "11010111101", "11010111111",
    "11011010101", "11011010111", "11011011011", "11011011101", "11011011111",
    "11011101011", "11011101101", "11011101111", "11011110101", "11011110111",
    "11011111011", "11011111101", "11011111111", "11101010101", "11101010111",
    "11101011011", "11101011101", "11101011111", "11101101011", "11101101101",
    "11101101111", "11101110101", "11101110111", "11101111011", "11101111101",
    "11101111111", "11110101011", "11110101101", "11110101111", "11110110101",
    "11110110111", "11110111011", "11110111101", "11110111111", "11111010101",
    "11111010111", "11111011011", "11111011101", "11111011111", "11111101011",
    "11111101101", "11111101111", "11111110101", "11111110111", "11111111011",
    "11111111101", "11111111111", "101010101011", "101010101101", "101010101111",
    "101010110101", "101010110111", "101010111011", "101010111101", "101010111111",
    "101011010101", "101011010111", "101011011011", "101011011101", "101011011111",
    "101011101011", "101011101101", "101011101111", "101011110101", "101011110111",
    "101011111011", "101011111101", "101011111111", "101101010101", "101101010111",
    "101101011011",
]
_REVERSE_TABLE = {code: i for i, code in enumerate(_VARICODE_TABLE)}

# Guards against a runaway accumulator on a real bit stream that never
# produces a "00" boundary (e.g. pure noise before lock) -- the longest
# real code is 12 bits, so anything meaningfully longer than that is
# already known-garbage; reset early rather than accumulating forever.
_MAX_CODE_LEN = 16


class PSK31VaricodeDeframer(gr.sync_block):
    def __init__(self, on_char):
        gr.sync_block.__init__(self, name="PSK31VaricodeDeframer", in_sig=[np.uint8], out_sig=None)
        self._on_char = on_char
        self._lock = threading.Lock()
        self._acc = []       # confirmed content bits of the code currently being accumulated
        self._pending = None  # a single held-back '0' bit (might be content, or the first '0' of "00" --
        # a valid code can legitimately contain a lone '0' internally, just never two in a row, so a
        # single 0 can't be classified as content-vs-separator until the NEXT bit disambiguates it;
        # this one-bit delay buffer is the streaming equivalent of the pure-Python prototype's
        # look-ahead check (`bits[i]==0 and bits[i+1]==0`), verified byte-for-byte equivalent against
        # it during this mode's own Phase 0 software round-trip test before adopting this streaming form)
        self._chars_decoded = 0

    def work(self, input_items, output_items):
        bits = input_items[0]
        with self._lock:
            for bit in bits:
                bit = int(bit) & 1
                if self._pending is None:
                    if bit == 0:
                        self._pending = 0
                    else:
                        self._acc.append(1)
                elif bit == 0:
                    # "00" boundary -- the held-back 0 was the separator's first bit, never content.
                    code = "".join(str(b) for b in self._acc)
                    char_code = _REVERSE_TABLE.get(code)
                    if char_code is not None:
                        self._chars_decoded += 1
                        self._on_char(chr(char_code))
                    self._acc = []
                    self._pending = None
                else:
                    # the held-back 0 turned out to be genuine content (followed by a 1, not another 0).
                    self._acc.append(0)
                    self._acc.append(1)
                    self._pending = None
                if len(self._acc) > _MAX_CODE_LEN:
                    self._acc = []
                    self._pending = None
        return len(bits)

    @property
    def chars_decoded(self):
        with self._lock:
            return self._chars_decoded
