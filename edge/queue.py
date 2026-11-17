"""Lokal SQLite navbat: internet uzilsa hodisalar va kuzatuv uzilishlari shu yerda saqlanadi."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone


class Queue:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS q (id TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL)"
        )
        # channel = '' — barcha kanallar (masalan, agentning o'zi ishlamagan davr)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS gaps2 (start TEXT NOT NULL, channel TEXT NOT NULL DEFAULT '', end TEXT,"
            " PRIMARY KEY (start, channel))"
        )
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self.conn.commit()

    # --- kuzatuv uzilishlari
    def gap_open(self, start: str, channel: int | None = None) -> None:
        ch = "" if channel is None else str(channel)
        open_row = self.conn.execute("SELECT 1 FROM gaps2 WHERE channel = ? AND end IS NULL", (ch,)).fetchone()
        if open_row is None:
            self.conn.execute("INSERT OR IGNORE INTO gaps2 (start, channel, end) VALUES (?, ?, NULL)", (start, ch))
            self.conn.commit()

    def gap_close(self, end: str, channel: int | None = None) -> None:
        ch = "" if channel is None else str(channel)
        self.conn.execute("UPDATE gaps2 SET end = ? WHERE end IS NULL AND channel = ?", (end, ch))
        self.conn.commit()

    def gaps(self, since_hours: int = 48) -> list[dict]:
        cutoff = datetime.fromtimestamp(time.time() - since_hours * 3600, timezone.utc).isoformat()
        rows = self.conn.execute(
            "SELECT start, channel, end FROM gaps2 WHERE end IS NULL OR end >= ? ORDER BY start", (cutoff,)
        ).fetchall()
        out = []
        for s, ch, e in rows:
            item = {"start": s, "end": e}
            if ch:
                item["channel"] = int(ch)
            out.append(item)
        return out

    # --- meta
    def meta_get(self, k: str) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row[0] if row else None

    def meta_set(self, k: str, v: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))
        self.conn.commit()

    # --- hodisalar
    def put(self, event: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO q (id, payload, created) VALUES (?, ?, ?)",
            (event["id"], json.dumps(event), time.time()),
        )
        self.conn.commit()

    def peek(self, n: int) -> list[dict]:
        rows = self.conn.execute("SELECT payload FROM q ORDER BY created LIMIT ?", (n,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def ack(self, ids: list[str]) -> None:
        self.conn.executemany("DELETE FROM q WHERE id = ?", [(i,) for i in ids])
        self.conn.commit()

    def size(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM q").fetchone()[0]
