"""The operator's station data -- callsign and Maidenhead locator.

A setting of its own, not part of any mode: FT8 builds its messages from it
(and hands it to jt9 for a-priori decoding), M17 uses the callsign as the
default source. Pure Python, no GNU Radio; SessionManager validates every
set_station request here, backends only store the result.

No personal data as defaults in the source: the start values come from
WEB_TRX_STATION_CALL / WEB_TRX_STATION_LOCATOR (web-trx.env on the radio
server) and only apply while nothing has been saved yet.
"""
from __future__ import annotations

import os
import re

_CALL_RE = re.compile(r"^[A-Z0-9/]{3,10}$")
_LOCATOR_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$")


def normalize(call: str, locator: str) -> dict:
    """-> {"call", "locator"}, upper-cased; empty strings mean "not set".
    Raises ValueError with a message meant for the operator."""
    if not isinstance(call, str) or not isinstance(locator, str):
        raise ValueError("Rufzeichen und Locator müssen Text sein")  # noqa: TRY004 -- one error type for bad input
    call = call.strip().upper()
    locator = locator.strip().upper()
    if call and not (_CALL_RE.match(call) and re.search(r"[0-9]", call) and re.search(r"[A-Z]", call)):
        raise ValueError(f"ungültiges Rufzeichen '{call}' (3–10 Zeichen A–Z, 0–9, /, mit Ziffer und Buchstabe)")
    if locator and not _LOCATOR_RE.match(locator):
        raise ValueError(f"ungültiger Locator '{locator}' (4 oder 6 Stellen, z. B. JO43 oder JO43AB)")
    return {"call": call, "locator": locator}


def locator_center(locator: str) -> tuple[float, float] | None:
    """(latitude, longitude; north and east positive) of the middle of a validated locator; None if empty."""
    if not locator:
        return None
    lon = (ord(locator[0]) - 65) * 20 + int(locator[2]) * 2 - 180
    lat = (ord(locator[1]) - 65) * 10 + int(locator[3]) - 90
    if len(locator) == 6:
        return lat + (ord(locator[5]) - 65 + 0.5) / 24, lon + (ord(locator[4]) - 65 + 0.5) / 12
    return lat + 0.5, lon + 1.0


def from_environment() -> dict:
    """Start values; invalid ones are ignored rather than stopping the server."""
    try:
        return normalize(os.environ.get("WEB_TRX_STATION_CALL", ""), os.environ.get("WEB_TRX_STATION_LOCATOR", ""))
    except ValueError:
        return {"call": "", "locator": ""}
