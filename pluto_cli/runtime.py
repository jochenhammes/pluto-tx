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
        elif mode == PlutoTxFlowgraph.MODE_FT8:
            _run_ft8_series(tb, args, emitter)
        elif mode == PlutoTxFlowgraph.MODE_JS8:
            _run_js8_series(tb, args, emitter)
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


def _run_ft8_series(tb, args, emitter: Emitter):
    """FT8: every transmission waits for its UTC slot (pluto_tx.ft8.plan_transmission(): keyed
    FT8_KEY_EARLY_S ahead, the flowgraph pads with silence up to tb.ft8_start_at -- same as the GUI);
    repeats stay in the parity of the first one."""
    from pluto_tx.flowgraph import PlutoTxFlowgraph
    from pluto_tx import config as tx_config
    from pluto_tx import ft8 as tx_ft8

    count = getattr(args, "repeat_count", 1) or 1
    parity = args.slot
    for i in range(count):
        tb.prepare_ft8()
        at, start = tx_ft8.plan_transmission(time.time(), parity, tx_config.FT8_KEY_EARLY_S,
                                             tx_config.FT8_START_IN_SLOT_S, tx_config.FT8_LATE_START_MAX_S)
        parity = tx_ft8.slot_parity(start)
        emitter.emit("ft8_waiting", slot_utc=time.strftime("%H:%M:%S", time.gmtime(tx_ft8.current_slot_start(start))),
                     seconds=round(max(0.0, start - time.time()), 1),
                     **({"repetition": i + 1, "of": count} if count > 1 else {}))
        while time.time() < at:
            time.sleep(min(0.05, max(0.0, at - time.time())))
        tb.ft8_start_at = start
        tb.key_ptt()
        emitter.emit("keyed", mode=PlutoTxFlowgraph.MODE_FT8)
        time.sleep(tb.ft8_hold_s)
        tb.unkey_ptt()
        emitter.emit("unkeyed", mode=PlutoTxFlowgraph.MODE_FT8)


# JS8 drift pre-compensation across CLI runs: the drift model (PlutoTxFlowgraph.ft8_drift_hz_per_s()) counts
# its warm-up from the flowgraph's start, but pluto_cli starts a fresh flowgraph for every message, so in a
# series it always predicted a cold device and over-compensated (J7 on 2 m: SLOW frames lost). The last
# transmission per device is kept here; a run within JS8_DRIFT_SESSION_GAP_S of it continues that session.
JS8_DRIFT_STATE_PATH = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                                    "pluto-tx", "js8_drift.json")
# Longer pauses start cold again (J7: the first frame after 8 min drifted like a freshly started device).
JS8_DRIFT_SESSION_GAP_S = 300.0


def _js8_drift_key(args):
    from pluto_tx import devices as tx_devices
    try:
        connection = resolve_connection(tx_devices.DEVICE_REGISTRY[args.device], args.uri)
    except Exception:
        connection = getattr(args, "uri", None)
    return f"{args.device}:{connection or ''}"


def _js8_drift_load(path=None):
    try:
        with open(path or JS8_DRIFT_STATE_PATH) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def js8_drift_restore(tb, key, now=None, path=None):
    """Before the first frame: continue the device's session if its last JS8 transmission (from an earlier
    CLI run) ended at most JS8_DRIFT_SESSION_GAP_S ago. Returns the session start (wall clock) to save
    afterwards."""
    now = time.time() if now is None else now
    entry = _js8_drift_load(path).get(key) or {}
    try:
        session_start, last_end = float(entry["session_start"]), float(entry["last_tx_end"])
    except (KeyError, TypeError, ValueError):
        return now
    if not 0.0 <= now - last_end <= JS8_DRIFT_SESSION_GAP_S or session_start > last_end:
        return now
    tb.restore_drift_history(now - session_start, now - last_end)
    return session_start


def js8_drift_save(key, session_start, last_tx_end, path=None):
    path = path or JS8_DRIFT_STATE_PATH
    state = _js8_drift_load(path)
    state[key] = {"session_start": session_start, "last_tx_end": last_tx_end}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        pass                    # only the drift estimate of the next run suffers


def _run_js8_series(tb, args, emitter: Emitter):
    """JS8: the message's frames in consecutive periods of its speed (pluto_tx.js8.plan_frames(): the first
    frame keyed JS8_KEY_EARLY_S ahead -- one source swap --, every later one JS8_REKEY_EARLY_S ahead), each
    frame keyed and unkeyed on its own. Ctrl-C / SIGTERM runs shutdown_safe(), which aborts the rest."""
    from pluto_tx.flowgraph import PlutoTxFlowgraph
    from pluto_tx import config as tx_config
    from pluto_tx import js8 as tx_js8

    n = len(tb.js8_frames)
    plan = tx_js8.plan_frames(time.time(), tb.js8_submode, n, key_early_s=tx_config.JS8_KEY_EARLY_S,
                              late_max_s=tx_config.JS8_LATE_START_MAX_S,
                              rekey_early_s=tx_config.JS8_REKEY_EARLY_S)
    start = plan[0][1]
    period = tx_js8.speed_info(tb.js8_submode)["period_s"]
    emitter.emit("js8_waiting", slot_utc=time.strftime("%H:%M:%S", time.gmtime(start // period * period)),
                 seconds=round(max(0.0, start - time.time()), 1), frames=n)
    tb.js8_start_at = start
    drift_key = _js8_drift_key(args)
    session_start = js8_drift_restore(tb, drift_key)
    sent = 0
    keyed_any = False
    try:
        for i, (key_at, _) in enumerate(plan):
            while time.time() < key_at:
                time.sleep(min(0.05, max(0.0, key_at - time.time())))
            keyed_any = True
            tb.key_ptt()
            emitter.emit("keyed", mode=PlutoTxFlowgraph.MODE_JS8, frame=i + 1, of=n,
                         drift_comp_hz_s=round(tb.js8_last_drift_comp_hz_s, 4))
            time.sleep(tb.js8_hold_s)
            tb.unkey_ptt()
            emitter.emit("unkeyed", mode=PlutoTxFlowgraph.MODE_JS8, frame=i + 1, of=n)
            frame, flags = tb.js8_frames[i]
            emitter.emit("js8_frame_sent", frame_no=i + 1, of=n, frame=frame, flags=flags)
            sent += 1
    except BaseException:
        tb.js8_cancel()
        emitter.emit("js8_cancelled", sent=sent, of=n)
        raise
    finally:
        if keyed_any:
            js8_drift_save(drift_key, session_start, time.time())
    emitter.emit("js8_done", frames=n)


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
