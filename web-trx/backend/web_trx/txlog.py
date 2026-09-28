"""Persistent record of every TX activity (start/end time, mode,
frequency, parameters) -- accountability for a remotely-controlled
amateur radio station, built directly on the keyed/unkeyed events every
SessionBackend already emits (see session.py's SessionManager, which
calls record_keyed()/record_unkeyed() as those events pass through).

Plain sqlite3 (stdlib, synchronous) rather than an async driver: writes
only happen on keyed/unkeyed transitions (a human pressing PTT), never in
a hot loop like the spectrum/audio generators, so a few milliseconds of
blocking the event loop per transition is an accepted, deliberate
trade-off against pulling in an extra dependency (aiosqlite) for
something this infrequent.
"""
from __future__ import annotations

import json
import sqlite3
import time


class TxLog:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tx_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL,
                ended_at REAL,
                mode TEXT NOT NULL,
                freq_hz REAL,
                device_type TEXT,
                connection TEXT,
                params_json TEXT NOT NULL
            )
            """
        )
        self._conn.commit()
        self._open_id: int | None = None

    def record_keyed(
        self, mode: str | None, freq_hz: float | None, device_type: str | None,
        connection: str | None, params: dict,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO tx_log (started_at, mode, freq_hz, device_type, connection, params_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), mode, freq_hz, device_type, connection, json.dumps(params)),
        )
        self._conn.commit()
        self._open_id = cur.lastrowid
        return cur.lastrowid

    def record_unkeyed(self) -> None:
        """No-op if nothing is open -- estop()/unkeyed can both fire after
        each other in some paths, and this must stay safe either way."""
        if self._open_id is None:
            return
        self._conn.execute("UPDATE tx_log SET ended_at = ? WHERE id = ?", (time.time(), self._open_id))
        self._conn.commit()
        self._open_id = None

    def recent(self, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            "SELECT id, started_at, ended_at, mode, freq_hz, device_type, connection, params_json "
            "FROM tx_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": row[0],
                "started_at": row[1],
                "ended_at": row[2],
                "mode": row[3],
                "freq_hz": row[4],
                "device_type": row[5],
                "connection": row[6],
                "params": json.loads(row[7]),
            }
            for row in cur.fetchall()
        ]

    def close(self) -> None:
        self._conn.close()
