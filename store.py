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
CREATE INDEX IF NOT EXISTS idx_records_num ON records(family, key, ts, num);
CREATE TABLE IF NOT EXISTS sync_state(
  name TEXT PRIMARY KEY,
  last_ts INTEGER,
  last_wm INTEGER,
  updated_at INTEGER
);
"""

# Dedup is maintained as a physical table at write time (one row per
# family/key/ts, keeping the largest canonical number). Doing it as a VIEW means
# SQLite materialises the whole window function per query — measured at 20-80 s
# for large reads; the maintained table answers the same queries with an index
# seek. `records` keeps every raw row, so nothing is lost.
_DEDUP_TABLE = """
CREATE TABLE IF NOT EXISTS dedup(
  family TEXT NOT NULL,
  key TEXT NOT NULL,
  ts INTEGER NOT NULL,
  sid TEXT NOT NULL DEFAULT '',
  value TEXT NOT NULL DEFAULT '{}',
  num REAL,
  zone_offset INTEGER,
  zone_name TEXT,
  update_time INTEGER,
  watermark INTEGER,
  PRIMARY KEY (family, key, ts)
);
CREATE INDEX IF NOT EXISTS idx_dedup_ts ON dedup(family, key, ts);
CREATE VIEW IF NOT EXISTS v_dedup AS
  SELECT family, key, ts, sid, value, num, zone_offset, zone_name, update_time, watermark
  FROM dedup;
"""

_DEDUP_REFRESH = """
DELETE FROM dedup WHERE family=? AND key=? AND ts BETWEEN ? AND ?;
INSERT INTO dedup(family,key,ts,sid,value,num,zone_offset,zone_name,update_time,watermark)
  SELECT family,key,ts,sid,value,num,zone_offset,zone_name,update_time,watermark FROM (
    SELECT *, ROW_NUMBER() OVER (
              PARTITION BY family, key, ts
              ORDER BY (num IS NULL), num DESC, rowid) AS _rn
    FROM records
    WHERE family=? AND key=? AND ts BETWEEN ? AND ?
  ) WHERE _rn = 1;
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
        self.db.executescript(_DEDUP_TABLE)
        self.db.commit()
        self.dedup_mode = "materialized"

    def rebuild_dedup(self, progress=None):
        """One-off (re)build of the dedup table from raw rows."""
        with self._lock:
            self.db.execute("DELETE FROM dedup")
            self.db.execute(
                "INSERT INTO dedup(family,key,ts,sid,value,num,zone_offset,zone_name,update_time,watermark)"
                " SELECT family,key,ts,sid,value,num,zone_offset,zone_name,update_time,watermark FROM ("
                "   SELECT *, ROW_NUMBER() OVER (PARTITION BY family,key,ts"
                "                                ORDER BY (num IS NULL), num DESC, rowid) rn"
                "   FROM records) WHERE rn=1")
            self.db.commit()
        return self.count(dedup=True)

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
            # refresh the dedup rows for exactly the (family,key) ts-ranges touched
            spans = {}
            for (fam, key, _sid, ts, *_rest) in rows:
                lo, hi = spans.get((fam, key), (ts, ts))
                spans[(fam, key)] = (min(lo, ts), max(hi, ts))
            for (fam, key), (lo, hi) in spans.items():
                self.db.execute(_DEDUP_REFRESH, (fam, key, lo, hi, fam, key, lo, hi))
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
    def rows(self, family, key=None, start_ms=None, end_ms=None, dedup=True, limit=None,
             sid=None, order="asc"):
        """Read rows as wire-shaped dicts (value JSON decoded, _t/_sid/_key filled).

        Dimension filters: family / key / time window / source sid;
        `dedup=False` returns raw per-source rows instead of the collapsed view.
        """
        view = "dedup" if dedup else "records"
        q = f"SELECT key,ts,sid,value,zone_offset,zone_name,update_time,watermark FROM {view} WHERE family=?"
        args = [family]
        if key:
            q += " AND key=?"; args.append(key)
        if sid:
            q += " AND sid=?"; args.append(sid)
        if start_ms is not None:
            q += " AND ts>=?"; args.append(int(start_ms // 1000))
        if end_ms is not None:
            q += " AND ts<=?"; args.append(int(end_ms // 1000))
        q += " ORDER BY ts " + ("DESC" if str(order).lower() == "desc" else "ASC")
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
            obj["_key"] = k          # record's own key (sport type, data key, ...)
            out.append(obj)
        return out

    def stats(self, dedup=False):
        with self._lock:
            rows = list(self.db.execute(
                "SELECT family, key, COUNT(*), MIN(ts), MAX(ts) FROM "
                + ("dedup" if dedup else "records")
                + " GROUP BY family, key ORDER BY family, key"))
        out = []
        for fam, key, n, lo, hi in rows:
            out.append({"family": fam, "key": key, "count": n, "first": lo, "last": hi})
        return out

    def aggregate(self, family, key, field, bucket_s=3600, agg="sum",
                  start_ms=None, end_ms=None, sid=None, dedup=True):
        """Server-side aggregation over the inner JSON value field.

        One SQL pass — no row decoding — so 7-day (or 10-year) series are cheap:
          SELECT ts/bucket, <agg>(json_extract(value,'$.<field>')) ... GROUP BY bucket
        """
        agg = agg.lower()
        if agg not in ("sum", "max", "min", "avg", "count"):
            raise ValueError("agg must be sum|max|min|avg|count")
        expr = "COUNT(*)" if agg == "count" else f"{agg.upper()}(json_extract(value, '$.{field}'))"
        table = "dedup" if dedup else "records"
        q = (f"SELECT (ts/{int(bucket_s)})*{int(bucket_s)} AS b, {expr} AS v "
             f"FROM {table} WHERE family=? AND key=?")
        args = [family, key]
        if sid:
            q += " AND sid=?"; args.append(sid)
        if start_ms is not None:
            q += " AND ts>=?"; args.append(int(start_ms // 1000))
        if end_ms is not None:
            q += " AND ts<=?"; args.append(int(end_ms // 1000))
        q += " GROUP BY b ORDER BY b"
        with self._lock:
            rows = list(self.db.execute(q, args))
        return [{"t": b, "v": v} for b, v in rows if v is not None]

    def keys_of(self, family):
        """Distinct keys (and their row counts) inside a family."""
        with self._lock:
            return [{"key": k, "count": n}
                    for k, n in self.db.execute(
                        "SELECT key, COUNT(*) FROM records WHERE family=? GROUP BY key ORDER BY 2 DESC",
                        (family,))]

    def sources(self, family=None, key=None):
        """Distinct data sources (sid) — watch / phone / app lines."""
        q = "SELECT sid, COUNT(*) FROM records"
        args, cond = [], []
        if family:
            cond.append("family=?"); args.append(family)
        if key:
            cond.append("key=?"); args.append(key)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " GROUP BY sid ORDER BY 2 DESC"
        with self._lock:
            return [{"sid": s, "count": n} for s, n in self.db.execute(q, args)]

    def count(self, family=None, key=None, dedup=False):
        q = "SELECT COUNT(*) FROM " + ("dedup" if dedup else "records")
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
