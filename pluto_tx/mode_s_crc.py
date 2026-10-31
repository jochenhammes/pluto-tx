"""The Mode-S CRC-24 (ICAO Annex 10), used by both ADS-B on 1090 MHz and ADS-L on 868 MHz. Running it over a message
plus its own three parity bytes gives 0."""

_POLY = 0xFFF409  # the generator without its x^24 term


def _table_entry(byte: int) -> int:
    crc = byte << 16
    for _ in range(8):
        crc = ((crc << 1) ^ _POLY if crc & 0x800000 else crc << 1) & 0xFFFFFF
    return crc


_TABLE = tuple(_table_entry(b) for b in range(256))


def crc24(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ _TABLE[(crc >> 16) ^ byte]
    return crc
