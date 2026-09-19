"""Oscillator frequency correction in ppm, shared by the TX and RX device backends.

Convention (the one rtl_test / Kalibrate / gqrx use): `ppm` is the DEVICE's
error. A positive value means the device runs too HIGH -- signals appear too
high on receive, and it transmits too high. The app compensates by tuning the
hardware LO lower: hardware = wanted / (1 + ppm * 1e-6). Being a ratio, a known
error scales with the tuned frequency automatically (unlike a fixed Hz offset).

Example: a receiver shows a 432.150 MHz carrier at 432.125 MHz -> the device is
about 58 ppm LOW -> enter -58.
"""

PPM_RANGE = (-200.0, 200.0)


def hardware_frequency(wanted_hz: float, ppm: float) -> float:
    """LO frequency to program so the device really tunes `wanted_hz`."""
    return wanted_hz / (1.0 + ppm * 1e-6)
