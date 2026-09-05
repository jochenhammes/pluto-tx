"""Bounding a blocking device-connection attempt.

Constructing an iio.Context (directly in PlutoSafety, or indirectly inside
gr-iio's fmcomms2_sink_fc32/fmcomms2_source_fc32) is a synchronous call with
no connect-timeout of its own. A wrong address that IS reachable at the IP
layer but never answers as expected (e.g. a live host with something else
listening, or an interrupted network path) can leave that call blocking the
whole GUI thread far longer than any "try to connect" button press should
reasonably wait -- the window stops repainting and looks crashed, even
though it would eventually raise a normal Python exception.

probe_uri_with_timeout() runs a throwaway raw-libiio connection attempt on a
background thread, bounded to timeout_s, to check reachability BEFORE the
real flowgraph (PlutoTxFlowgraph/PlutoRxFlowgraph) is constructed. The real
construction then happens synchronously on the caller's thread as normal --
it must, because it builds real Qt widgets (the waterfall sink), and Qt
widgets can only be created on the main GUI thread; building them on a
background thread produces "QObject::setParent: Cannot set parent, new
parent is in a different thread" warnings and undefined widget behavior
(this was tried and reverted). If the probe already succeeded, the real
construction's own connection attempt is expected to complete quickly too
(same server, same network path) -- the probe is a cheap way to fail fast
without ever touching Qt off the main thread.
"""
import threading

import iio

CONNECT_TIMEOUT_S = 5.0


def probe_uri_with_timeout(uri, timeout_s=CONNECT_TIMEOUT_S):
    """Try opening a raw iio.Context to `uri` on a background thread, bounded
    by timeout_s. Returns None if a context was opened successfully, or the
    exception (including our own TimeoutError) that reachability failed
    with. Never touches Qt, so it's always safe to run off the main thread."""
    result = {}

    def worker():
        try:
            iio.Context(uri)
        except BaseException as e:
            result["error"] = e

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_s)

    if thread.is_alive():
        return TimeoutError(f"no response after {timeout_s:g}s")
    return result.get("error")
