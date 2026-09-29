#!/usr/bin/env python3
"""Extracts the JS8 protocol tables from a pinned JS8Call source tree (docs/js8/SPEC.md).

    tools/js8_extract_tables.py --src js8call [--out pluto_tx/js8_tables.py] [--jsc-out js8call/jsc.json]

Writes pluto_tx/js8_tables.py (small tables, checked in, GPLv3 like JS8Call) and, with --jsc-out, the
JSC word lists (> 1 MB, loaded at runtime, not checked in). Deterministic: the output depends only on
the input files -- no timestamps, stable ordering; a second run gives byte-identical files.

Nothing is typed in by hand here: every value comes out of the C/C++ source through a small C
tokenizer (comments, string literals with C escapes incl. greedy \\x, raw strings, adjacent-literal
concatenation) and is checked against the expected shape; any surprise raises instead of guessing."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

PINNED_TAG = "v2.5.2"
PINNED_COMMIT = "f0f0d01b357c8eb8786aee687e8b5c787c787159"
PINNED_REPO = "https://github.com/JS8Call-improved/JS8Call-improved.git"

FILES = {
    "commons": "JS8_Include/commons.h",
    "js8h": "JS8_Mode/JS8.h",
    "js8cpp": "JS8_Mode/JS8.cpp",
    "submode": "JS8_Mode/JS8Submode.cpp",
    "varicode_h": "JS8_Main/varicode.h",
    "varicode": "JS8_Main/varicode.cpp",
    "jsc_h": "JS8_jsc/jsc.h",
    "jsc_map": "JS8_jsc/jsc_map.cpp",
    "jsc_list": "JS8_jsc/jsc_list.cpp",
}


class ExtractError(RuntimeError):
    pass


# --- C tokenizer -----------------------------------------------------------------------------------

_SIMPLE_ESC = {"n": 10, "t": 9, "r": 13, "0": 0, "a": 7, "b": 8, "f": 12, "v": 11,
               "\\": 92, "'": 39, '"': 34, "?": 63}


def _c_string(src, i):
    """src[i] == '"'. Returns (bytes, index after closing quote). C semantics: \\x takes all hex
    digits that follow, octal up to 3 digits."""
    out = bytearray()
    i += 1
    while True:
        c = src[i]
        if c == '"':
            return bytes(out), i + 1
        if c == "\n":
            raise ExtractError("newline in string literal")
        if c != "\\":
            out += c.encode("latin-1")
            i += 1
            continue
        e = src[i + 1]
        if e == "x":
            j = i + 2
            while j < len(src) and src[j] in "0123456789abcdefABCDEF":
                j += 1
            if j == i + 2:
                raise ExtractError("empty \\x escape")
            v = int(src[i + 2:j], 16)
            if v > 255:
                raise ExtractError(f"\\x escape out of range: {src[i:j]}")
            out.append(v)
            i = j
        elif e in "01234567":
            j = i + 1
            while j < len(src) and j < i + 4 and src[j] in "01234567":
                j += 1
            out.append(int(src[i + 1:j], 8) & 0xFF)
            i = j
        elif e in _SIMPLE_ESC:
            out.append(_SIMPLE_ESC[e])
            i += 2
        else:
            raise ExtractError(f"unknown escape \\{e}")


_NUM_RE = re.compile(r"0[xX][0-9a-fA-F]+|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_ID_RE = re.compile(r"[A-Za-z_]\w*")


def tokenize(src):
    """-> list of (kind, value): 'str' (bytes, adjacent literals joined), 'num' (int), 'id' (str),
    'op' (str)."""
    toks = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
        elif src.startswith("//", i):
            i = src.find("\n", i)
            i = n if i < 0 else i
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise ExtractError("unterminated comment")
            i = j + 2
        elif c == "#":                                      # preprocessor line: keep as one token
            j = src.find("\n", i)
            j = n if j < 0 else j
            toks.append(("pp", src[i:j]))
            i = j
        elif c == "R" and src.startswith('R"', i):          # raw string R"delim(...)delim"
            k = src.index("(", i)
            delim = src[i + 2:k]
            end = src.index(")" + delim + '"', k)
            val = src[k + 1:end].encode("latin-1")
            i = end + len(delim) + 2
            toks.append(("str", val))
        elif c == '"':
            val, i = _c_string(src, i)
            toks.append(("str", val))
        elif c == "'":
            j = src.index("'", i + 1 if src[i + 1] != "\\" else i + 3)
            lit = src[i:j + 1]
            val, _ = _c_string('"' + lit[1:-1].replace('"', '\\"') + '"', 0)
            toks.append(("num", val[0]))
            i = j + 1
        elif c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _NUM_RE.match(src, i)
            s = m.group(0)
            if s[:2] in ("0x", "0X"):
                v = int(s, 16)
            elif any(ch in s for ch in ".eE"):
                v = float(s)
            else:
                v = int(s)
            toks.append(("num", v))
            i += len(s)
            while i < n and src[i] in "uUlLfF":
                i += 1
        elif c.isalpha() or c == "_":
            m = _ID_RE.match(src, i)
            toks.append(("id", m.group(0)))
            i += len(m.group(0))
        else:
            for op in ("::", "<<", ">>", "->", "==", "!=", "<=", ">=", "&&", "||"):
                if src.startswith(op, i):
                    toks.append(("op", op))
                    i += len(op)
                    break
            else:
                toks.append(("op", c))
                i += 1
    # join adjacent string literals (C concatenation)
    out = []
    for t in toks:
        if t[0] == "str" and out and out[-1][0] == "str":
            out[-1] = ("str", out[-1][1] + t[1])
        else:
            out.append(t)
    return out


def find_seq(toks, pattern, start=0):
    """Index just after the first occurrence of the token-value sequence `pattern`."""
    m = len(pattern)
    for i in range(start, len(toks) - m + 1):
        if all(toks[i + k][1] == pattern[k] for k in range(m)):
            return i + m
    raise ExtractError(f"not found: {' '.join(map(str, pattern))}")


def brace_block(toks, i):
    """toks[i] must be '{'; returns (nested list, index after the matching '}'). Each element is a
    list of tokens (comma separated) or a nested list for inner braces."""
    if toks[i] != ("op", "{"):
        raise ExtractError(f"expected '{{' at token {i}, got {toks[i]}")
    items, cur = [], []
    i += 1
    while True:
        t = toks[i]
        if t == ("op", "{"):
            sub, i = brace_block(toks, i)
            cur.append(("block", sub))
            continue
        if t == ("op", "}"):
            if cur:
                items.append(cur)
            return items, i + 1
        if t == ("op", ","):
            items.append(cur)
            cur = []
        else:
            cur.append(t)
        i += 1


def eval_expr(tokens, names):
    parts = []
    for kind, v in tokens:
        if kind == "num":
            parts.append(repr(v))
        elif kind == "id":
            if v not in names:
                raise ExtractError(f"unknown name in expression: {v}")
            parts.append(repr(names[v]))
        elif kind == "op" and v in ("+", "-", "*", "/", "(", ")", "<<", ">>", "|", "&"):
            parts.append("//" if v == "/" else v)
        else:
            raise ExtractError(f"unexpected token in expression: {kind} {v!r}")
    return eval(" ".join(parts), {"__builtins__": {}}, {})   # noqa: S307 -- ints/operators only


def one(item, kind):
    toks = [t for t in item if t[0] != "pp"]
    if len(toks) != 1 or toks[0][0] != kind:
        raise ExtractError(f"expected one {kind}, got {item}")
    return toks[0][1]


def ints_in(item_list):
    """Flatten nested brace lists into the integers they contain (unary minus aware)."""
    out = []

    def walk(items):
        for it in items:
            neg = False
            for t in it:
                if t[0] == "block":
                    walk(t[1])
                elif t == ("op", "-"):
                    neg = True
                elif t[0] == "num":
                    out.append(-t[1] if neg else t[1])
                    neg = False
    walk(item_list)
    return out


def latin1(b):
    return b.decode("latin-1")


# --- extraction ------------------------------------------------------------------------------------

def read(src_dir, key):
    with open(os.path.join(src_dir, FILES[key]), encoding="latin-1") as f:
        return f.read()


def extract(src_dir):
    t = {}
    sha = {}
    for key, rel in FILES.items():
        with open(os.path.join(src_dir, rel), "rb") as f:
            sha[rel] = hashlib.sha256(f.read()).hexdigest()

    # commons.h #defines (plain integers only)
    defines = {}
    for m in re.finditer(r"^#define\s+(JS8\w*)\s+(\d+)\b", read(src_dir, "commons"), re.M):
        defines[m.group(1)] = int(m.group(2))
    for need in ("JS8_NUM_SYMBOLS", "JS8_RX_SAMPLE_RATE", "JS8A_SYMBOL_SAMPLES"):
        if need not in defines:
            raise ExtractError(f"missing #define {need}")
    t["NUM_SYMBOLS"] = defines["JS8_NUM_SYMBOLS"]
    t["RX_SAMPLE_RATE"] = defines["JS8_RX_SAMPLE_RATE"]

    # varicode.h enums
    vh = tokenize(read(src_dir, "varicode_h"))
    enums = {}
    for name in ("SubmodeType", "TransmissionType", "FrameType"):
        items, _ = brace_block(vh, find_seq(vh, ["enum", name]))
        d = {}
        for it in items:
            if not it:
                continue
            ident = it[0][1]
            d[ident] = eval_expr(it[2:], {})
        enums[name] = d
    t["SUBMODE_IDS"] = enums["SubmodeType"]
    t["TRANSMISSION_TYPES"] = enums["TransmissionType"]
    t["FRAME_TYPES"] = enums["FrameType"]

    # Costas arrays (JS8.h): ORIGINAL then MODIFIED, 3 x 7 each
    jh = tokenize(read(src_dir, "js8h"))
    i = find_seq(jh, ["constexpr", "auto", "COSTAS", "="])
    j = find_seq(jh, [";"], i)
    nums = [v for k, v in jh[i:j] if k == "num"]
    enum_order = [one(it, "id") for it in brace_block(jh, find_seq(jh, ["enum", "class", "Type"]))[0]]
    if len(nums) != 42 or enum_order != ["ORIGINAL", "MODIFIED"]:
        raise ExtractError(f"Costas: {len(nums)} values, enum {enum_order}")
    t["COSTAS"] = {name: tuple(tuple(nums[k * 21 + r * 7:k * 21 + r * 7 + 7]) for r in range(3))
                   for k, name in enumerate(enum_order)}

    # JS8.cpp: channel alphabet, CRC, parity matrix, LDPC graph, code constants
    jc = tokenize(read(src_dir, "js8cpp"))
    i = find_seq(jc, ["constexpr", "std", "::", "string_view", "alphabet", "="])
    t["PHY_ALPHABET"] = latin1(jc[i][1])
    if len(t["PHY_ALPHABET"]) != 64:
        raise ExtractError("PHY alphabet is not 64 characters")
    i = find_seq(jc, ["boost", "::", "augmented_crc", "<"])
    width, poly = jc[i][1], jc[i + 2][1]
    k = find_seq(jc, ["^"], i)
    t["CRC12"] = {"width": width, "poly": poly, "xor_out": jc[k][1], "augmented": True}
    for name in ("N", "K", "KK", "ND", "NS", "BP_MAX_ROWS", "BP_MAX_CHECKS"):
        i = find_seq(jc, ["constexpr", "int", name, "="])
        t.setdefault("LDPC", {})[name] = jc[i][1] if jc[i][0] == "num" else None
    t["LDPC"]["N"], t["LDPC"]["K"] = 174, 87
    for name, want in (("N", 174), ("K", 87)):
        i = find_seq(jc, ["constexpr", "int", name, "="])
        if jc[i] != ("num", want):
            raise ExtractError(f"LDPC {name} != {want}")
    i = find_seq(jc, ["std", "::", "string_view", ",", "Rows", ">", "Data", "="])
    items, _ = brace_block(jc, i)
    rows = [latin1(one(it, "str")) for it in items]
    if len(rows) != 87 or any(len(r) != 22 or not re.fullmatch(r"[0-9a-f]+", r) for r in rows):
        raise ExtractError("parity matrix shape")
    t["PARITY_HEX"] = tuple(rows)
    i = find_seq(jc, ["Mn", "="])
    items, _ = brace_block(jc, i)
    mn = ints_in(items)
    if len(mn) != 174 * 3:
        raise ExtractError(f"Mn has {len(mn)} values")
    t["LDPC_MN"] = tuple(tuple(mn[k * 3:k * 3 + 3]) for k in range(174))
    i = find_seq(jc, ["Nm", "="])
    items, _ = brace_block(jc, i)
    nm = ints_in(items)
    if len(nm) != 87 * 8:
        raise ExtractError(f"Nm has {len(nm)} values")
    t["LDPC_NM"] = tuple(tuple(nm[k * 8 + 1:k * 8 + 1 + nm[k * 8]]) for k in range(87))

    # JS8Submode.cpp: per-speed data (constructor order: name, symbol samples, start delay ms,
    # period s, costas type, rx SNR threshold[, rx threshold = 10])
    sm = tokenize(read(src_dir, "submode"))
    i = find_seq(sm, ["class", "Data"])
    i = find_seq(sm, ["int", "const", "rxThreshold", "="], i)
    default_rx_threshold = sm[i][1]
    names = dict(defines)
    names.update({"ORIGINAL": "ORIGINAL", "MODIFIED": "MODIFIED"})
    data = {}
    for obj in ("Normal", "Fast", "Turbo", "Slow", "Ultra"):
        items, _ = brace_block(sm, find_seq(sm, ["constexpr", "Data", obj, "="]))
        vals = []
        for it in items:
            if it[0][0] == "str":
                vals.append(latin1(it[0][1]))
            elif it[-1][1] in ("ORIGINAL", "MODIFIED"):
                vals.append(it[-1][1])
            else:
                vals.append(eval_expr(it, defines))
        if len(vals) == 6:
            vals.append(default_rx_threshold)
        if len(vals) != 7:
            raise ExtractError(f"submode {obj}: {vals}")
        data[obj] = dict(name=vals[0], symbol_samples=vals[1], start_delay_ms=vals[2], period_s=vals[3],
                         costas=vals[4], rx_snr_threshold=vals[5], rx_threshold=vals[6])
    # which Varicode submode id maps to which Data object
    i = find_seq(sm, ["constexpr", "Data", "const", "&", "data", "("])
    j = find_seq(sm, ["default"], i)
    cases = re.findall(r"case Varicode :: (\w+) : return (\w+) ;",
                       " ".join(str(v) for _, v in sm[i:j]))
    enabled = {"Normal": "JS8_ENABLE_JS8A", "Fast": "JS8_ENABLE_JS8B", "Turbo": "JS8_ENABLE_JS8C",
               "Slow": "JS8_ENABLE_JS8E", "Ultra": "JS8_ENABLE_JS8I"}
    t["SUBMODES"] = {}
    for enum_name, obj in cases:
        d = dict(data[obj])
        d["enabled"] = bool(defines[enabled[obj]])
        t["SUBMODES"][enums["SubmodeType"][enum_name]] = d
    if sorted(t["SUBMODES"]) != [0, 1, 2, 4, 8]:
        raise ExtractError(f"submode ids {sorted(t['SUBMODES'])}")

    # varicode.cpp tables
    vc = tokenize(read(src_dir, "varicode"))

    def qstring(name):
        i = find_seq(vc, ["QString", name, "="])
        if vc[i] == ("op", "{"):
            items, _ = brace_block(vc, i)
            return latin1(one(items[0], "str"))
        if vc[i] == ("id", "QString"):                  # QString("...")
            return latin1(vc[i + 2][1])
        raise ExtractError(f"QString {name}: {vc[i]}")

    t["ALPHABET_41"] = qstring("alphabet")
    t["ALPHABET_72"] = qstring("alphabet72")
    t["ALPHANUMERIC"] = qstring("alphanumeric")
    i = find_seq(vc, ["const", "int", "nalphabet", "="])
    if vc[i][1] != len(t["ALPHABET_41"]):
        raise ExtractError("nalphabet mismatch")
    t["REGEX"] = {name: qstring(name) for name in (
        "grid_pattern", "orig_compound_callsign_pattern", "base_callsign_pattern",
        "compound_callsign_pattern", "pack_callsign_pattern", "callsign_pattern", "optional_cmd_pattern",
        "optional_grid_pattern", "optional_extended_grid_pattern", "optional_num_pattern")}
    i = find_seq(vc, ["QRegularExpression", "heartbeat_re", "("])
    t["REGEX"]["heartbeat_re"] = latin1(vc[i][1])

    def qmap(anchor):
        items, _ = brace_block(vc, find_seq(vc, anchor))
        return [it[0][1] for it in items if it]

    consts = {}
    for name in ("nbasecall", "nbasegrid", "nusergrid", "nmaxgrid"):
        i = find_seq(vc, [name, "="])
        j = find_seq(vc, [";"], i)
        consts[name] = eval_expr(vc[i:j - 1], consts)
    t["NBASECALL"], t["NBASEGRID"] = consts["nbasecall"], consts["nbasegrid"]
    t["NUSERGRID"], t["NMAXGRID"] = consts["nusergrid"], consts["nmaxgrid"]

    def pairs(anchor, key_kind, val_fn):
        out = []
        for blk in qmap(anchor):
            if len(blk) != 2:
                raise ExtractError(f"{anchor}: entry {blk}")
            k = one(blk[0], key_kind)
            out.append((latin1(k) if key_kind == "str" else k, val_fn(blk[1])))
        return tuple(out)

    num = lambda toks: eval_expr(toks, {})                                         # noqa: E731
    t["DIRECTED_CMDS"] = pairs(["directed_cmds", "="], "str", num)
    t["HUFF_TABLE"] = pairs(["hufftable", "="], "str", lambda toks: latin1(one(toks, "str")))
    t["BASECALLS"] = pairs(["basecalls", "="], "str", lambda toks: eval_expr(toks, consts))
    t["CQS"] = pairs(["cqs", "="], "num", lambda toks: latin1(one(toks, "str")))
    t["HBS"] = pairs(["hbs", "="], "num", lambda toks: latin1(one(toks, "str")))
    t["DBM2MW"] = pairs(["dbm2mw", "="], "num", num)
    t["CHECKSUM_CMDS"] = pairs(["checksum_cmds", "="], "num", num)
    for name in ("allowed_cmds", "autoreply_cmds", "buffered_cmds", "snr_cmds"):
        items, _ = brace_block(vc, find_seq(vc, [name, "="]))
        t[name.upper()] = tuple(sorted(eval_expr(it, {}) for it in items if it))
    i = find_seq(vc, ["QChar", "ESC", "="])
    t["ESC"] = chr(vc[i][1])
    i = find_seq(vc, ["QChar", "EOT", "="])
    t["EOT"] = chr(vc[i][1])

    # jsc.h sizes; the tables themselves go to --jsc-out
    jh2 = tokenize(read(src_dir, "jsc_h"))
    t["JSC_SIZE"] = jh2[find_seq(jh2, ["quint32", "size", "="])][1]
    t["JSC_PREFIX_SIZE"] = jh2[find_seq(jh2, ["quint32", "prefixSize", "="])][1]
    return t, sha


def extract_jsc(src_dir, size, prefix_size):
    def table(key, anchor, n):
        toks = tokenize(read(src_dir, key))
        items, _ = brace_block(toks, find_seq(toks, anchor))
        out = []
        for it in items:
            if not it:
                continue
            blk = it[0][1]
            if it[0][0] != "block" or len(blk) != 3:
                raise ExtractError(f"{key}: bad entry {it}")
            s = one(blk[0], "str")
            # the compiled Tuple holds a C string: bytes up to the first NUL
            s = s.split(b"\0", 1)[0]
            out.append((latin1(s), one(blk[1], "num"), one(blk[2], "num")))
        if len(out) != n:
            raise ExtractError(f"{key}: {len(out)} entries, expected {n}")
        return out

    return {
        "map": table("jsc_map", ["Tuple", "JSC", "::", "map", "[", size, "]", "="], size),
        "list": table("jsc_list", ["Tuple", "JSC", "::", "list", "[", size, "]", "="], size),
        "prefix": table("jsc_list", ["Tuple", "JSC", "::", "prefix", "[", prefix_size, "]", "="], prefix_size),
    }


def git_commit(src_dir):
    try:
        return subprocess.run(["git", "-C", src_dir, "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


# --- output ----------------------------------------------------------------------------------------

HEADER = '''"""JS8 protocol tables, extracted from JS8Call {tag} ({repo}, commit {commit}).

GENERATED by tools/js8_extract_tables.py -- do not edit; re-run the extractor instead. See
docs/js8/SPEC.md for where each table comes from and what it means.

JS8Call is (C) Jordan Sherer KN4CRD, Allan Bazinet W6BAZ and contributors, GPLv3; these tables are
derived from it and distributed under the same license (GPLv3, like pluto-tx).

The JSC word lists (2 x 262144 entries) are not in here: install-js8.sh extracts them into
js8call/jsc.json at install time, and load_jsc() reads them from there.
"""
import json
import os

'''

FOOTER = '''

JSC_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "js8call", "jsc.json")


def load_jsc(path=JSC_DEFAULT_PATH):
    """-> dict with 'map', 'list', 'prefix': lists of (str, size, index) as in JSC::map/list/prefix,
    or None if the file is missing (install-js8.sh not run)."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("commit") != SOURCE["commit"]:
        raise ValueError(f"{path} is from JS8Call commit {data.get('commit')}, expected {SOURCE['commit']}")
    return {k: [tuple(e) for e in data[k]] for k in ("map", "list", "prefix")}
'''


def render(tables, sha, commit):
    lines = [HEADER.format(tag=PINNED_TAG, repo=PINNED_REPO, commit=commit)]
    source = {"repo": PINNED_REPO, "tag": PINNED_TAG, "commit": commit,
              "sha256": dict(sorted(sha.items()))}
    lines.append(f"SOURCE = {pformat(source)}\n")
    for key in sorted(tables):
        lines.append(f"\n{key} = {pformat(tables[key])}\n")
    lines.append(FOOTER)
    return "".join(lines)


def pformat(v, indent=0):
    """Deterministic, readable Python literal (dicts keep insertion order, which is source order)."""
    pad = " " * (indent + 4)
    if isinstance(v, dict):
        if not v:
            return "{}"
        body = ",\n".join(f"{pad}{k!r}: {pformat(x, indent + 4)}" for k, x in v.items())
        return "{\n" + body + ",\n" + " " * indent + "}"
    if isinstance(v, tuple):
        if not v:
            return "()"
        if all(isinstance(x, (int, str)) for x in v):
            if len(repr(v)) <= 100 - indent:
                return repr(v)
            rows, cur = [], ""
            for x in v:
                piece = repr(x) + ","
                if cur and len(pad) + len(cur) + 1 + len(piece) > 100:
                    rows.append(cur)
                    cur = piece
                else:
                    cur = f"{cur} {piece}" if cur else piece
            rows.append(cur)
            return "(\n" + "\n".join(pad + r for r in rows) + "\n" + " " * indent + ")"
        body = ",\n".join(f"{pad}{pformat(x, indent + 4)}" for x in v)
        return "(\n" + body + ",\n" + " " * indent + ")"
    return repr(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default="js8call", help="JS8Call source tree (pinned tag)")
    ap.add_argument("--out", default=None, help="write the tables module here (default: stdout)")
    ap.add_argument("--jsc-out", default=None, help="write the JSC word lists (JSON) here")
    ap.add_argument("--allow-other-commit", action="store_true",
                    help="do not insist on the pinned commit (the file hashes are still recorded)")
    args = ap.parse_args(argv)

    commit = git_commit(args.src)
    if commit != PINNED_COMMIT and not args.allow_other_commit:
        print(f"{args.src} is at commit {commit}, pinned is {PINNED_COMMIT} ({PINNED_TAG})", file=sys.stderr)
        return 2
    commit = commit or "unknown"
    tables, sha = extract(args.src)
    text = render(tables, sha, commit)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    if args.jsc_out:
        jsc = extract_jsc(args.src, tables["JSC_SIZE"], tables["JSC_PREFIX_SIZE"])
        jsc["commit"] = commit
        with open(args.jsc_out, "w", encoding="utf-8") as f:
            json.dump(jsc, f, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
