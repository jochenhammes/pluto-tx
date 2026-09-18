"""LoRa CSS PHY RX decoder, wrapping gr-lora_sdr -- RX-side counterpart of
pluto_tx/lora.py's LoraTxEncoder (see that module's docstring for the full
three-real-bugs investigation writeup this session; not repeated here,
only the RX-specific parts are).

LORA_AVAILABLE is its own independent try/except here (not imported from
pluto_tx.lora) -- matches this codebase's established convention of
duplicating optional-dependency availability flags per file rather than
sharing one central import (see pluto_tx/flowgraph.py's own
M17_AVAILABLE/RADE_AVAILABLE comments for why).

Message-output, not stream-output, for decoded payloads -- mirrors
pluto_advanced_rx/m17_deframer.py's M17FieldsDeframer (message-port-based,
PMT dict output) rather than rtty_deframer.py's character-stream approach:
Meshtastic/MeshCore packets are structured, arrive as whole frames, not a
character-at-a-time stream. The actual Meshtastic/MeshCore protocol
decoding (Phase 2 of the LoRa mesh plan) happens OUTSIDE this class, fed
the raw decoded payload bytes this emits -- this module only implements
the LoRa CSS PHY, deliberately no protocol-layer knowledge at all."""
try:
    from gnuradio import lora_sdr as _lora_sdr
    LORA_AVAILABLE = True
except ImportError:
    _lora_sdr = None
    LORA_AVAILABLE = False

from gnuradio import gr, blocks

from pluto_tx import config as _tx_config
from pluto_tx.lora import _sync_symbols


class LoraRxDecoder(gr.hier_block2):
    """LoRa CSS RX chain: frame sync -> FFT demod -> Gray mapping ->
    deinterleaving -> Hamming FEC decode -> header decode -> dewhitening
    -> CRC verification. Takes a complex64 IQ stream in, emits decoded
    payloads (PMT string messages, ASCII -- matches gr-lora_sdr's own
    crc_verif "msg" output convention) on a hierarchical "msg" message
    output port.

    Only constructible if LORA_AVAILABLE -- callers must check that
    first, same as LoraTxEncoder."""

    def __init__(self, sf, bw, cr, center_freq_hz, has_crc=True, impl_head=False,
                 ldro=2, preamb_len=_tx_config.MESHTASTIC_PREAMBLE_LEN,
                 sync_word=None, samp_rate_mult=4):
        assert LORA_AVAILABLE, "gr-lora_sdr not installed -- see install-lora.sh"
        # Real bug #3 (see pluto_tx/lora.py's module docstring): frame_sync
        # segfaults unconditionally with center_freq=0, even in a pure
        # baseband/no-real-RF context -- bisected across SF7-SF12 this
        # session, all crashed at 0, all passed cleanly at a real RF
        # frequency. Enforced here, not just documented, so this bug can
        # never resurface by a caller passing 0 "since there's no real RF
        # anyway" (the exact, reasonable-looking mistake that caused it
        # originally this session).
        assert center_freq_hz != 0, (
            "center_freq_hz must not be 0 -- gr-lora_sdr's frame_sync segfaults "
            "unconditionally at 0, even in a pure baseband loopback with no real "
            "RF (bisected across SF7-SF12 this session, see pluto_tx/lora.py's "
            "module docstring for the full writeup). Pass a real frequency, e.g. "
            "config.LoraPreset.frequency_hz."
        )
        gr.hier_block2.__init__(
            self, "lora_rx_decoder",
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
            gr.io_signature(0, 0, 0),
        )
        self.sf = sf
        self.bw = bw
        self.cr = cr
        self.samp_rate = bw * samp_rate_mult
        if sync_word is None:
            sync_word = _tx_config.meshtastic_sync_symbols(sf)  # Meshtastic's on-air sync symbols for this SF

        self.message_port_register_hier_out("msg")

        # Real bug #1's RX-side counterpart: frame_sync needs a large
        # min_output_buffer on whatever feeds it -- (2^sf+2)*samp_rate/bw,
        # matching gr-lora_sdr's own reference example's channel_model
        # minoutbuf setting. Applied to an explicit internal pass-through
        # block (not left as the caller's responsibility) so LoraRxDecoder
        # is self-contained -- a caller connecting into this hier block's
        # stream input doesn't need to know this quirk exists.
        self._input_buffer = blocks.multiply_const_cc(1.0 + 0.0j)
        self._input_buffer.set_min_output_buffer(int((2 ** sf + 2) * self.samp_rate / bw))

        self._frame_sync = _lora_sdr.frame_sync(
            int(center_freq_hz), bw, sf, impl_head, _sync_symbols(sync_word), samp_rate_mult, preamb_len
        )
        self._fft_demod = _lora_sdr.fft_demod(False, True)
        self._gray_mapping = _lora_sdr.gray_mapping(False)
        self._deinterleaver = _lora_sdr.deinterleaver(False)
        self._hamming_dec = _lora_sdr.hamming_dec(False)
        self._header_decoder = _lora_sdr.header_decoder(impl_head, cr, 255, has_crc, ldro, False)
        self._dewhitening = _lora_sdr.dewhitening()
        self._crc_verif = _lora_sdr.crc_verif(0, False)  # print_rx_msg=0 -- silent, we use the msg port

        self.connect(self, self._input_buffer)
        self.connect(self._input_buffer, self._frame_sync)
        self.connect(self._frame_sync, self._fft_demod)
        self.connect(self._fft_demod, self._gray_mapping)
        self.connect(self._gray_mapping, self._deinterleaver)
        self.connect(self._deinterleaver, self._hamming_dec)
        self.connect(self._hamming_dec, self._header_decoder)
        self.connect(self._header_decoder, self._dewhitening)
        self.connect(self._dewhitening, self._crc_verif)
        self.msg_connect(self._header_decoder, "frame_info", self._frame_sync, "frame_info")
        self.msg_connect(self._crc_verif, "msg", self, "msg")
