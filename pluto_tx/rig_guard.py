"""Watches over a rig's PTT from outside the app: CAT PTT has no dead man's handle.

Measured on a Xiegu X6200 (docs/x6200/TESTS.md): a keyed radio stays keyed when the host process dies or closes the port,
for at least 45 s. This process is started with a pipe from the app and needs neither GNU Radio nor the app's GIL. It
releases the PTT when the app's "key <seconds>" time is up, and when the pipe closes -- which also happens on kill -9.

Run as a script, by path (standard library plus rigctl.py): rig_guard.py <tcp port>
Protocol on stdin, one line each: "key <max seconds>", "unkey". On stdout: "released timeout" when it had to step in."""
import os
import select
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rigctl import RigctlClient, RigError  # noqa: E402

RELEASE_ATTEMPTS = 20
RELEASE_RETRY_S = 0.25
SETTLE_S = 0.35   # the X6200 reports a new PTT state up to 200 ms late


def release(client):
    """-> True once the radio reports RX. Keeps trying: this is the last line of defence."""
    for _ in range(RELEASE_ATTEMPTS):
        try:
            client.set_ptt(False)
            time.sleep(SETTLE_S)
            if not client.get_ptt():
                return True
        except RigError:
            time.sleep(RELEASE_RETRY_S)
    return False


def run(tcp_port, stdin_fd=0, out=sys.stdout):
    client = RigctlClient(port=tcp_port, timeout=1.0)
    deadline = None
    buffered = b""
    while True:
        wait = None if deadline is None else max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([stdin_fd], [], [], wait)
        if not ready:
            deadline = None
            print("released timeout" if release(client) else "release failed", file=out, flush=True)
            continue
        data = os.read(stdin_fd, 4096)
        if not data:  # the app is gone, however it went
            release(client)
            break
        buffered += data
        while b"\n" in buffered:
            line, buffered = buffered.split(b"\n", 1)
            word = line.decode("ascii", "replace").split()
            if word and word[0] == "key" and len(word) == 2:
                deadline = time.monotonic() + float(word[1])
            elif word and word[0] == "unkey":
                deadline = None
    client.close()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # Ctrl-C reaches the whole group: the app must get to release first
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # likewise; only the closed pipe ends this process
    run(int(sys.argv[1]))
