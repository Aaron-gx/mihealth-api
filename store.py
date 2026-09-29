#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite store for Mi Health records.

Schema
------
records(family, key, sid, ts, value, num, zone_offset, zone_name, update_time, watermark)
    PK (family, key, sid, ts)  — per-source raw rows, idempotent upsert.
    `num` is a canonical numeric extracted per key (see SCALAR_FIELDS) used by
    the dedup view; NULL for non-scalar keys.
v_dedup — one row per (family, key, ts), keeps the row with the largest `num`
    (collapses paging overlap and multi-source duplicates at query time).
sync_state(name, last_ts, last_wm, updated_at) — incremental cursors.

Everything is stdlib sqlite3; safe to open from several processes (WAL).
"""
import csv, json, os, sqlite3, threading, time

from mihealth_client import SCALAR_FIELDS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records(
  family TEXT NOT NULL,
  key TEXT NOT NULL,
  sid TEXT NOT NULL DEFAULT '',
  ts INTEGER NOT NULL,
  value TEXT NOT NULL DEFAULT '{}',
  num REAL,
  zone_offset INTEGER,
  zone_name TEXT,
  update_time INTEGER,
  watermark INTEGER,
  PRIMARY KEY (family, key, sid, ts)
);
CREATE INDEX IF NOT EXISTS idx_records_ts ON records(family, key, ts);
CREATE TABLE IF NOT EXISTS sync_state(
  name TEXT PRIMARY KEY,
  last_ts INTEGER,
  last_wm INTEGER,
  updated_at INTEGER
);
CREATE VIEW IF NOT EXISTS v_dedup AS
  SELECT family, key, ts, sid, value, num, zone_offset, zone_name, update_time, watermark
  FROM records r
  WHERE num IS NULL
     OR num = (SELECT MAX(num) FROM records x
               WHERE x.family = r.family AND x.key = r.key AND x.ts = r.ts)
  GROUP BY family, key, ts;
"""


def canonical_num(key, value_obj):
    for f in SCALAR_FIELDS.get(key, ()):
        v = value_obj.get(f)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


class Store:
    """Thread-safe SQLite store (one shared connection, serialised by a lock)."""

    def __init__(self, path="mihealth.db"):
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self):
        with self._lock:
            try:
                self.db.commit()
            finally:
                self.db.close()

    # ---------------------------------------------------------- writes
    def upsert(self, family, key, items, batch=2000):
        """Insert raw records; (family,key,sid,ts) upsert makes re-syncs idempotent.

        items: iterable of the wire dicts {sid,time,value,zone_offset,...}.
        Returns number of rows written (including replaced ones).
        """
        n = 0
        rows = []
        for it in items:
            ts = it.get("time") or it.get("_t")
            if ts is None:
                continue
            try:
                vobj = json.loads(it.get("value") or "{}")
            except Exception:
                vobj = {}
            rows.append((
                family, key, str(it.get("sid") or ""), int(ts),
                it.get("value") or "{}", canonical_num(key, vobj),
                it.get("zone_offset"), it.get("zone_name"),
                it.get("update_time"), it.get("watermark"),
            ))
            if len(rows) >= batch:
                n += self._flush(rows)
                rows = []
        n += self._flush(rows)
        return n

    def _flush(self, rows):
        if not rows:
            return 0
        with self._lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO records"
                "(family,key,sid,ts,value,num,zone_offset,zone_name,update_time,watermark)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.commit()
        return len(rows)

    # ---------------------------------------------------------- state
    def set_state(self, name, last_ts=None, last_wm=None):
        """last_ts is in SECONDS (same unit as the wire `time` field)."""
        with self._lock:
            cur = self.db.execute("SELECT last_ts,last_wm FROM sync_state WHERE name=?",
                                  (name,)).fetchone()
            if cur:
                self.db.execute(
                    "UPDATE sync_state SET last_ts=COALESCE(?,last_ts), last_wm=COALESCE(?,last_wm),"
                    " updated_at=? WHERE name=?",
                    (last_ts, last_wm, int(time.time() * 1000), name))
            else:
                self.db.execute(
                    "INSERT INTO sync_state(name,last_ts,last_wm,updated_at) VALUES(?,?,?,?)",
                    (name, last_ts, last_wm, int(time.time() * 1000)))
            self.db.commit()

    def get_state(self, name, field="last_wm"):
        with self._lock:
            row = self.db.execute(f"SELECT {field} FROM sync_state WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def states(self):
        with self._lock:
            rows = list(self.db.execute("SELECT name,last_ts,last_wm,updated_at FROM sync_state"))
        return {r[0]: {"last_ts": r[1], "last_wm": r[2], "updated_at": r[3]} for r in rows}

    # ---------------------------------------------------------- reads
    def rows(self, family, key=None, start_ms=None, end_ms=None, dedup=True, limit=None):
        """Read rows as wire-shaped dicts (value JSON decoded, _t filled)."""
        view = "v_dedup" if dedup else "records"
        q = f"SELECT key,ts,sid,value,zone_offset,zone_name,update_time,watermark FROM {view} WHERE family=?"
        args = [family]
        if key:
            q += " AND key=?"; args.append(key)
        if start_ms is not None:
            q += " AND ts>=?"; args.append(int(start_ms // 1000))
        if end_ms is not None:
            q += " AND ts<=?"; args.append(int(end_ms // 1000))
        q += " ORDER BY ts ASC"
        if limit:
            q += " LIMIT ?"; args.append(int(limit))
        with self._lock:
            data = list(self.db.execute(q, args))
        out = []
        for (k, ts, sid, value, zo, zn, ut, wm) in data:
            try:
                obj = json.loads(value or "{}")
            except Exception:
                obj = {}
            obj["_t"] = ts
            obj["_sid"] = sid
            out.append(obj)
        return out

    def stats(self):
        with self._lock:
            rows = list(self.db.execute(
                "SELECT family, key, COUNT(*), MIN(ts), MAX(ts) FROM records "
                "GROUP BY family, key ORDER BY family, key"))
        out = []
        for fam, key, n, lo, hi in rows:
            out.append({"family": fam, "key": key, "count": n, "first": lo, "last": hi})
        return out

    def count(self, family=None, key=None):
        q = "SELECT COUNT(*) FROM records"
        args, cond = [], []
        if family:
            cond.append("family=?"); args.append(family)
        if key:
            cond.append("key=?"); args.append(key)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        with self._lock:
            return self.db.execute(q, args).fetchone()[0]

    # ---------------------------------------------------------- export
    def to_csv(self, path, family, key=None, start_ms=None, end_ms=None, dedup=True):
        rows = self.rows(family, key, start_ms, end_ms, dedup=dedup)
        cols = ["family", "key", "time", "sid"]
        extra = set()
        for r in rows:
            extra.update(k for k in r if not k.startswith("_") and k not in ("time", "sid"))
        extra = sorted(extra)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols + extra)
            for r in rows:
                vals = [family, key, r.get("_t"), r.get("_sid")]
                for c in extra:
                    v = r.get(c)
                    vals.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                w.writerow(vals)
        return len(rows)
