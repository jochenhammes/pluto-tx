"""JTTY through wsjt-codec, the WSJT-X project's own Qt-free stream codec (docs/jtty/SPEC.md).

Nothing of JTTY is reimplemented here: its text packer, FEC, waveform and decoder stay upstream's, reached
over the codec's stream protocol (lib/streaming_io.f90, lib/streaming_jtty.f90 at the tag pinned in
packaging/components.lock): an 8-byte header, then frames [type:1][len:4 LE][body] on stdin, one JSON object
per line on stdout. render() runs one short-lived process per message; JttyStream is the long-lived one that
receives. They are separate because a request holds up the audio behind it in the codec.

The codec's bundled libraries lose to LD_LIBRARY_PATH (its README), and the apps set that for LoRa, so the
variable is removed from its environment.

Qt-free and GNU-Radio-free."""
import base64
import dataclasses
import functools
import json
import os
import struct
import subprocess
import tempfile
import threading
import wave

import numpy as np

from . import config, paths

SAMPLE_RATE = 12000                  # what the decoder takes
HEADER = b"WSJT\0\1" + struct.pack("<H", SAMPLE_RATE // 1000)  # int16 PCM, mono
FRAME_AUDIO = 0x01
FRAME_CONTROL = 0x02
RENDER_TIMEOUT_S = 20.0
STOP_TIMEOUT_S = 5.0
_REJECTIONS = {"empty": "message is empty",
               "too_long": "message is too long",
               "not_configured": "the text contains a % placeholder (%M, %G); write it out",
               "missing": "the text contains a % placeholder (%N, %R); write it out",
               "invalid_runtime": "the text contains a % placeholder that cannot be filled",
               "encoding_failed": "the codec could not encode this text"}


def codec_program():
    return paths.program("wsjt-codec")


def _environment():
    return {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}


def _frame(frame_type, body):
    return bytes([frame_type]) + struct.pack("<I", len(body)) + body


def _control(request):
    return _frame(FRAME_CONTROL, json.dumps(request).encode())


@dataclasses.dataclass(frozen=True)
class Rendered:
    audio: np.ndarray        # float32 at the requested rate, full scale +-1
    rate: int
    text: str                # what the frames carry, as a receiver shows it
    frames: tuple            # each frame's text
    substituted: bool        # a character JTTY lacks went out as '#'

    @property
    def duration_s(self):
        return len(self.audio) / self.rate


def render(text, tone_hz=config.JTTY_DEFAULT_TONE_HZ, rate=config.AUDIO_RATE, timeout=RENDER_TIMEOUT_S):
    """The audio of one message. Raises ValueError with a reason meant for the operator, before any RF action."""
    program = codec_program()
    if not program:
        raise ValueError("JTTY is not available: wsjt-codec is not installed, see install-jtty.sh")
    if len(text) > config.JTTY_MAX_TEXT_LEN:
        raise ValueError(f"message is longer than {config.JTTY_MAX_TEXT_LEN} characters")
    lo, hi = config.JTTY_TONE_RANGE_HZ
    if not lo <= tone_hz <= hi:
        raise ValueError(f"audio frequency must be {lo:.0f}..{hi:.0f} Hz")
    request = {"t": "render", "id": 1, "text": text, "freq": float(tone_hz), "rate": int(rate)}
    with tempfile.TemporaryDirectory(prefix="wsjt-codec-") as work:  # a render needs no FFT plans to be kept
        try:
            done = subprocess.run([program, "-a", work, "-t", work], cwd=work, env=_environment(),
                                  input=HEADER + _control(request) + _control({"t": "halt"}),
                                  capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ValueError(f"wsjt-codec failed: {e}") from e
    segments, pcm, finished = [], [], None
    for line in done.stdout.splitlines():
        reply = json.loads(line)
        if reply.get("id") != 1:
            continue
        if reply["t"] == "segment":
            segments.append(reply)
        elif reply["t"] == "pcm":
            pcm.append(base64.b64decode(reply["data"]))
        elif reply["t"] == "rejected":
            raise ValueError(_REJECTIONS.get(reply.get("reason"), reply.get("detail") or "the codec refused this text"))
        elif reply["t"] == "rendered":
            finished = reply
    if finished is None:
        raise ValueError(f"wsjt-codec gave no audio (exit {done.returncode})")
    if len(segments) != 1:  # a line break starts a second message, or the text ran past what a receiver keeps
        raise ValueError("message must be one line")
    audio = np.frombuffer(b"".join(pcm), dtype="<i2").astype(np.float32) / 32767.0
    return Rendered(audio, int(rate), segments[0]["canonical"], tuple(f["text"].rstrip() for f in segments[0]["frames"]),
                    bool(finished["substituted"]))


class JttyStream:
    """One receiving wsjt-codec process. on_event(dict) and on_exit(returncode, expected) run on its reader thread.
    send_audio() can block while the codec is busy, so the caller keeps it off the GNU Radio thread."""

    def __init__(self, on_event, on_exit=None, data_dir=None, program=None):
        program = program or codec_program()
        if not program:
            raise RuntimeError("wsjt-codec is not installed, see install-jtty.sh")
        # FFT plans are kept here across runs; measuring them again stalls the first reception.
        self.data_dir = str(data_dir or paths.user_dir("cache", create=True) / "jtty")
        os.makedirs(self.data_dir, exist_ok=True)
        self._on_event = on_event
        self._on_exit = on_exit
        self._write_lock = threading.Lock()
        self._closing = False
        self._proc = subprocess.Popen([program, "-a", self.data_dir, "-t", self.data_dir], cwd=self.data_dir,
                                      env=_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        self._reader = threading.Thread(target=self._read, name="jtty-codec", daemon=True)
        self._write(HEADER)
        self._reader.start()

    def _read(self):
        for line in self._proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:  # unknown output must not end reception
                continue
            self._on_event(event)
        code = self._proc.wait()
        self._proc.stdout.close()
        if self._on_exit:
            self._on_exit(code, self._closing)

    def _write(self, data):
        with self._write_lock:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()

    def configure(self, rxfreq, ntol, nfa, nfb):
        self.control({"t": "configure", "mode": "JTTY", "rxfreq": int(rxfreq), "ntol": int(ntol), "nfa": int(nfa), "nfb": int(nfb)})

    def control(self, request):
        self._write(_control(request))

    def send_audio(self, pcm_bytes):
        self._write(_frame(FRAME_AUDIO, pcm_bytes))

    def close(self):
        """Ends the session so messages in progress get their last line and the FFT plans are saved."""
        self._closing = True
        try:
            self.control({"t": "halt"})
            self._proc.stdin.close()
            self._proc.wait(timeout=STOP_TIMEOUT_S)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self._proc.kill()
            self._proc.wait()
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=STOP_TIMEOUT_S)

    @property
    def alive(self):
        return self._proc.poll() is None


def decode_pcm(pcm_bytes, rxfreq=1500, ntol=50, nfa=200, nfb=2800, data_dir=None):
    """Every message's last update in int16 12 kHz audio -> [dict]; ends the session itself. For tests and self-tests."""
    final = {}

    def on_event(event):
        if event.get("t") == "jtty_update":
            final[event["id"]] = event

    stream = JttyStream(on_event, data_dir=data_dir)
    stream.configure(rxfreq, ntol, nfa, nfb)
    for i in range(0, len(pcm_bytes), 8192):
        stream.send_audio(pcm_bytes[i:i + 8192])
    stream.close()
    return list(final.values())


def decode_pcm_file(path):
    """The texts in a 12 kHz mono WAV file."""
    with wave.open(path) as w:
        return [m["text"] for m in decode_pcm(w.readframes(w.getnframes()))]


@functools.lru_cache(maxsize=1)
def self_test():
    """True if the installed codec decodes what it rendered -- more than its mere presence."""
    if not codec_program():
        return False
    try:
        rendered = render("CQ DE SELF TEST", rate=SAMPLE_RATE)
        pcm = (rendered.audio * 1000.0).astype("<i2")
        silence = np.zeros(SAMPLE_RATE, dtype="<i2")
        texts = [m["text"] for m in decode_pcm(np.concatenate([silence, pcm, silence]).tobytes())]
    except (OSError, ValueError, RuntimeError):
        return False
    return rendered.text in texts


def available():
    return self_test()
