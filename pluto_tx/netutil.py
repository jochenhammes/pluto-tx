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
import ipaddress
import json
import subprocess
import threading

import iio

from . import config

CONNECT_TIMEOUT_S = 5.0
# Short: a local USB-Ethernet-gadget link, not a real network hop --
# should resolve near-instantly if reachable at all. See
# _augment_with_pluto_usb_ip()'s own docstring below.
PLUTO_USB_IP_PROBE_TIMEOUT_S = 2.0
# Short: `ip addr`/`ip neigh` read local kernel state only, no network
# round-trip -- this is a generous safety backstop, not an expected wait.
# The final IIO probe below is the only part that actually talks to the
# network, and is bounded by this same timeout. See
# _augment_with_direct_ethernet_pluto()'s own docstring below.
DIRECT_ETH_TIMEOUT_S = 2.0


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


def scan_devices_with_timeout(timeout_s=CONNECT_TIMEOUT_S):
    """Enumerate reachable libiio contexts (mDNS-discovered network devices,
    USB, and this machine's own local IIO devices) on a background thread,
    bounded by timeout_s. Returns (devices, None) on success -- a dict of
    uri -> human-readable description, exactly iio.scan_contexts()'s own
    return shape (plus one possible extra entry, see
    _augment_with_pluto_usb_ip() below) -- or (None, exception) on failure/
    timeout. Never touches Qt, so it's always safe to run off the main
    thread. In practice this scan is fast (~1s, mDNS + USB + local); the
    timeout is just a backstop."""
    devices, error = _scan_contexts_with_timeout(timeout_s)
    if error is not None:
        return None, error
    _augment_with_pluto_usb_ip(devices)
    _augment_with_direct_ethernet_pluto(devices)
    return devices, None


def _scan_contexts_with_timeout(timeout_s):
    """The original scan_devices_with_timeout() body, factored out so the
    augmentation above can wrap it without nesting a second bounded probe
    inside this function's own worker thread (see
    _augment_with_pluto_usb_ip()'s docstring for why that matters)."""
    result = {}

    def worker():
        try:
            result["devices"] = iio.scan_contexts()
        except BaseException as e:
            result["error"] = e

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_s)

    if thread.is_alive():
        return None, TimeoutError(f"no response after {timeout_s:g}s")
    if "error" in result:
        return None, result["error"]
    return result["devices"], None


def _augment_with_pluto_usb_ip(devices):
    """If a Pluto is locally USB-attached (a usb:X.Y.Z entry is present)
    and its fixed default network IP isn't already a separate entry,
    probe it directly -- bypasses ip:plutoplus.local's mDNS resolution
    entirely (a literal IP needs no hostname lookup at connect time),
    giving the operator a reliably-reconnectable option even when mDNS
    is having one of its flaky moments (a real, repeated finding earlier
    this project -- see pluto_cli/runtime.py's probe_or_exit()/
    build_or_exit() docstrings for two concrete real-hardware instances).

    Deliberately called from OUTSIDE _scan_contexts_with_timeout()'s own
    worker thread (after it already returned successfully), not nested
    inside it -- so a slow/unreachable probe here can never turn an
    otherwise-successful scan into a false timeout for the caller; it can
    only ever cost its own small, separate, bounded
    PLUTO_USB_IP_PROBE_TIMEOUT_S on top. Best-effort and silent either
    way: probe_uri_with_timeout() never raises (always returns None or an
    exception object), so a failure/timeout here just means the extra
    entry isn't added -- the scan's own real results are returned
    regardless. Mutates `devices` in place."""
    if not any(key.startswith("usb:") for key in devices):
        return
    ip_uri = f"ip:{config.PLUTO_USB_DEFAULT_IP}"
    if ip_uri in devices:
        return
    if probe_uri_with_timeout(ip_uri, PLUTO_USB_IP_PROBE_TIMEOUT_S) is None:
        devices[ip_uri] = f"{config.PLUTO_USB_DEFAULT_IP} (Pluto via USB-Ethernet-Gadget, default IP)"


def _augment_with_direct_ethernet_pluto(devices):
    """A Pluto directly cabled to this machine's Ethernet port (no router/
    switch/DHCP server in between) self-assigns an RFC 3927 link-local
    address (169.254.0.0/16) via avahi, and so does this machine's own NIC
    on the same link -- confirmed real, working, zero-configuration
    end-to-end on real hardware this session (a genuine iio_info/libiio
    session over ip:169.254.x.x, no config.txt changes needed at all).

    Deliberately NOT discovered via mDNS (ip:plutoplus.local): reproduced
    real, live mDNS resolution failures this same session, moments after an
    identical lookup had succeeded, simply because more simultaneously-
    reachable interfaces (USB-gadget, WiFi, and now direct Ethernet) make
    which address a hostname resolves to ambiguous/flaky. Instead: find
    this machine's own interface(s) that are up, have a carrier, and have
    ONLY a link-local IPv4 address (no DHCP/static one too) -- the
    distinctive signature of "cable plugged in, nothing else configured".
    A genuine point-to-point cable can have at most one other host on that
    L2 segment, so any 169.254.x.x entry in that interface's own ARP/
    neighbor table is almost certainly the Pluto -- confirmed live this
    session (`ip -j neigh show dev eno1` correctly returned exactly the
    Pluto's own address, no extra provocation needed).

    Same best-effort/bounded/mutate-in-place contract as
    _augment_with_pluto_usb_ip() above, and the same real-IIO-connection
    rigor before adding an entry -- a neighbor-table hit only means
    SOMETHING answered ARP on that link, not that it's actually a Pluto."""
    try:
        addr_json = subprocess.run(
            ["ip", "-j", "addr", "show"],
            capture_output=True, timeout=DIRECT_ETH_TIMEOUT_S, text=True, check=True,
        ).stdout
        interfaces = json.loads(addr_json)
    except Exception:
        return  # best-effort -- no `ip` binary, timeout, unexpected output, etc.

    link_local_net = ipaddress.ip_network("169.254.0.0/16")
    for iface in interfaces:
        if "LOWER_UP" not in iface.get("flags", []):
            continue
        inet_addrs = [a for a in iface.get("addr_info", []) if a.get("family") == "inet"]
        if not inet_addrs or any(a.get("scope") != "link" for a in inet_addrs):
            continue  # no IPv4 at all, or has a real (DHCP/static) one too -- not our direct-cable case
        try:
            neigh_json = subprocess.run(
                ["ip", "-j", "neigh", "show", "dev", iface["ifname"]],
                capture_output=True, timeout=DIRECT_ETH_TIMEOUT_S, text=True, check=True,
            ).stdout
            neighbors = json.loads(neigh_json)
        except Exception:
            continue
        for n in neighbors:
            if n.get("state") in (["FAILED"], ["INCOMPLETE"]):
                continue
            dst = n.get("dst", "")
            try:
                if ipaddress.ip_address(dst) not in link_local_net:
                    continue
            except ValueError:
                continue
            ip_uri = f"ip:{dst}"
            if ip_uri in devices:
                continue
            if probe_uri_with_timeout(ip_uri, DIRECT_ETH_TIMEOUT_S) is None:
                devices[ip_uri] = f"{dst} (Pluto via direct Ethernet cable, no router/DHCP)"
