"""JS8 message layer: text <-> 12-character frames. A line-by-line port of JS8Call v2.5.2's
JS8_Main/varicode.cpp (Varicode::*), JS8_jsc/jsc.cpp (JSC) and JS8_Mode/decodedtext.cpp (DecodedText), plus
the transmit-text normalisation of JS8_Main/TransmitTextEdit.cpp. docs/js8/SPEC.md section 2 has the frame
layouts with source lines; every table comes from pluto_tx/js8_tables.py.

The port keeps JS8Call's quirks on purpose (they are what goes over the air), with three deliberate
differences, all on inputs JS8Call's GUI never produces:
  - build_message_frames() raises ValueError where JS8Call would loop forever (text it cannot encode --
    the GUI filters those characters out first, see normalize_text()),
  - out-of-range values that would be undefined behaviour in C++ (alphabet .at() past the end) unpack to "",
  - without the JSC word lists (js8call/jsc.json, install-js8.sh) compressed data frames cannot be built
    and decode as "[JSC]".

Qt semantics that matter here: QRegularExpression::match() searches (unanchored unless the pattern has ^),
\\w/\\b/\\s are ASCII-only (re.ASCII), QMap::key(value) returns the first key in ascending order, and
QString/QVector::mid() treats any negative length as "to the end".
"""
import re
import threading

from . import js8_phy
from . import js8_tables as T

# --- constants (varicode.cpp:41-342, varicode.h) ----------------------------------------------------------

ALPHABET = T.ALPHABET_41            # base-41 checksum/number alphabet
ALPHABET72 = T.ALPHABET_72          # frame characters (only the first 64 are ever produced)
ALPHANUMERIC = T.ALPHANUMERIC       # callsign/grid alphabet (39 characters)
NBASECALL = T.NBASECALL
NBASEGRID = T.NBASEGRID
NUSERGRID = T.NUSERGRID
NMAXGRID = T.NMAXGRID

FRAME_HEARTBEAT = T.FRAME_TYPES["FrameHeartbeat"]
FRAME_COMPOUND = T.FRAME_TYPES["FrameCompound"]
FRAME_COMPOUND_DIRECTED = T.FRAME_TYPES["FrameCompoundDirected"]
FRAME_DIRECTED = T.FRAME_TYPES["FrameDirected"]
FRAME_DATA = T.FRAME_TYPES["FrameData"]
FRAME_DATA_COMPRESSED = T.FRAME_TYPES["FrameDataCompressed"]
FRAME_UNKNOWN = T.FRAME_TYPES["FrameUnknown"]

JS8_CALL = T.TRANSMISSION_TYPES["JS8Call"]
FLAG_FIRST = js8_phy.FLAG_FIRST
FLAG_LAST = js8_phy.FLAG_LAST
FLAG_DATA = js8_phy.FLAG_DATA

DIRECTED_CMDS = dict(T.DIRECTED_CMDS)
ALLOWED_CMDS = frozenset(T.ALLOWED_CMDS)
AUTOREPLY_CMDS = frozenset(T.AUTOREPLY_CMDS)
BUFFERED_CMDS = frozenset(T.BUFFERED_CMDS)
SNR_CMDS = frozenset(T.SNR_CMDS)
CHECKSUM_CMDS = dict(T.CHECKSUM_CMDS)
BASECALLS = dict(T.BASECALLS)
CQS = dict(T.CQS)
HBS = dict(T.HBS)
HUFF = dict(T.HUFF_TABLE)
EOT = T.EOT
FRAME_SIZE = 72

_FRAME_INDEX = {c: i for i, c in enumerate(ALPHABET72)}


def _qmap_key(mapping, value, default=""):
    """QMap::key(value, default): the smallest key (Qt order) mapped to value."""
    for k in sorted(mapping):
        if mapping[k] == value:
            return k
    return default


def _mid(seq, pos, length=-1):
    """QString/QVector::mid (Qt 6 QContainerImplHelper::mid)."""
    n = len(seq)
    if pos > n:
        return seq[:0]
    if pos < 0:
        if length < 0 or length + pos >= n:
            return seq[:]
        if length + pos <= 0:
            return seq[:0]
        length += pos
        pos = 0
    elif length < 0 or length > n - pos:
        length = n - pos
    return seq[pos:pos + length]


def _left(s, n):
    return s if n < 0 or n >= len(s) else s[:n]


def _is_space(c):
    return c.isspace()


def lstrip(s):
    for i, c in enumerate(s):
        if not _is_space(c):
            return s[i:]
    return ""


def rstrip(s):
    for i in range(len(s) - 1, -1, -1):
        if not _is_space(s[i]):
            return s[:i + 1]
    return ""


def _qre(pattern):
    """A Qt (PCRE2) pattern as a Python regex: named groups (?<n>...) -> (?P<n>...), ASCII classes."""
    return re.compile(re.sub(r"\(\?<(?![=!])", "(?P<", pattern), re.ASCII)


_R = T.REGEX
_GRID_RE = _qre(_R["grid_pattern"])
_BASE_CALLSIGN_RE = _qre(_R["base_callsign_pattern"])
_COMPOUND_CALLSIGN_RE = _qre(_R["compound_callsign_pattern"])
_COMPOUND_CALLSIGN_ANCHORED_RE = _qre("^" + _R["compound_callsign_pattern"])
_PACK_CALLSIGN_RE = _qre(_R["pack_callsign_pattern"])
_DIRECTED_RE = _qre("^" + _R["callsign_pattern"] + _R["optional_cmd_pattern"] + _R["optional_num_pattern"])
_HEARTBEAT_RE = _qre(_R["heartbeat_re"])
_COMPOUND_RE = _qre(r"^\s*[`]" + _R["callsign_pattern"] + "(?<extra>" + _R["optional_grid_pattern"]
                    + _R["optional_cmd_pattern"] + _R["optional_num_pattern"] + ")")
_CALL_CHARS_RE = re.compile(r"[0-9][A-Z]|[A-Z][0-9]")
_NOT_ALNUM50_RE = re.compile(r"[^A-Z0-9 /@]")


def _cap(m, name):
    return (m.group(name) or "") if m else ""


# --- JSC (JS8_jsc/jsc.cpp) --------------------------------------------------------------------------------

class _Jsc:
    _lock = threading.Lock()
    _data = None
    _loaded = False
    _cache = {}

    @classmethod
    def data(cls):
        with cls._lock:
            if not cls._loaded:
                cls._data = T.load_jsc()
                cls._loaded = True
                cls._cache = {}
            return cls._data


def jsc_available():
    return _Jsc.data() is not None


def _strncmp_eq(b, s, n):
    for k in range(n):
        cb = b[k] if k < len(b) else "\0"
        cs = s[k] if k < len(s) else "\0"
        if cb != cs:
            return False
        if cb == "\0":
            return True
    return True


def jsc_lookup(w):
    """JSC::lookup(QString): index into map of the entry that prefixes w, or None."""
    d = _Jsc.data()
    if d is None or not w:
        return None
    if w in _Jsc._cache:
        return _Jsc._cache[w]
    b = "".join(c if ord(c) < 256 else "?" for c in w)          # QString::toLatin1()
    result = None
    for s, size, index in d["prefix"]:
        if not s or b[0] != s[0]:
            continue
        if size == 1:
            result = d["list"][index][2]
            break
        for i in range(index, index + size):
            ls, lsize, lindex = d["list"][i]
            if _strncmp_eq(b, ls, lsize):
                result = lindex
                break
        break
    if result is not None:
        _Jsc._cache[w] = result
    return result


def _int_to_bits(value, expected=0):
    bits = []
    while value:
        bits.insert(0, value & 1)
        value >>= 1
    while len(bits) < expected:
        bits.insert(0, 0)
    return bits


def _bits_to_int(bits):
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


JSC_B, JSC_S = 4, 7
JSC_C = 2 ** JSC_B - JSC_S


def jsc_codeword(index, separate, bytesize=JSC_B, s=JSC_S, c=JSC_C):
    out = [_int_to_bits(((index % s) << 1) + int(separate), bytesize + 1)]
    x = index // s
    while x > 0:
        x -= 1
        out.insert(0, _int_to_bits((x % c) + s, bytesize))
        x //= c
    return [b for w in out for b in w]


def jsc_compress(text):
    """-> [(bits, chars)] (JSC::compress)."""
    d = _Jsc.data()
    if d is None:
        return []
    out = []
    words = text.split(" ")
    for i, w in enumerate(words):
        is_last_word = i == len(words) - 1
        is_space = False
        if not w and not is_last_word:
            w = " "
            is_space = True
        while w:
            index = jsc_lookup(w)
            if index is None:
                break
            size = d["map"][index][1]
            w = _mid(w, size)
            append_space = not w and not is_space and not is_last_word
            out.append((jsc_codeword(index, append_space), size + (1 if append_space else 0)))
    return out


def jsc_decompress(bits):
    d = _Jsc.data()
    if d is None:
        return "[JSC]"
    s, c = JSC_S, JSC_C
    base = [0, s]
    for k in range(2, 8):
        base.append(base[k - 1] + s * c ** (k - 1))
    size = T.JSC_SIZE
    words, separators = [], []
    i, count = 0, len(bits)
    while i < count:
        b = bits[i:i + 4]
        if len(b) != 4:
            break
        byte = _bits_to_int(b)
        words.append(byte)
        i += 4
        if byte < s:
            if count - i > 0 and bits[i]:
                separators.append(len(words) - 1)
            i += 1
    out = []
    start = 0
    while start < len(words):
        k = j = 0
        while start + k < len(words) and words[start + k] >= s:
            j = j * c + (words[start + k] - s)
            k += 1
        if j >= size:
            break
        if start + k >= len(words):
            break
        j = j * s + words[start + k] + base[k]
        if j >= size:
            break
        out.append(d["map"][j][0])
        if separators and separators[0] == start + k:
            out.append(" ")
            separators.pop(0)
        start += k + 1
    return "".join(out)


def extended_chars():
    """Varicode::extendedChars(): single-character JSC prefixes (the extra Latin-1 capitals)."""
    d = _Jsc.data()
    if d is None:
        return ""
    return "".join(s[:1] for s, size, _ in d["prefix"] if size == 1)


# --- small helpers (varicode.cpp:487-830) ------------------------------------------------------------------

def format_snr(snr):
    snr = int(snr)
    if snr < -60 or snr > 60:
        return ""
    return f"{snr:+03d}"


def _crc16_kermit(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def _crc32_bzip2(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc ^ 0xFFFFFFFF


def pack16bits(packed):
    packed &= 0xFFFF
    n = len(ALPHABET)
    a = packed // (n * n)
    b = (packed - a * n * n) // n
    return ALPHABET[a] + ALPHABET[b] + ALPHABET[packed % n]


def unpack16bits(value):
    n = len(ALPHABET)
    a, b, c = (ALPHABET.find(ch) for ch in value[:3])
    v = n * n * a + n * b + c
    return 0 if v > 0xFFFF else v & 0xFFFF


def pack32bits(packed):
    return pack16bits((packed >> 16) & 0xFFFF) + pack16bits(packed & 0xFFFF)


def checksum16(text):
    return pack16bits(_crc16_kermit(text.encode("utf-8"))).ljust(3)


def checksum16_valid(checksum, text):
    return pack16bits(_crc16_kermit(text.encode("utf-8"))) == checksum


def checksum32(text):
    return pack32bits(_crc32_bzip2(text.encode("utf-8"))).ljust(6)


def checksum32_valid(checksum, text):
    return pack32bits(_crc32_bzip2(text.encode("utf-8"))) == checksum


def pack72bits(bits):
    """72 bits (MSB first) -> 12 frame characters (Varicode::pack72bits)."""
    if len(bits) != FRAME_SIZE:
        raise ValueError("pack72bits needs 72 bits")
    return "".join(ALPHABET72[_bits_to_int(bits[i:i + 6])] for i in range(0, FRAME_SIZE, 6))


def unpack72bits(text):
    """12 frame characters -> 72 bits, as Varicode::unpack72bits + intToBits(value, 64) + rem."""
    value = 0
    for i in range(10):
        value |= (ALPHABET72.find(text[i]) & 0xFFFFFFFFFFFFFFFF) << (58 - 6 * i)
    rem_high = ALPHABET72.find(text[10]) & 0xFF
    value |= rem_high >> 2
    rem = (((rem_high & 3) << 6) | (ALPHABET72.find(text[11]) & 0xFF)) & 0xFF
    value &= 0xFFFFFFFFFFFFFFFF
    return _int_to_bits(value, 64)[-64:] + _int_to_bits(rem, 8)


def _unpack72_value(text):
    bits = unpack72bits(text)
    return bits[:64], _bits_to_int(bits[64:])


# --- callsigns and grids (varicode.cpp:830-1375) -----------------------------------------------------------

def pack_alphanumeric50(value):
    word = _NOT_ALNUM50_RE.sub("", value)
    if len(word) > 3 and word[3] != "/":
        word = word[:3] + " " + word[3:]
    if len(word) > 7 and word[7] != "/":
        word = word[:7] + " " + word[7:]
    word = word.ljust(11)
    idx = ALPHANUMERIC.find
    v = idx(word[0])
    for pos, ch in enumerate(word[1:11], start=1):
        if pos in (3, 7):
            v = v * 2 + (1 if ch == "/" else 0)
        else:
            v = v * 38 + idx(ch)
    return v


def unpack_alphanumeric50(packed):
    word = [""] * 11
    for pos in range(10, 0, -1):
        if pos in (3, 7):
            word[pos] = "/" if packed % 2 else " "
            packed //= 2
        else:
            word[pos] = ALPHANUMERIC[packed % 38]
            packed //= 38
    word[0] = ALPHANUMERIC[packed % 39]
    return "".join(word).replace(" ", "")


def pack_callsign(value):
    """-> (packed 28-bit value or 0, portable)."""
    portable = False
    callsign = value.upper().strip()
    if callsign in BASECALLS:
        return BASECALLS[callsign], portable
    if callsign.endswith("/P"):
        callsign = callsign[:-2]
        portable = True
    if callsign.startswith("3DA0"):
        callsign = "3D0" + callsign[4:]
    if callsign.startswith("3X") and len(callsign) > 2 and "A" <= callsign[2] <= "Z":
        callsign = "Q" + callsign[2:]
    slen = len(callsign)
    if slen < 2 or slen > 6:
        return 0, portable
    perms = [callsign]
    if slen == 2:
        perms.append(" " + callsign + "   ")
    if slen == 3:
        perms += [" " + callsign + "  ", callsign + "   "]
    if slen == 4:
        perms += [" " + callsign + " ", callsign + "  "]
    if slen == 5:
        perms += [" " + callsign, callsign + " "]
    matched = ""
    for p in perms:
        m = _PACK_CALLSIGN_RE.search(p)
        if m:
            matched = m.group(0)
    if len(matched) < 6:
        return 0, portable
    a = ALPHANUMERIC.find
    packed = a(matched[0])
    packed = 36 * packed + a(matched[1])
    packed = 10 * packed + a(matched[2])
    packed = 27 * packed + a(matched[3]) - 10
    packed = 27 * packed + a(matched[4]) - 10
    packed = 27 * packed + a(matched[5]) - 10
    return packed, portable


def unpack_callsign(value, portable):
    for key in sorted(BASECALLS):
        if BASECALLS[key] == value:
            return key
    word = [""] * 6
    for pos in (5, 4, 3):
        word[pos] = ALPHANUMERIC[value % 27 + 10]
        value //= 27
    word[2] = ALPHANUMERIC[value % 10]
    value //= 10
    word[1] = ALPHANUMERIC[value % 36]
    value //= 36
    if value >= len(ALPHANUMERIC):
        return ""
    word[0] = ALPHANUMERIC[value]
    callsign = "".join(word)
    if callsign.startswith("3D0"):
        callsign = "3DA0" + callsign[3:]
    if callsign.startswith("Q") and "A" <= callsign[1] <= "Z":
        callsign = "3X" + callsign[1:]
    if portable:
        callsign = callsign.strip() + "/P"
    return callsign.strip()


def deg2grid(dlong, dlat):
    if dlong < -180:
        dlong += 360
    if dlong > 180:
        dlong -= 360
    nlong = int(60.0 * (180.0 - dlong) / 5)
    n1 = nlong // 240
    n2 = (nlong - 240 * n1) // 24
    n3 = nlong - 240 * n1 - 24 * n2
    g = [chr(ord("A") + n1), "", chr(ord("0") + n2), "", chr(ord("a") + n3), ""]
    nlat = int(60.0 * (dlat + 90) / 2.5)
    n1 = nlat // 240
    n2 = (nlat - 240 * n1) // 24
    n3 = nlat - 240 * n1 - 24 * n2
    g[1], g[3], g[5] = chr(ord("A") + n1), chr(ord("0") + n2), chr(ord("a") + n3)
    return "".join(g)


def grid2deg(grid):
    g = grid if len(grid) >= 6 else _left(grid, 4) + "mm"
    g = _left(g, 4).upper() + g[-2:].lower()
    nlong = 180 - 20 * (ord(g[0]) - ord("A"))
    n20d = 2 * (ord(g[2]) - ord("0"))
    xminlong = 5 * (ord(g[4]) - ord("a") + 0.5)
    dlong = nlong - n20d - xminlong / 60.0
    nlat = -90 + 10 * (ord(g[1]) - ord("A")) + ord(g[3]) - ord("0")
    xminlat = 2.5 * (ord(g[5]) - ord("a") + 0.5)
    dlat = nlat + xminlat / 60.0
    return dlong, dlat


def pack_grid(value):
    grid = value.strip()
    if len(grid) < 4:
        return (1 << 15) - 1
    dlong, dlat = grid2deg(grid[:4])
    ilong = int(dlong)
    ilat = int(dlat + 90)
    return (((ilong + 180) // 2) * 180 + ilat) & 0xFFFF


def unpack_grid(value):
    if value > NBASEGRID:
        return ""
    dlat = value % 180 - 90
    dlong = value // 180 * 2 - 180 + 2
    return deg2grid(float(dlong), float(dlat))[:4]


def _qt_to_int(s):
    s = s.strip()
    if re.fullmatch(r"[+-]?\d+", s):
        return int(s), True
    return 0, False


def pack_num(num):
    """-> (0..62, ok) (Varicode::packNum)."""
    if not num:
        return 0, False
    v, ok = _qt_to_int(num)
    return max(-30, min(v, 31)) + 31, ok


def is_snr_command(cmd):
    return cmd in DIRECTED_CMDS and DIRECTED_CMDS[cmd] in SNR_CMDS


def is_command_allowed(cmd):
    return cmd in DIRECTED_CMDS and DIRECTED_CMDS[cmd] in ALLOWED_CMDS


def is_command_buffered(cmd):
    return cmd in DIRECTED_CMDS and (" " in cmd or DIRECTED_CMDS[cmd] in BUFFERED_CMDS)


def is_command_checksummed(cmd):
    if cmd not in DIRECTED_CMDS or DIRECTED_CMDS[cmd] not in CHECKSUM_CMDS:
        return 0
    return CHECKSUM_CMDS[DIRECTED_CMDS[cmd]]


def is_command_autoreply(cmd):
    return cmd in DIRECTED_CMDS and DIRECTED_CMDS[cmd] in AUTOREPLY_CMDS


def pack_cmd(cmd, num):
    """-> (value, packed_num) (Varicode::packCmd); cmd as quint8."""
    cmd &= 0xFF                                          # quint8, so -1 (HB/CQ) becomes 255: no key
    cmd_str = _qmap_key(DIRECTED_CMDS, cmd)
    if is_snr_command(cmd_str):
        value = ((1 << 1) | int(cmd_str == " HEARTBEAT SNR")) << 6
        return (value + (num & 0x3F)) & 0xFF, True
    return cmd & 0x7F, False


def unpack_cmd(value):
    """-> (cmd, num) (Varicode::unpackCmd)."""
    value &= 0xFF
    if value & 0x80:
        cmd = DIRECTED_CMDS[" HEARTBEAT SNR"] if value & 0x40 else DIRECTED_CMDS[" SNR"]
        return cmd, value & 0x3F
    return value & 0x7F, 0


def _is_valid_compound_callsign(callsign):
    if len(callsign) - callsign.count("/") > 9:
        return False
    if "/" in callsign:
        return callsign[:callsign.index("/")] not in BASECALLS
    if callsign.startswith("@"):
        return True
    return len(callsign) > 2 and bool(_CALL_CHARS_RE.search(callsign))


def is_valid_callsign(callsign):
    """-> (valid, is_compound) (Varicode::isValidCallsign)."""
    if callsign in BASECALLS:
        return True, False
    m = _BASE_CALLSIGN_RE.search(callsign)
    if m and len(m.group(0)) == len(callsign):
        return len(callsign) > 2 and bool(_CALL_CHARS_RE.search(callsign)), False
    m = _COMPOUND_CALLSIGN_ANCHORED_RE.search(callsign)
    if m and len(m.group(0)) == len(callsign):
        valid = _is_valid_compound_callsign(m.group(0))
        return valid, valid
    return False, False


def is_compound_callsign(callsign):
    if callsign in BASECALLS and not callsign.startswith("@"):
        return False
    m = _BASE_CALLSIGN_RE.search(callsign)
    if m and len(m.group(0)) == len(callsign):
        return False
    m = _COMPOUND_CALLSIGN_ANCHORED_RE.search(callsign)
    if not m or len(m.group(0)) != len(callsign):
        return False
    return _is_valid_compound_callsign(m.group(0))


def is_group_allowed(group):
    return group not in ("@APRSIS", "@JS8NET")


def parse_callsigns(text):
    out = []
    for m in _COMPOUND_CALLSIGN_RE.finditer(text):
        callsign = (m.group("callsign") or "").strip()
        if not is_valid_callsign(callsign)[0]:
            continue
        if _GRID_RE.search(callsign):
            continue
        out.append(callsign)
    return out


def starts_with_cq(text):
    return any(text.startswith(v) for v in CQS.values())


def starts_with_hb(text):
    return any(text.startswith(v) for v in HBS.values())


# --- frame packing (varicode.cpp:1376-2034) ----------------------------------------------------------------

def pack_compound_frame(callsign, frame_type, num, bits3):
    if frame_type in (FRAME_DATA, FRAME_DIRECTED):
        return ""
    packed_callsign = pack_alphanumeric50(callsign)
    if packed_callsign == 0:
        return ""
    num &= 0xFFFF
    packed_11 = (num & (((1 << 11) - 1) << 5)) >> 5
    packed_5 = num & 0x1F
    packed_8 = ((packed_5 << 3) | bits3) & 0xFF
    bits = _int_to_bits(frame_type, 3) + _int_to_bits(packed_callsign, 50) + _int_to_bits(packed_11, 11)
    value = _bits_to_int(bits) & 0xFFFFFFFFFFFFFFFF
    return pack72bits(_int_to_bits(value, 64)[-64:] + _int_to_bits(packed_8, 8))


def unpack_compound_frame(text):
    """-> (parts, type, num, bits3) or None."""
    if len(text) < 12 or " " in text:
        return None
    bits, packed_8 = _unpack72_value(text)
    packed_5, packed_3 = packed_8 >> 3, packed_8 & 7
    flag = _bits_to_int(bits[0:3])
    if flag in (FRAME_DATA, FRAME_DIRECTED):
        return None
    callsign = unpack_alphanumeric50(_bits_to_int(bits[3:53]))
    num = ((_bits_to_int(bits[53:64]) << 5) | packed_5) & 0xFFFF
    return [callsign, ""], flag, num, packed_3


def pack_heartbeat_message(text, callsign):
    """-> (frame, n)."""
    m = _HEARTBEAT_RE.search(text)
    if not m:
        return "", 0
    extra = _cap(m, "grid")
    htype = _cap(m, "type")
    is_alt = htype.startswith("CQ")
    if not callsign:
        return "", 0
    packed_extra = NMAXGRID
    if len(extra) == 4 and _GRID_RE.search(extra):
        packed_extra = pack_grid(extra)
    cq_number = _qmap_key(HBS, htype, 0)
    if is_alt:
        packed_extra |= 1 << 15
        cq_number = _qmap_key(CQS, htype, 0)
    frame = pack_compound_frame(callsign, FRAME_HEARTBEAT, packed_extra, cq_number)
    if not frame:
        return "", 0
    return frame, len(m.group(0))


def unpack_heartbeat_message(text):
    """-> (parts, type, is_alt, bits3) or None."""
    r = unpack_compound_frame(text)
    if r is None or r[1] != FRAME_HEARTBEAT:
        return None
    parts, ftype, num, bits3 = r
    parts.append(unpack_grid(num & ((1 << 15) - 1)))
    return parts, ftype, bool(num & (1 << 15)), bits3


def pack_compound_message(text):
    """-> (frame, n)."""
    m = _COMPOUND_RE.search(text)
    if not m:
        return "", 0
    callsign = _cap(m, "callsign")
    grid = _cap(m, "grid")
    cmd = _cap(m, "cmd")
    num = _cap(m, "num").strip()
    if not callsign:
        return "", 0
    ftype = FRAME_COMPOUND
    extra = NMAXGRID
    if cmd and cmd in DIRECTED_CMDS and is_command_allowed(cmd):
        inum, _ = pack_num(num)
        value, _ = pack_cmd(DIRECTED_CMDS[cmd], inum)
        extra = (NUSERGRID + value) & 0xFFFF
        ftype = FRAME_COMPOUND_DIRECTED
    elif grid:
        extra = pack_grid(grid)
    return pack_compound_frame(callsign, ftype, extra, 0), len(m.group(0))


def unpack_compound_message(text):
    """-> (parts, type, bits3) or None."""
    r = unpack_compound_frame(text)
    if r is None or r[1] not in (FRAME_COMPOUND, FRAME_COMPOUND_DIRECTED):
        return None
    parts, ftype, extra, bits3 = r
    if extra <= NBASEGRID:
        parts.append(" " + unpack_grid(extra))
    elif NUSERGRID <= extra < NMAXGRID:
        cmd, num = unpack_cmd((extra - NUSERGRID) & 0xFF)
        cmd_str = _qmap_key(DIRECTED_CMDS, cmd)
        parts.append(cmd_str)
        if is_snr_command(cmd_str):
            parts.append(format_snr(num - 31))
    return parts, ftype, bits3


def pack_directed_message(text, mycall):
    """-> dict(frame, to, to_compound, cmd, num, n); frame "" if text is not a directed message."""
    out = dict(frame="", to="", to_compound=False, cmd="", num="", n=0)
    m = _DIRECTED_RE.search(text)
    if not m:
        return out
    frm = mycall
    if is_compound_callsign(frm):
        frm = "<....>"
    to = _cap(m, "callsign")
    cmd = _cap(m, "cmd")
    num = _cap(m, "num")
    if not cmd:
        return out
    valid, to_compound = is_valid_callsign(to)
    if not (to != mycall and valid):
        return out
    out["to"], out["to_compound"] = to, to_compound
    if to_compound:
        to = "<....>"
    if not is_command_allowed(cmd) and not is_command_allowed(cmd.strip()):
        return out
    inum, num_ok = pack_num(num.strip())
    if num_ok:
        out["num"] = num
    packed_from, portable_from = pack_callsign(frm)
    packed_to, portable_to = pack_callsign(to)
    if packed_from == 0 or packed_to == 0:
        return out
    cmd_out, packed_cmd = "", 0
    if cmd in DIRECTED_CMDS:
        cmd_out, packed_cmd = cmd, DIRECTED_CMDS[cmd] & 0xFF
    if cmd.strip() in DIRECTED_CMDS:
        cmd_out, packed_cmd = cmd.strip(), DIRECTED_CMDS[cmd.strip()] & 0xFF
    packed_extra = ((int(portable_from) << 7) + (int(portable_to) << 6) + inum) & 0xFF
    bits = (_int_to_bits(FRAME_DIRECTED, 3) + _int_to_bits(packed_from, 28) + _int_to_bits(packed_to, 28)
            + _int_to_bits(packed_cmd % 32, 5))
    out["cmd"] = cmd_out
    out["n"] = len(m.group(0))
    out["frame"] = pack72bits(_int_to_bits(_bits_to_int(bits), 64)[-64:] + _int_to_bits(packed_extra, 8))
    return out


def unpack_directed_message(text):
    """-> (parts, type) or None."""
    if len(text) < 12 or " " in text:
        return None
    bits, extra = _unpack72_value(text)
    flag = _bits_to_int(bits[0:3])
    if flag != FRAME_DIRECTED:
        return None
    packed_from = _bits_to_int(bits[3:31])
    packed_to = _bits_to_int(bits[31:59])
    packed_cmd = _bits_to_int(bits[59:64])
    portable_from = (extra >> 7) & 1 == 1
    portable_to = (extra >> 6) & 1 == 1
    extra %= 64
    cmd = _qmap_key(DIRECTED_CMDS, packed_cmd % 32)
    parts = [unpack_callsign(packed_from, portable_from), unpack_callsign(packed_to, portable_to), cmd]
    if extra != 0:
        parts.append(format_snr(extra - 31) if is_snr_command(cmd) else str(extra - 31))
    return parts, flag


def _huff_valid_chars():
    return set(HUFF)


def huff_encode(text):
    keys = sorted(HUFF, key=lambda k: (-len(k), [-ord(c) for c in k]))
    out = []
    i = 0
    while i < len(text):
        for ch in keys:
            if text.startswith(ch, i):
                out.append((len(ch), [int(b) for b in HUFF[ch]]))
                i += len(ch)
                break
        else:
            i += 1
    return out


def huff_decode(bits):
    s = "".join("1" if b else "0" for b in bits)
    text = ""
    while s:
        found = False
        for key in sorted(HUFF):
            code = HUFF[key]
            if s.startswith(code):
                if key == EOT:
                    text += " "
                    found = False
                    break
                text += key
                s = s[len(code):]
                found = True
        if not found:
            break
    return text


def _pad_frame(frame_bits):
    pad = FRAME_SIZE - len(frame_bits)
    return frame_bits + ([0] + [1] * (pad - 1) if pad > 0 else [])


def pack_huff_message(text, prefix):
    frame_bits = list(prefix)
    valid = _huff_valid_chars()
    for ch in text:
        if ch.upper() not in valid:
            return "", 0
    n = 0
    for chars, code in huff_encode(text):
        if len(frame_bits) + len(code) < FRAME_SIZE:
            frame_bits += code
            n += chars
            continue
        break
    return pack72bits(_pad_frame(frame_bits)), n


def pack_compressed_message(text, prefix):
    frame_bits = list(prefix)
    n = 0
    for code, chars in jsc_compress(text):
        if len(frame_bits) + len(code) < FRAME_SIZE:
            frame_bits += code
            n += chars
            continue
        break
    return pack72bits(_pad_frame(frame_bits)), n


def pack_data_message(text):
    """NORMAL speed data frame: Huffman or JSC, whichever carries more characters (-> frame, n)."""
    huff_frame, huff_n = pack_huff_message(text, [1, 0])
    comp_frame, comp_n = pack_compressed_message(text, [1, 1])
    if huff_n > comp_n:
        return huff_frame, huff_n
    return comp_frame, comp_n


def pack_fast_data_message(text):
    """FAST/TURBO/SLOW data frame: JSC only, flagged by FLAG_DATA (-> frame, n)."""
    return pack_compressed_message(text, [])


def _last_index_of_zero(bits):
    for i in range(len(bits) - 1, -1, -1):
        if not bits[i]:
            return i
    return -1


def unpack_data_message(text):
    if len(text) < 12 or " " in text:
        return ""
    bits = unpack72bits(text)
    if not bits[0]:
        return ""
    bits = bits[1:]
    compressed = bits[0]
    n = _last_index_of_zero(bits)
    bits = _mid(bits, 1, n - 1)
    return jsc_decompress(bits) if compressed else huff_decode(bits)


def unpack_fast_data_message(text):
    if len(text) < 12 or " " in text:
        return ""
    bits = unpack72bits(text)
    n = _last_index_of_zero(bits)
    return jsc_decompress(_mid(bits, 0, n))


# --- text -> frames (varicode.cpp:2037-2331) ---------------------------------------------------------------

def build_message_frames(mycall, mygrid, selected_call, text, force_identify=True, force_data=False,
                         submode=js8_phy.NORMAL):
    """Varicode::buildMessageFrames -> (frames, info): frames as [(frame, bits)] with the builder's flags
    (see transmit_flags()), info = dict(dir_to, dir_cmd, dir_num). Raises ValueError on text that
    JS8Call could not encode (it would loop forever there)."""
    mycall_compound = is_compound_callsign(mycall)
    info = dict(dir_to="", dir_cmd="", dir_num="")
    all_frames = []
    for line in [text]:
        line_frames = []
        has_directed = False
        has_data = False
        if force_data:
            force_identify = False
            has_data = True
        if line.startswith(mycall + ":") or line.startswith(mycall + " "):
            line = lstrip(line[len(mycall) + 1:])
        if selected_call and not line.startswith(selected_call) and not line.startswith("`") and not force_data:
            base = line.startswith("@ALLCALL") or starts_with_cq(line) or starts_with_hb(line)
            calls = parse_callsigns(line)
            standard = bool(calls) and line.startswith(calls[0]) and len(calls[0]) > 3
            if not (base or standard):
                sep = "" if line.startswith(" ") else " "
                line = f"{selected_call}{sep}{line}"
        while len(line) > 0:
            bcn_frame, l_ = pack_heartbeat_message(line, mycall)
            cmp_frame, o_ = pack_compound_message(line)
            d = pack_directed_message(line, mycall)
            dir_frame, n_ = d["frame"], d["n"]
            if not dir_frame:
                n_ = 0
            likely_data = not line_frames and not selected_call and not d["to"] and l_ == 0 and o_ == 0
            if force_identify and likely_data and mycall not in line:
                line = f"{mycall}: {line}"
            if submode == js8_phy.NORMAL:
                dat_frame, m_ = pack_data_message(line)
                fast = False
            else:
                dat_frame, m_ = pack_fast_data_message(line)
                fast = True
            if not has_directed and not has_data and l_ > 0:
                line_frames.append([bcn_frame, JS8_CALL])
                line = _mid(line, l_)
            elif not has_directed and not has_data and o_ > 0:
                line_frames.append([cmp_frame, JS8_CALL])
                line = _mid(line, o_)
            elif not has_directed and not has_data and n_ > 0:
                has_directed = True
                use_standard = True
                if mycall_compound or d["to_compound"]:
                    de, _ = pack_compound_message(f"`{mycall} {mygrid}")
                    if de:
                        line_frames.append([de, JS8_CALL])
                    dc, _ = pack_compound_message(f"`{d['to']}{d['cmd']}{d['num']}")
                    if dc:
                        line_frames.append([dc, JS8_CALL])
                    use_standard = False
                if use_standard:
                    line_frames.append([dir_frame, JS8_CALL])
                line = _mid(line, n_)
                if is_command_buffered(d["cmd"]) and line:
                    line = lstrip(line)
                    size = is_command_checksummed(d["cmd"])
                    if size == 32:
                        line = line + " " + checksum32(line)
                    elif size == 16:
                        line = line + " " + checksum16(line)
                info = dict(dir_to=d["to"], dir_cmd=d["cmd"], dir_num=d["num"])
            elif m_ > 0:
                has_data = True
                line_frames.append([dat_frame, FLAG_DATA if fast else JS8_CALL])
                line = _mid(line, m_)
            else:
                if not jsc_available() and submode != js8_phy.NORMAL:
                    raise ValueError("JS8 free text in this speed needs the JSC word lists "
                                     "(js8call/jsc.json, run install-js8.sh)")
                raise ValueError(f"JS8 cannot encode this text: {line!r}")
        if line_frames:
            line_frames[0][1] |= FLAG_FIRST
            line_frames[-1][1] |= FLAG_LAST
        all_frames += [tuple(f) for f in line_frames]
    return all_frames, info


def transmit_flags(frames):
    """The flags as JS8Call actually sends them (MainWindow::prepareNextMessageFrame(), mainwindow.cpp:6199):
    FIRST only on the first frame, LAST forced on the last."""
    out = []
    for i, (frame, bits) in enumerate(frames):
        if i > 0:
            bits &= ~FLAG_FIRST
        if i == len(frames) - 1:
            bits |= FLAG_LAST
        out.append((frame, bits))
    return out


# --- frames -> text (decodedtext.cpp) ------------------------------------------------------------------------

def _build_compound(parts):
    return "/".join(p for p in parts[:2] if p)


def decode_frame(frame, bits, submode=js8_phy.NORMAL):
    """DecodedText(frame, bits, submode) -> dict(message, frame_type, is_heartbeat, is_alt, compound,
    directed, extra). message is exactly what JS8Call shows."""
    r = dict(message=frame, frame_type=FRAME_UNKNOWN, is_heartbeat=False, is_alt=False, compound="",
             directed=[], extra="")
    m = frame.strip()
    if len(m) < 12 or " " in m:
        return r
    is_data = (bits & FLAG_DATA) == FLAG_DATA
    if is_data:                                                      # tryUnpackFastData
        data = unpack_fast_data_message(m)
        if data:
            r.update(message=data, frame_type=FRAME_DATA)
            return r
    else:                                                            # tryUnpackData
        data = unpack_data_message(m)
        if data:
            r.update(message=data, frame_type=FRAME_DATA)
            return r
    if not is_data:                                                  # tryUnpackHeartbeat
        hb = unpack_heartbeat_message(m)
        if hb and len(hb[0]) >= 2:
            parts, ftype, is_alt, bits3 = hb
            compound = _build_compound(parts)
            extra = parts[2] if len(parts) > 2 else ""
            msg = compound + ": "
            if is_alt:
                msg += "@ALLCALL " + CQS.get(bits3, "")
            else:
                s = HBS.get(bits3, "")
                msg += "@HB " + ("HEARTBEAT" if s == "HB" else s)
            msg += " " + extra + " "
            r.update(message=msg, frame_type=ftype, is_heartbeat=True, is_alt=is_alt, extra=extra,
                     compound=compound)
            return r
    cp = unpack_compound_message(m)                                  # tryUnpackCompound
    if cp and len(cp[0]) >= 2 and not is_data:
        parts, ftype, _ = cp
        extra = " ".join(parts[2:])
        compound = _build_compound(parts)
        msg = r["message"]
        directed = []
        if ftype == FRAME_COMPOUND:
            msg = compound + ": "
        elif ftype == FRAME_COMPOUND_DIRECTED:
            msg = compound + extra + " "
            directed = ["<....>", compound] + parts[2:]
        r.update(message=msg, frame_type=ftype, extra=extra, compound=compound, directed=directed)
        return r
    if not is_data:                                                  # tryUnpackDirected
        dm = unpack_directed_message(m)
        if dm:
            parts, ftype = dm
            if len(parts) in (3, 4):
                msg = parts[0] + ": " + parts[1] + " ".join(parts[2:]) + " "
            else:
                msg = "".join(parts)
            r.update(message=msg, frame_type=ftype, directed=parts)
            return r
    return r


# --- input normalisation (TransmitTextEdit.cpp:127, 258-285) -------------------------------------------------

def normalize_text(text):
    """What JS8Call's transmit box turns typed text into: upper case, then only Latin-1 32..127, 0x10, 0x1A
    and the extended JSC capitals survive."""
    ext = extended_chars()
    out = []
    for c in text.upper():
        code = ord(c)
        if code in (0x10, 0x1A) or 32 <= code <= 127 or (ext and c.upper() in ext):
            out.append(c)
    return "".join(out)


# --- the operations pluto-tx offers (docs/JS8_PLAN.md 3, decision 1) ---------------------------------------

MESSAGE_KINDS = (
    ("cq", "CQ"),                          # CQ CQ CQ GRID
    ("hb", "Heartbeat"),                   # CALL: HEARTBEAT GRID
    ("allcall", "@ALLCALL text"),
    ("directed", "Text to a station"),
    ("snr_query", "SNR?"),
    ("snr_reply", "SNR report"),
    ("ack", "ACK"),
    ("free", "Free text"),
)


def compose(kind, my_call, my_grid="", to="", text="", snr_db=0):
    """-> the text JS8Call's GUI would hand to buildMessageFrames for this action (normalised)."""
    my = my_call.strip().upper()
    grid = my_grid.strip().upper()[:4]
    to = to.strip().upper()
    body = normalize_text(text).strip()
    if kind == "cq":
        return f"CQ CQ CQ {grid}".strip()
    if kind == "hb":
        return f"{my}: HEARTBEAT {grid}".strip()
    if kind == "allcall":
        return f"@ALLCALL {body}".strip()
    if kind == "free":
        return body
    if not to:
        return ""
    if kind == "directed":
        return f"{to} {body}".strip()
    if kind == "snr_query":
        return f"{to} SNR?"
    if kind == "snr_reply":
        return f"{to} SNR {format_snr(snr_db)}"
    if kind == "ack":
        return f"{to} ACK"
    raise ValueError(f"unknown JS8 message kind {kind!r}")


def build_frames(my_call, my_grid, text, submode=js8_phy.NORMAL, selected_call="", force_identify=True):
    """Text -> [(frame, flags)] exactly as JS8Call transmits them (builder + GUI flag rule)."""
    my = my_call.strip().upper()
    if not is_valid_callsign(my)[0]:
        raise ValueError(f"not a valid callsign: {my_call!r}")
    frames, _ = build_message_frames(my, my_grid.strip().upper()[:4], selected_call.strip().upper(),
                                     text, force_identify, False, submode)
    if not frames:
        raise ValueError("nothing to send")
    return transmit_flags(frames)


def frames_text(frames, submode=js8_phy.NORMAL):
    """What a JS8Call receiver shows for a whole transmission: the decoded frames joined."""
    return "".join(decode_frame(f, b, submode)["message"] for f, b in frames)
