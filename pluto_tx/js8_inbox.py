"""JS8 inbox and message store: a port of JS8Call's Inbox (JS8_Main/Inbox.cpp at the pinned commit,
docs/js8/SPEC.md 7.6) with the same SQLite schema and JSON blobs, so the file is interchangeable with JS8Call's
inbox.db3.

Types: UNREAD (a MSG to me), STORE (a MSG TO: held for another station), DELIVERED (a stored message that was
retrieved). A message is {"type", "value", "params": {UTC, TO, FROM, PATH, TDRIFT, FREQ, DIAL, OFFSET, CMD, SNR,
SUBMODE[, GRID][, EXTRA][, TEXT]}} (Message.cpp:184, mainwindow.cpp:10522-10558)."""
import json
import os
import sqlite3
import threading
import time

# Inbox.cpp:27-47
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS inbox_v1 ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "  blob TEXT"
    ");"
    "CREATE INDEX IF NOT EXISTS idx_inbox_v1__type ON"
    "  inbox_v1(json_extract(blob, '$.type'));"
    "CREATE INDEX IF NOT EXISTS idx_inbox_v1__params_from ON"
    "  inbox_v1(json_extract(blob, '$.params.FROM'));"
    "CREATE INDEX IF NOT EXISTS idx_inbox_v1__params_to ON"
    "  inbox_v1(json_extract(blob, '$.params.TO'));"
    "CREATE TABLE IF NOT EXISTS inbox_group_recip_v1 ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "  msg_id INTEGER, "
    "  callsign VARCHAR(255), "
    "  FOREIGN KEY(msg_id) REFERENCES inbox_v1(id) ON DELETE CASCADE"
    ");"
    "CREATE INDEX IF NOT EXISTS idx_inbox_group_recip_v1__callsign ON"
    "  inbox_group_recip_v1(callsign);"
)
GROUP_FLOOR_S = 2 * 86400          # Inbox.cpp:487-495 (addDays(-2))


def utc_string(t):
    """"yyyy-MM-dd hh:mm:ss" (mainwindow.cpp:10530)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))


def base_callsign(call):
    """Radio::base_callsign (JS8_Main/Radio.cpp:99-111): split on the first '/' and keep the larger side (the
    right one on a tie), upper case."""
    slash = call.find("/")
    if slash >= 0:
        call = call[slash + 1:] if len(call) - slash - 1 >= slash else call[:slash]
    return call.upper()


class Js8Inbox:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    # --- low level (Inbox.cpp:134-289) ----------------------------------------------------------------------
    def append(self, type_, params, value=""):
        blob = json.dumps({"type": type_, "value": value, "params": params}, separators=(",", ":"))
        with self._lock:
            cur = self._db.execute("INSERT INTO inbox_v1 (blob) VALUES (?);", (blob,))
            self._db.commit()
            return cur.lastrowid

    def value(self, key):
        with self._lock:
            row = self._db.execute("SELECT blob FROM inbox_v1 WHERE id = ? LIMIT 1;", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, message):
        with self._lock:
            self._db.execute("UPDATE inbox_v1 SET blob = ? WHERE id = ?;",
                             (json.dumps(message, separators=(",", ":")), key))
            self._db.commit()

    def delete(self, key):
        with self._lock:
            self._db.execute("DELETE FROM inbox_v1 WHERE id = ?;", (key,))
            self._db.commit()

    def values(self, type_, query="$", match="%", offset=0, limit=10000):
        sql = ("SELECT id, blob FROM inbox_v1 WHERE json_extract(blob, '$.type') = ? "
               "AND json_extract(blob, ?) LIKE ? ORDER BY id ASC LIMIT ? OFFSET ?;")
        with self._lock:
            rows = self._db.execute(sql, (type_, query, match, limit, offset)).fetchall()
        return [(i, json.loads(b)) for i, b in rows]

    def all(self, limit=500):
        with self._lock:
            rows = self._db.execute("SELECT id, blob FROM inbox_v1 ORDER BY id DESC LIMIT ?;", (limit,)).fetchall()
        return [(i, json.loads(b)) for i, b in rows]

    # --- storage (mainwindow.cpp:10514-10662) ---------------------------------------------------------------
    def add_command(self, type_, cmd, dial_hz=0.0):
        """addCommandToStorage: a received command (js8_commands.Js8Command) as UNREAD or STORE."""
        params = {"UTC": utc_string(cmd.utc), "TO": cmd.to, "FROM": cmd.from_call, "PATH": cmd.relay_path,
                  "TDRIFT": 0.0, "FREQ": int(round(dial_hz + cmd.offset_hz)), "DIAL": int(round(dial_hz)),
                  "OFFSET": int(round(cmd.offset_hz)), "CMD": cmd.cmd, "SNR": int(round(cmd.snr_db)),
                  "SUBMODE": cmd.submode}
        if cmd.grid:
            params["GRID"] = cmd.grid
        if cmd.extra:
            params["EXTRA"] = cmd.extra
        if cmd.text:
            params["TEXT"] = cmd.text
        return self.append(type_, params)

    def _first_with_text(self, rows):
        for i, m in rows:
            if str(m.get("params", {}).get("TEXT", "")).strip():
                return i
        return -1

    def next_message_id_for(self, callsign):
        """getNextMessageIdForCallsign (mainwindow.cpp:10560-10586)."""
        mid = self._first_with_text(self.values("STORE", "$.params.TO", callsign, 0, 10))
        if mid == -1:
            mid = self._first_with_text(self.values("STORE", "$.params.TO", base_callsign(callsign), 0, 10))
        return mid

    def lookahead_message_id_for(self, callsign, after_id):
        """getLookaheadMessageIdForCallsign (mainwindow.cpp:10588, Inbox.cpp:290-347)."""
        sql = ("SELECT inbox_v1.id, inbox_v1.blob FROM inbox_v1 WHERE inbox_v1.id > ? "
               "AND json_extract(blob, '$.type') = 'STORE' AND json_extract(blob, '$.params.TO') LIKE ? "
               "ORDER BY inbox_v1.id ASC LIMIT 10 OFFSET 0;")
        for call in (callsign, base_callsign(callsign)):
            with self._lock:
                rows = [(i, json.loads(b)) for i, b in self._db.execute(sql, (after_id, call)).fetchall()]
            mid = self._first_with_text(rows)
            if mid != -1:
                return mid
        return -1

    def _group_rows(self, group, callsign, after_id, now):
        sql = ("SELECT inbox_v1.id, inbox_v1.blob FROM inbox_v1 LEFT JOIN inbox_group_recip_v1 ON "
               "(inbox_group_recip_v1.msg_id=inbox_v1.id AND inbox_group_recip_v1.callsign = ?) "
               "WHERE inbox_v1.id > ? AND json_extract(blob, '$.type') = 'STORE' "
               "AND json_extract(blob, '$.params.TO') LIKE ? AND json_extract(blob, '$.params.UTC') > ? "
               "AND inbox_group_recip_v1.id IS NULL ORDER BY inbox_v1.id ASC LIMIT 10 OFFSET 0;")
        with self._lock:
            rows = self._db.execute(sql, (callsign, after_id, group, utc_string(now - GROUP_FLOOR_S))).fetchall()
        return [(i, json.loads(b)) for i, b in rows]

    def next_group_message_id_for(self, group, callsign, now=None):
        """Inbox::getNextGroupMessageIdForCallsign (Inbox.cpp:460-528)."""
        return self._first_with_text(self._group_rows(group, callsign, -1, time.time() if now is None else now))

    def lookahead_group_message_id_for(self, group, callsign, after_id, now=None):
        """getLookaheadGroupMessageIdForCallsign (mainwindow.cpp, Inbox.cpp:530-600)."""
        now = time.time() if now is None else now
        for call in (callsign, base_callsign(callsign)):
            mid = self._first_with_text(self._group_rows(group, call, after_id, now))
            if mid != -1:
                return mid
        return -1

    def mark_delivered(self, mid):
        """markMsgDelivered: the stored message becomes DELIVERED."""
        msg = self.value(mid)
        if msg is not None:
            msg["type"] = "DELIVERED"
            self.set(mid, msg)

    def mark_group_delivered(self, mid, callsign):
        """Inbox::markGroupMsgDeliveredForCallsign (Inbox.cpp:410-458)."""
        with self._lock:
            n = self._db.execute("SELECT count(id) FROM inbox_group_recip_v1 WHERE msg_id = ? AND callsign = ? "
                                 "LIMIT 1;", (mid, callsign)).fetchone()[0]
            if not n:
                self._db.execute("INSERT INTO inbox_group_recip_v1 (msg_id, callsign) VALUES (?,?);",
                                 (mid, callsign))
                self._db.commit()
