"""LoRa CSS PHY TX encoder, wrapping gr-lora_sdr (install-lora.sh, optional
GPL-3.0 dependency -- see /home/hammesj/.claude/plans/swirling-waddling-noodle.md
for the full LoRa Mesh plan this is Phase 1 of). Gated by LORA_AVAILABLE,
duplicated per-file the same way M17_AVAILABLE/RADE_AVAILABLE already are
elsewhere in this codebase (pluto_tx/flowgraph.py, pluto_advanced_rx/
flowgraph.py, pluto_cli/tx.py, pluto_cli/rx.py) -- no shared central import.

Message-driven, like m17_coder: LoraTxEncoder exposes a hierarchical
message input port ("msg") a caller posts a payload string to directly
(`lora_tx.post(pmt.intern("msg"), pmt.intern(payload_text))`), the exact
same idiom this codebase already uses for M17's SOT/EOT control messages
(see flowgraph.py's `self.m17_coder.post(...)` calls) -- not a separate
message_strobe/source block a caller has to wire up externally.

Three real, non-obvious bugs found and fixed getting a working TX+RX
software loopback (see the LoRa mesh plan's Phase 1 entry for the full
investigation writeup -- reproduced this session via gr-lora_sdr's own
compiled upstream example (tx_rx_functionality_check.grc) as a known-good
baseline, then bisected against a hand-written harness):

1. `lora_sdr.modulate`'s output MUST have a large min_output_buffer
   (10_000_000, matching gr-lora_sdr's own reference example) -- without
   it, GNU Radio's scheduler starves frame_sync downstream with a hard
   "requesting more input data than we can provide" error (a related,
   but NOT identical, failure mode to M17's own documented scheduler-
   starvation bug -- there it was m17_coder's ~520x expansion factor vs.
   blocks.selector; here it's LoRa's own internal windowed-read pattern
   needing more buffer headroom than GNU Radio's default).
2. A `blocks.delay` of `int(2**sf*samp_rate/bw*10.1)` samples is needed
   immediately after `modulate`, before the signal reaches the channel/
   RX chain -- omitting it produced a hard, unconditional SEGFAULT (not
   a Python exception) in frame_sync's C++ general_work() on the very
   first burst, reproduced at every SF from 7 through 12 without this
   delay, at every SF passing cleanly with it. gr-lora_sdr's own
   reference example includes this exact delay; the reason isn't
   documented upstream, but frame_sync's internal correlator/preamble
   detector evidently needs this much leading buffer history before
   its first real read.
3. `lora_sdr.frame_sync`'s `center_freq` argument (see LoraRxDecoder in
   pluto_advanced_rx/lora_rx.py) must NOT be 0, even in a pure baseband
   loopback with no real RF involved at all -- bisected this session:
   EVERY SF from 7 through 12 segfaults unconditionally with
   center_freq=0, and ALL pass cleanly with a real RF-like value (e.g.
   868.1e6, matching gr-lora_sdr's own reference example). The exact
   internal reason wasn't traced further (not worth the time once the
   real, reproducible fix was found) -- just pass this project's own
   real per-preset frequency (config.LoraPreset.frequency_hz) rather
   than 0, which we'd want to do anyway for a real device connection.

A fourth finding worth recording because the OPPOSITE conclusion was
initially (wrongly) reached mid-investigation: SF11/SF12 do NOT show
any scheduler-starvation risk once the above three fixes are in place
-- all of SF7 through SF12 passed a clean, bit-exact, 20-random-payload
software loopback test this session, no different treatment needed at
high SF. The LoRa mesh plan's own "SF11/SF12 scheduler-risk" concern
(modeled on M17's real, different bug) turned out not to apply here
once these three real bugs were fixed -- worth re-reading before
assuming it's still an open risk.

A fifth real bug, found in Phase 3 (combining this with the Meshtastic
protocol layer): `gr.hier_block2.post()` does NOT reliably deliver a
message to a `message_port_register_hier_in()`-forwarded port. Directly
mirroring M17's own `m17_coder.post(pmt.intern("transmission_control"),
...)` idiom by calling `.post()` on a LoraTxEncoder INSTANCE silently
produced nothing (confirmed via a vector_sink tap: exactly the
`blocks.delay`'s own startup zero-padding came out, then the flowgraph
stalled forever -- `modulate` never actually ran). The IDENTICAL
topology worked perfectly when the message was instead delivered via a
real `tb.msg_connect(external_block, "port", lora_tx, "msg")` wired at
flowgraph-construction time -- construction-time hierarchical msg_connect
wiring works, direct runtime `.post()` on the hier_block2 object does
not. Worked around by dropping the hierarchical message port entirely
and exposing a plain `send_payload(text)` Python method instead, which
posts straight to the real internal `whitening` leaf block (bypassing
hier_block2 forwarding altogether) -- this is why M17's own `.post()`
idiom works fine for it (`m17_coder` is a real leaf block, never wrapped
in a hier_block2), and why LoraTxEncoder can't just copy that pattern
verbatim despite looking superficially identical.

A sixth real bug, found (and this time FIXED, unlike the earlier
session's writeup) while testing this against real Meshtastic-sized
encrypted payloads: `send_payload(text)`'s PMT-symbol path is NOT
byte-safe for arbitrary binary data. `pmt.intern(text)`'s Python
binding UTF-8-encodes `text` internally (confirmed: a raw-bytes payload
round-tripped via `.decode("latin-1")` measurably inflates -- 221 bytes
became 314 after `bytes.decode("latin-1").encode("utf-8")`, since any
byte >=128 becomes a 2-byte UTF-8 sequence), and the RX side's obvious
mirror-image read (`pmt.symbol_to_string(msg)`) ALSO force-decodes as
UTF-8 and raises `UnicodeDecodeError` on genuinely arbitrary bytes.
Confirmed via gr-lora_sdr's own C++ source (`lib/whitening_impl.cc`)
that this is a PYTHON-BINDING-LAYER problem, not a gr-lora_sdr one:
`whitening_impl`'s message handler just does
`payload_str.push_back(pmt::symbol_to_string(message))`, a plain C++
`std::string` (raw bytes, no Unicode validation at all) -- the UTF-8
enforcement is entirely inside `pmt.intern()`/`pmt.symbol_to_string()`'s
pybind11 `s: str` signatures on the PYTHON side.

**Real fix, found and verified this session**: `whitening_impl`'s
constructor ALSO exposes an optional raw uint8_t STREAM input (`gr::
io_signature::make(0, 1, sizeof(uint8_t))`, confirmed in source) --
completely separate from the message path, driven by a `packet_len`
stream tag marking each frame's byte length (the same mechanism GRC's
"file_source" mode uses). Feeding a fresh `blocks.vector_source_b` (one
frame, `repeat=False`, carrying a `packet_len` tag at offset 0) into
`whitening`'s stream input bypasses the PMT-symbol/UTF-8 path entirely
-- verified end-to-end with all 256 byte values present in a single
221-byte payload, bit-exact round trip. On the RX side, `crc_verif`'s
"msg" output is still a PMT symbol, but `pmt.serialize_str(msg)` (which
returns real Python `bytes`, not a UTF-8-decoded `str`) exposes PMT's
own binary wire format directly: a fixed, empirically-confirmed 3-byte
header (`0x02` type tag + a 2-byte BIG-ENDIAN length) immediately
followed by the raw payload bytes -- confirmed stable across payload
lengths 0, 2, 5, and 300 this session. `decode_pmt_symbol_bytes()`
below parses exactly that. `send_payload(text)` (PMT-symbol path) is
KEPT for plain short text (still fine, unaffected below LoRa's real
payload ceiling) -- `send_payload_bytes(raw)` is the one to use for
anything binary/high-entropy like a real Meshtastic/MeshCore frame.

A tenth real bug, found doing the first real Meshtastic-interop bench
test (Pluto TX -> a real Heltec V3 running stock Meshtastic firmware):
self-consistency (this project's own TX decoded by this project's own RX
via gr-lora_sdr on both ends, even via an independent RTL-SDR capture)
does NOT prove interop with an independent LoRa implementation. Two real
over-the-air attempts decoded perfectly on this project's own receiver
but never reached the real Heltec, and the reverse direction (real
Heltec -> our RX) also failed to decode.

**Root cause, MEASURED from a real recorded Heltec burst (2026-09-19)
and confirmed by gr-lora_sdr's own diagnostics -- an earlier version of
this docstring blamed the sync-word byte (0x12 vs 0x2B) and, before
that, an assumed CFO/overload problem; both were WRONG:**
manually dechirping the real burst (numpy, symbol by symbol) showed a
standard frame -- 16 preamble upchirps, 2 sync symbols, 2.25 SFD
downchirps, then payload -- with a tiny CFO (about -0.5 kHz, < 1 ppm) and
a strong, unclipped signal. The two sync symbols have the VALUES
(16, 2008) (frame_sync itself reported "netid1 is 16, netid2 is 2008"
when its check was disabled via sync_word=0). gr-lora_sdr derives its
expected sync symbols from a single byte as (high nibble << 3, low
nibble << 3), i.e. values 0..120, so 2008 can never be produced or
matched by ANY sync_word byte: frame_sync rejected every real Meshtastic
frame at its network-id check (`mod(netid2 - offset, N) !=
sync_words[1]`, frame_sync_impl.cc), and our own TX emitted (16, 88) for
0x2B, which a real SX126x correlator does not accept either. Fix:
`modulate` and `frame_sync` both accept a 2-element list and then use
the elements as RAW symbol values (modulate_impl.cc / frame_sync_impl.cc
only expand a 1-element list) -- so the defaults here are now
config.MESHTASTIC_SYNC_SYMBOLS = (16, 2008) and
config.MESHTASTIC_PREAMBLE_LEN = 16 (also measured: 16 upchirps, not 8).
Verified by replaying the real capture with these values: the frame
decodes and my meshtastic_codec reads it correctly (from = the Heltec's
node number, text "Hallo Welt!"). TX-direction interop (Pluto -> Heltec)
still has to be verified on real hardware with these values."""
try:
    from gnuradio import lora_sdr as _lora_sdr
    LORA_AVAILABLE = True
except ImportError:
    _lora_sdr = None
    LORA_AVAILABLE = False

import queue

import numpy as np
import pmt
from gnuradio import gr, blocks
from gnuradio.filter import firdes
from gnuradio.fft import window

from . import config


def lora_resampler_taps(bw, gain, design_rate, min_io_rate=None):
    """Explicit low-pass taps for resampling a LoRa signal between the
    encoder/decoder rate (4 x bw) and a device rate -- used by the TX branch
    (interpolating up to the device) and the RX branch (down/up to 4 x bw).
    `design_rate` is the rate the polyphase filter actually runs at
    (input_rate * interpolation); `gain` = the interpolation factor.
    Never taps=[] (see backlog/ft8 -- auto-designed taps corrupt phase-
    continuous signals). firdes' cutoff is the CENTRE of the transition band:
    flat up to 0.56 x bw (the chirp occupies +-0.5 x bw, plus CFO margin), fully
    down by 3.2 x bw -- the first image/alias of a 4 x bw stream sits at
    3.5 x bw. `min_io_rate` (the smaller of the resampler's input/output
    rate, default 4 x bw) caps the stop edge for wide presets whose 4 x bw
    stream is faster than the device rate: the first image then sits at
    min_io_rate - 0.5 x bw."""
    min_io_rate = 4 * bw if min_io_rate is None else min_io_rate
    pass_edge = 0.56 * bw
    stop_edge = min(3.2 * bw, min_io_rate - 0.5 * bw)
    return firdes.low_pass(
        gain, design_rate, (pass_edge + stop_edge) / 2.0, stop_edge - pass_edge, window.WIN_HAMMING,
    )


def _sync_symbols(sync_word):
    """sync_word may be a single byte (gr-lora_sdr expands it to the two
    symbol values (hi_nibble << 3, lo_nibble << 3)) or a 2-element
    sequence of RAW symbol values, passed through unchanged (used for
    Meshtastic's real on-air values, see config.MESHTASTIC_SYNC_SYMBOLS)."""
    if isinstance(sync_word, (tuple, list)):
        assert len(sync_word) == 2, "sync_word sequence must be exactly 2 raw symbol values"
        return [int(v) for v in sync_word]
    return [int(sync_word)]


class _ByteQueueSource(gr.sync_block):
    """A permanently-connected uint8 stream source fed from a thread-safe
    Python queue via push(raw_bytes) -- NOT a fresh-vector-source-per-
    transmission design (the pattern this project uses elsewhere, e.g.
    Digitext/RTTY), because that needs `lock()/disconnect()/connect()/
    unlock()` at runtime, which deadlocks here specifically when a
    LoraRxDecoder (containing frame_sync) is connected downstream in the
    same top_block -- confirmed this session: the identical dynamic-
    reconnect code worked fine in a TX-only flowgraph, and hung
    indefinitely (inside `unlock()`/a subsequent `lock()` call) as soon
    as an RX chain was present too. A permanent block that never needs
    lock()/connect() again after construction sidesteps the deadlock
    entirely, at the cost of needing its own tiny internal framing state
    machine instead of reusing blocks.vector_source_b."""

    def __init__(self):
        gr.sync_block.__init__(
            self, name="byte_queue_source",
            in_sig=None, out_sig=[np.uint8],
        )
        self._queue = queue.Queue()
        self._current = b""
        self._pos = 0
        self._need_tag = False

    def push(self, raw: bytes):
        """Thread-safe -- call from any thread, no lock()/connect() ever
        needed (unlike send_payload_bytes()'s earlier, deadlocking
        design this replaced)."""
        self._queue.put(bytes(raw))

    def work(self, input_items, output_items):
        out = output_items[0]
        produced = 0
        while produced < len(out):
            if self._pos >= len(self._current):
                try:
                    self._current = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._pos = 0
                self._need_tag = True
            if self._need_tag:
                self.add_item_tag(
                    0, self.nitems_written(0) + produced,
                    pmt.string_to_symbol("packet_len"),
                    pmt.from_long(len(self._current)),
                )
                self._need_tag = False
            n = min(len(out) - produced, len(self._current) - self._pos)
            out[produced:produced + n] = np.frombuffer(
                self._current[self._pos:self._pos + n], dtype=np.uint8
            )
            produced += n
            self._pos += n
        return produced


def decode_pmt_symbol_bytes(msg) -> bytes:
    """Extract the raw bytes of a PMT symbol WITHOUT UTF-8 decoding --
    the byte-safe counterpart of LoraTxEncoder.send_payload_bytes(),
    for reading LoraRxDecoder's "msg" output. See the module docstring's
    6th real bug for why `pmt.symbol_to_string(msg)` is NOT safe to use
    here (raises UnicodeDecodeError, or silently corrupts, on genuinely
    arbitrary/high-entropy bytes like a real encrypted mesh packet).
    Uses `pmt.serialize_str()` (returns real Python bytes) and PMT's own
    binary wire format for a symbol -- empirically confirmed stable this
    session across payload lengths 0/2/5/221/300: 1 byte type tag
    (0x02), 2 bytes big-endian length, then the raw payload bytes."""
    ser = pmt.serialize_str(msg)
    assert ser[0] == 0x02, f"not a PMT symbol (type tag {ser[0]:#04x}, expected 0x02)"
    length = int.from_bytes(ser[1:3], "big")
    return ser[3:3 + length]


class LoraTxEncoder(gr.hier_block2):
    """LoRa CSS TX chain: whitening -> header -> CRC -> Hamming FEC ->
    interleaving -> Gray mapping -> CSS modulation. Has NO stream input,
    only a complex64 IQ stream OUTPUT at bw*samp_rate_mult samples/sec.
    Triggered via send_payload(text), NOT a hierarchical message port
    (see module docstring's 5th real bug for why) -- call
    `lora_tx.send_payload("some text")` directly, no msg_connect needed
    from the caller's side.

    Only constructible if LORA_AVAILABLE -- callers must check that
    first (mirrors every other optional-dependency mode in this
    codebase, e.g. M17_AVAILABLE)."""

    def __init__(self, sf, bw, cr, has_crc=True, impl_head=False, ldro=2,
                 preamb_len=config.MESHTASTIC_PREAMBLE_LEN,
                 sync_word=None, samp_rate_mult=4):
        assert LORA_AVAILABLE, "gr-lora_sdr not installed -- see install-lora.sh"
        gr.hier_block2.__init__(
            self, "lora_tx_encoder",
            gr.io_signature(0, 0, 0),
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
        )
        self.sf = sf
        self.bw = bw
        self.cr = cr
        self.samp_rate = bw * samp_rate_mult
        if sync_word is None:
            sync_word = config.meshtastic_sync_symbols(sf)  # Meshtastic's on-air sync symbols for this SF

        self._whitening = _lora_sdr.whitening(False, True, ",", "packet_len")
        self._header = _lora_sdr.header(impl_head, has_crc, cr)
        self._add_crc = _lora_sdr.add_crc(has_crc)
        self._hamming_enc = _lora_sdr.hamming_enc(cr, sf)
        self._interleaver = _lora_sdr.interleaver(cr, sf, ldro, bw)
        self._gray_demap = _lora_sdr.gray_demap(sf)
        self._modulate = _lora_sdr.modulate(
            sf, self.samp_rate, bw, _sync_symbols(sync_word),
            int(20 * 2 ** sf * self.samp_rate / bw), preamb_len,
        )
        # Real bug #1 (see module docstring) -- without this, the scheduler
        # starves frame_sync downstream (in the RX chain this feeds, once
        # connected to one) before it ever runs.
        self._modulate.set_min_output_buffer(10_000_000)
        # Real bug #2 (see module docstring) -- without this, frame_sync
        # segfaults on the very first burst, at every SF tested.
        self._delay = blocks.delay(
            gr.sizeof_gr_complex, int(2 ** sf * self.samp_rate / bw * 10.1)
        )

        # send_payload_bytes()'s byte-safe stream source -- see
        # _ByteQueueSource's own docstring for why this is a permanent,
        # queue-fed block rather than a fresh-vector-source-per-
        # transmission with dynamic lock()/connect()/unlock() (deadlocks
        # once an RX chain is connected downstream in the same
        # top_block -- confirmed this session).
        self._stream_src = _ByteQueueSource()
        self.connect(self._stream_src, self._whitening)

        self.connect(self._whitening, self._header)
        self.connect(self._header, self._add_crc)
        self.connect(self._add_crc, self._hamming_enc)
        self.connect(self._hamming_enc, self._interleaver)
        self.connect(self._interleaver, self._gray_demap)
        self.connect(self._gray_demap, self._modulate)
        self.connect(self._modulate, self._delay)
        self.connect(self._delay, self)

    def send_payload(self, text: str):
        """Trigger a TX burst carrying `text` (plain short ASCII/text
        only -- see module docstring's 6th real bug: this path
        UTF-8-encodes internally, fine for real text, NOT byte-safe for
        arbitrary binary data). See the module docstring's 5th real bug
        for why this posts directly to the internal whitening block
        instead of exposing a hierarchical message port. For anything
        binary/high-entropy (an encrypted Meshtastic/MeshCore frame),
        use send_payload_bytes() instead."""
        self._whitening.post(pmt.intern("msg"), pmt.intern(text))

    def send_payload_bytes(self, raw: bytes):
        """Byte-safe counterpart of send_payload() -- see the module
        docstring's 6th real bug for the full story. Feeds `raw`
        through whitening's raw uint8_t STREAM input (NOT the PMT-symbol
        message path at all, so no UTF-8 involvement anywhere here) via
        the permanent `_ByteQueueSource` -- thread-safe, callable at any
        time, no lock()/connect() dance needed (see that class's own
        docstring for why the earlier fresh-vector-source design was
        replaced)."""
        self._stream_src.push(raw)
