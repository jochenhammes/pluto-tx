"""Shared wrapper machinery for pluto_cli's tx/rx commands.

Every piece here exists because pluto_tx's/pluto_advanced_rx's GUIs
currently mediate the equivalent behavior via a QtCore.QTimer -- nothing
inside PlutoTxFlowgraph/AdvancedRxFlowgraph themselves needs Qt at all
(verified this session: neither flowgraph.py imports Qt anywhere; only
gui.py/waterfall_widget.py do). This module replaces every QTimer-driven
GUI behavior this CLI needs with a plain, blocking time.sleep()-based
equivalent, suitable for a synchronous CLI process. See pluto_cli/README.md
for the full picture (this module is intentionally light on prose
comments beyond WHY each piece exists -- the README is the place for
usage-level explanation).
"""
import json
import os
import signal
import sys
import time


class Emitter:
    """Human-readable or JSON-lines output, one event per line -- the
    thing that actually makes pluto_cli's output easy to consume from
    another script/workflow (a fixed, documented per-event field shape)
    instead of something to scrape by regex. See pluto_cli/README.md's
    "JSON output schema" section for the authoritative event/field list."""

    def __init__(self, json_mode: bool):
        self.json_mode = json_mode

    def emit(self, event: str, **fields):
        if self.json_mode:
            print(json.dumps({"event": event, **fields}), flush=True)
        else:
            extra = " ".join(f"{k}={v}" for k, v in fields.items())
            print(f"{event} {extra}".rstrip(), flush=True)

    def error(self, message: str, **fields):
        self.emit("error", message=message, **fields)

    def emit_char(self, char: str, event: str = "psk31_char"):
        """A digimode's decoded-character stream (PSK31's "psk31_char" by
        default, or RTTY's "rtty_char") -- text mode writes a raw,
        unframed character stream (reads like a live chat feed, matching
        what the GUI's transcript box shows growing); JSON mode emits one
        structured event per character instead, matching every other
        event type's shape."""
        if self.json_mode:
            self.emit(event, char=char)
        else:
            sys.stdout.write(char)
            sys.stdout.flush()


def json_safe(value):
    """Recursively converts numpy arrays (M17's decoded "fields" dict
    contains numpy.uint8 arrays for type/meta, see m17_deframer.py) and
    other non-JSON-native values into plain Python types, for Emitter's
    json.dumps() to handle without raising."""
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if hasattr(value, "tolist"):  # numpy array
        return value.tolist()
    return value


def resolve_connection(device_cls, uri):
    """Mirrors gui.py's own _connect()/_rebuild() connection-string
    handling: a bare host/IP typed for a 'uri'-kind device (Pluto) is
    normalized into a libiio URI (normalize_uri() -- 'ip:' prefixed
    unless already an explicit scheme); every other connection_kind
    (serial/soapy_args/audio_device) is used exactly as typed.
    `uri=None` (pluto_cli's un-set --uri default) falls back to the
    device class's own DEFAULT_CONNECTION -- the same value
    devices.build_device() itself falls back to -- so probe_or_exit()
    below probes the EXACT connection string the flowgraph is about to
    open, not some other guess."""
    if uri is None:
        return device_cls.DEFAULT_CONNECTION
    if device_cls.connection_kind == "uri":
        from pluto_tx.config import normalize_uri
        return normalize_uri(uri)
    return uri.strip()


def probe_or_exit(device_cls, connection, emitter: Emitter):
    """Probes `connection` with a bounded timeout BEFORE the (much
    slower to fail, and far less friendly-looking) flowgraph
    construction -- exactly the same probe_with_timeout() check gui.py's
    own Connect button runs in _rebuild() before building a new
    PlutoTxFlowgraph/AdvancedRxFlowgraph. Without this, a wrong mDNS
    hit/unreachable IP/missing HackRF or RTL-SDR/busy audio device
    surfaces as a raw OSError/SoapySDR traceback several frames deep
    inside flowgraph.py -- real example hit on real hardware this
    session: 'ip:plutoplus.local' resolved via mDNS to a stale/wrong
    LAN IP (the Pluto+'s WiFi interface) instead of its actual
    192.168.2.1 USB-network address, raising 'OSError: [Errno 113] No
    route to host' with no indication of WHY. Exits the process (code
    1) with one clear line instead, mirroring confirm_or_exit()'s
    own "give up with a clear reason" shape."""
    probe_error = device_cls.probe_with_timeout(connection)
    if probe_error is not None:
        label = connection or "auto-detect"
        emitter.error(
            f"could not connect to {device_cls.display_name} ({label}): {probe_error}"
        )
        sys.exit(1)


def build_or_exit(build_fn, device_cls, connection, emitter: Emitter):
    """Wraps the actual PlutoTxFlowgraph()/AdvancedRxFlowgraph()
    construction in a try/except that turns any connection failure into
    the same one-line, human-readable message as probe_or_exit() above,
    instead of a raw multi-frame OSError/RuntimeError/SoapySDR
    traceback. This is a deliberate SECOND layer, not a replacement for
    probe_or_exit(): probing happens on a background thread with a hard
    timeout specifically because a truly unreachable host can otherwise
    hang forever with no bounded wait -- construction itself has no such
    timeout. The two checks can disagree: real example hit on real
    hardware this session -- Pluto+'s mDNS name resolved inconsistently
    between two successive lookups (its WiFi interface's LAN IP one
    moment, its actual USB-network 192.168.2.1 the next), so probing
    briefly succeeded while construction's own, separate DNS lookup
    moments later still hit the wrong address and raised 'OSError:
    [Errno 113] No route to host' deep inside flowgraph.py. Exits the
    process (code 1) either way."""
    try:
        return build_fn()
    except Exception as e:
        label = connection or "auto-detect"
        emitter.error(f"could not connect to {device_cls.display_name} ({label}): {e}")
        sys.exit(1)


def install_safety_handlers(tb, emitter: Emitter):
    """SIGINT/SIGTERM handlers + sys.excepthook, all converging on
    whichever shutdown method the flowgraph exposes (TX:
    shutdown_safe(), which also unkeys if still keyed and forces the
    device safe; RX: shutdown(), plain stop()/wait()) -- directly
    generalizes pluto_tx/app.py's own proven pattern (SIGINT/SIGTERM
    handler + excepthook, both calling shutdown_safe()) to work for
    either flowgraph class. Both shutdown methods are documented as
    idempotent, so a signal firing after a normal exit (or the reverse)
    is harmless."""
    shutdown = getattr(tb, "shutdown_safe", None) or tb.shutdown

    def sig_handler(signum, frame):
        emitter.emit("signal", signum=signum)
        shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    orig_excepthook = sys.excepthook

    def excepthook(exc_type, exc_value, exc_tb):
        emitter.error(f"uncaught exception: {exc_value}")
        shutdown()
        orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook


def confirm_or_exit(yes: bool, emitter: Emitter):
    """The --yes / input("Type YES to key up: ") gate, generalizing
    pluto_tx/app.py's exact existing confirmation pattern -- exits the
    process (code 1) rather than returning if the operator declines,
    matching app.py's own behavior."""
    if yes:
        return
    reply = input("Type YES to key up: ")
    if reply.strip() != "YES":
        emitter.emit("aborted", reason="not confirmed")
        sys.exit(1)


def run_tx_session(tb, mode, args, emitter: Emitter):
    """key_ptt() -> wait for completion (mode-dependent) -> unkey_ptt()
    (+ mode-specific tail wait) -> shutdown_safe(), always, even on
    error. Mirrors pluto_tx/app.py's exact key_ptt()->sleep()->
    unkey_ptt() shape, generalized across every mode's different
    "how long does this transmission actually take" semantics --
    Digitext/PSK31/RTTY render fresh audio inside key_ptt() itself (Meshtastic
    builds its packet there and reports tb.meshtastic_hold_s) and
    only reveal their real duration afterward (tb.digitext_duration_s/
    tb.psk31_duration_s/tb.rtty_duration_s), so --duration is ignored
    for those three."""
    from pluto_tx.flowgraph import PlutoTxFlowgraph
    from pluto_tx import config as tx_config

    try:
        if args.interactive:
            emitter.emit("interactive_ready", hint="press Enter to key/unkey, Ctrl-C to quit")
            while True:
                input()
                if not tb.keyed:
                    tb.key_ptt()
                    emitter.emit("keyed", mode=mode)
                else:
                    tb.unkey_ptt()
                    emitter.emit("unkeyed", mode=mode)
                    _tail_wait_tx(tb, mode)
        else:
            count = getattr(args, "repeat_count", 1) or 1
            interval = getattr(args, "repeat_interval", 0.0)
            for i in range(count):
                if i:
                    emitter.emit("repeat_wait", seconds=interval, next=i + 1, of=count)
                    time.sleep(interval)
                try:
                    tb.key_ptt()
                except ValueError as e:  # a refusal (e.g. Meshtastic duty cycle) ends a series, not the first call
                    if i == 0:
                        raise
                    emitter.error(f"repeat stopped after {i} of {count}: {e}")
                    break
                emitter.emit("keyed", mode=mode, **({"repetition": i + 1, "of": count} if count > 1 else {}))
                if mode == PlutoTxFlowgraph.MODE_DIGITEXT:
                    time.sleep(tb.digitext_duration_s)
                elif mode == PlutoTxFlowgraph.MODE_PSK31:
                    time.sleep(tb.psk31_duration_s)
                elif mode == PlutoTxFlowgraph.MODE_RTTY:
                    time.sleep(tb.rtty_duration_s)
                elif mode == PlutoTxFlowgraph.MODE_MESHTASTIC:
                    time.sleep(tb.meshtastic_hold_s)
                elif mode == PlutoTxFlowgraph.MODE_POCSAG:
                    time.sleep(tb.pocsag_hold_s)
                elif mode == PlutoTxFlowgraph.MODE_MESHCORE:
                    time.sleep(tb.meshcore_hold_s)
                else:
                    time.sleep(args.duration)
                tb.unkey_ptt()
                emitter.emit("unkeyed", mode=mode)
                _tail_wait_tx(tb, mode)
    finally:
        tb.shutdown_safe()
        emitter.emit("shutdown")


def _tail_wait_tx(tb, mode):
    from pluto_tx.flowgraph import PlutoTxFlowgraph
    from pluto_tx import config as tx_config

    if mode == PlutoTxFlowgraph.MODE_M17:
        time.sleep(tx_config.M17_EOT_HOLD_S)
        tb.finish_unkey_m17()
    elif mode == PlutoTxFlowgraph.MODE_RADE and getattr(tb, "rade_eoo_enabled", False):
        time.sleep(tb.rade_eoo_hold_s)
        tb.finish_unkey_rade()


def run_rx_session(tb, args, emitter: Emitter, filebroadcast_state=None):
    """tb.start() -> unmute -> a plain time.sleep() tick loop (NOT a Qt
    timer) replacing gui.py's QTimer-driven _poll_digimode()/
    _poll_filebroadcast() -- ticks whichever digimode's AFC step matches
    tb.active_digimode (AFC never advances otherwise -- confirmed nothing
    inside AdvancedRxFlowgraph calls it automatically) and watches
    FileBroadcastState for newly-complete files to save -> tb.shutdown()
    in finally, always."""
    from pluto_advanced_rx import config as rx_config

    tb.start()
    tb.set_rx_muted(False)
    emitter.emit("started")

    saved_files = set()
    last_progress = {}
    start_time = time.monotonic()
    last_afc = 0.0
    tick_s = 0.2
    try:
        while True:
            if args.duration is not None and time.monotonic() - start_time >= args.duration:
                break
            time.sleep(tick_s)
            now = time.monotonic()

            if tb.active_digimode == "psk31" and now - last_afc >= rx_config.PSK31_AFC_POLL_INTERVAL_S:
                last_afc = now
                tb.psk31_afc_step()
            elif tb.active_digimode == "rtty" and now - last_afc >= rx_config.RTTY_AFC_POLL_INTERVAL_S:
                last_afc = now
                tb.rtty_afc_step()

            if filebroadcast_state is not None:
                for entry in filebroadcast_state.get_snapshot():
                    file_id = entry["file_id"]
                    if last_progress.get(file_id) != entry["bytes_received"]:
                        last_progress[file_id] = entry["bytes_received"]
                        emitter.emit(
                            "filebroadcast_progress", file_id=file_id, filename=entry["filename"],
                            bytes_received=entry["bytes_received"], total_size=entry["total_size"],
                            is_complete=entry["is_complete"],
                        )
                    if (entry["is_complete"] and file_id not in saved_files
                            and args.filebroadcast_save_dir):
                        path = os.path.join(args.filebroadcast_save_dir, entry["filename"])
                        if filebroadcast_state.save_to_disk(file_id, path):
                            saved_files.add(file_id)
                            emitter.emit("filebroadcast_saved", file_id=file_id, filename=entry["filename"], path=path)
    except KeyboardInterrupt:
        pass
    finally:
        tb.shutdown()
        emitter.emit("shutdown")
