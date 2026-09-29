#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mi Health sync orchestrator — backfill / incremental / status / export.

  python sync.py backfill [--keys steps,sleep] [--workers 3]   # full history -> SQLite
  python sync.py incremental [--hours 72]                      # watermark feed + window top-up
  python sync.py status                                        # cursors + row counts
  python sync.py export --family fitness --key steps --csv out.csv

First run:  sync.py backfill        (time-paged full history, all keys)
After that: sync.py incremental     (cron/任务计划, a handful of requests)

Incremental strategy
--------------------
1. watermark feed per family (fitness / sport) — forward-only global stream,
   cursor stored in sync_state; cheap and complete for *new* records.
2. time-window top-up for the last --hours per key — catches records the feed
   may not surface (and repairs any gap after long downtime).
"""
import argparse, json, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mihealth_client import (ApiError, AuthError, MiHealthClient, SCALAR_FIELDS,
                             client_from_config, load_config)
from store import Store

FITNESS_KEYS = ["steps", "calories", "sleep", "heart_rate", "stress", "spo2",
                "intensity", "valid_stand", "energy", "goal", "pai",
                "blood_pressure", "blood_sugar", "headset", "weight",
                "vo2_max", "menstruation"]

# family -> (watermark endpoint, list field, watermark type for get_max_watermark)
WM_FAMILIES = {
    "fitness": ("data/get_fitness_data_by_watermark", "data_list", 0),
    "sport":   ("data/get_sport_records_by_watermark", "sport_records", 1),
}

DAY = 86400000


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


class Syncer:
    def __init__(self, db_path, workers=3, min_interval=0.4, refresh=None, config_path=None):
        self.db = Store(db_path)
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.workers = max(1, workers)
        self.min_interval = min_interval
        self.refresh = refresh          # callable(client) -> bool, invoked on AuthError
        self.stop = threading.Event()

    # ------------------------------------------------------------ helpers
    def _client(self):
        # re-read config from disk every time so a credential refresh (by the
        # auth hook or another process) is picked up by newly created clients
        self.cfg = load_config(self.config_path)
        return client_from_config(self.cfg, min_interval=self.min_interval,
                                  on_auth_error=self._on_auth_error)

    def _on_auth_error(self, client, why):
        """Returns True when credentials were refreshed (client then retries)."""
        if not (self.refresh and not self.stop.is_set()):
            return False
        log(f"auth error ({why}) -> refreshing credentials from device")
        try:
            ok = bool(self.refresh(client))
            log("credentials refreshed" if ok else "credential refresh returned nothing")
            return ok
        except Exception as e:
            log("credential refresh failed:", e)
            return False

    def _guard(self, fn, *a, **kw):
        """Run fn, translating AuthError into a clear operator message."""
        try:
            return fn(*a, **kw)
        except AuthError as e:
            log("AUTH FAILED:", e)
            log("  -> tools/refresh_credentials.py (or update config.json) and retry")
            raise SystemExit(2)

    # ------------------------------------------------------------ backfill
    def backfill(self, keys=None, families=("fitness", "sport"), days=0):
        keys = keys or FITNESS_KEYS
        now = int(time.time() * 1000)
        start = now - days * DAY if days else 0

        # 1) fitness keys in parallel
        log(f"backfill fitness: {len(keys)} keys, workers={self.workers}")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(self._backfill_key, k, start, now): k for k in keys}
            for f in as_completed(futs):
                k = futs[f]
                try:
                    n, more = f.result()
                    log(f"  {k}: {n} rows" + (" (partial, max_pages hit)" if more else ""))
                except SystemExit:
                    self.stop.set()
                    for x in futs: x.cancel()
                    raise
                except Exception as e:
                    log(f"  {k}: FAILED {e}")

        # 2) sport records
        if "sport" in families:
            n, more = self._backfill_sport(start, now)
            log(f"  sport_records: {n} rows" + (" (partial)" if more else ""))

        # 3) aggregated daily goals
        if "fitness" in families and not keys or "goal" in (keys or []):
            n = self._backfill_goals(start, now)
            log(f"  daily_goals: {n} rows")

        # 4) seed watermark cursors so the next incremental run starts 'now'
        for fam, (_sub, _field, wtype) in WM_FAMILIES.items():
            try:
                wm = self._client().get_max_watermark(wtype)
                self.db.set_state(f"wm_{fam}", last_wm=wm)
                log(f"  cursor wm_{fam} seeded at {wm}")
            except Exception as e:
                log(f"  cursor wm_{fam} seed failed: {e}")
        log("backfill done")

    def _backfill_key(self, key, start_ms, end_ms):
        """Stream every page straight into SQLite (bounded memory).

        The cursor overlaps pages heavily on some keys (headset repeated the same
        rows ~20x in one measured export), so accumulating in memory before the
        write can hold millions of dicts; upserting per page also makes progress
        visible and keeps a mid-run interruption useful.
        """
        c = self._client()
        stats = {"rows": 0, "pages": 0, "newest": 0}

        def on_page(batch, _nk, _page):
            stats["pages"] += 1
            if not batch:
                return
            stats["rows"] += self.db.upsert("fitness", key, batch)
            newest = max((x.get("_t") or 0) for x in batch)
            if newest > stats["newest"]:
                stats["newest"] = newest

        res = self._guard(c.get_fitness_data_all, key, start_ms, end_ms,
                          max_pages=3000, full_history=True, on_page=on_page)
        if stats["newest"]:
            self.db.set_state(f"ts_fitness_{key}", last_ts=stats["newest"])
        return stats["rows"], res.get("has_more")

    def _upsert_sport(self, batch):
        """Sport rows keep their own key (outdoor_running, swimming, ...) so the
        record type survives storage and queries can filter per sport."""
        by_key = {}
        for it in batch:
            by_key.setdefault(it.get("key") or "unknown", []).append(it)
        n = 0
        for k, rows in by_key.items():
            n += self.db.upsert("sport", k, rows)
        return n

    def _backfill_sport(self, start_ms, end_ms):
        c = self._client()
        stats = {"rows": 0, "newest": 0}

        def on_page(batch, _nk, _page):
            if not batch:
                return
            stats["rows"] += self._upsert_sport(batch)
            stats["newest"] = max(stats["newest"], max((x.get("_t") or 0) for x in batch))

        res = self._guard(c.get_sport_records_all, start_ms, end_ms,
                          max_pages=500, full_history=True, on_page=on_page)
        if stats["newest"]:
            self.db.set_state("ts_sport_sport_records", last_ts=stats["newest"])
        return stats["rows"], res.get("has_more")

    def _backfill_goals(self, start_ms, end_ms):
        c = self._client()
        res = self._guard(c.get_aggregated_all, None, start_ms, end_ms, "daily_fitness", 100)
        return self.db.upsert("goal", "goal", res["data_list"])

    # ------------------------------------------------------------ incremental
    def incremental(self, hours=72, families=("fitness", "sport")):
        total = 0
        # 1) watermark feeds
        for fam in families:
            if self.stop.is_set():
                break
            total += self._wm_family(fam)
        # 2) time-window top-up per key
        total += self._window_topup(hours)
        log(f"incremental done: {total} rows")
        return total

    def _wm_family(self, fam):
        """Walk one family's forward feed, persisting rows + cursor per page so
        an interrupted run loses nothing."""
        sub, field, wtype = WM_FAMILIES[fam]
        c = self._client()
        last = self.db.get_state(f"wm_{fam}", "last_wm")
        if not last:
            last = self._guard(c.get_max_watermark, wtype)
            self.db.set_state(f"wm_{fam}", last_wm=last)
            log(f"  wm_{fam}: cursor initialised at {last} (next run pulls changes)")
            return 0
        stats = {"rows": 0, "pages": 0}

        def on_page(batch, wm, page):
            stats["pages"] = page + 1
            if batch:
                if fam == "sport":
                    stats["rows"] += self._upsert_sport(batch)
                else:  # fitness feed is key-mixed
                    by_key = {}
                    for it in batch:
                        by_key.setdefault(it.get("key") or "unknown", []).append(it)
                    for k, rows in by_key.items():
                        stats["rows"] += self.db.upsert("fitness", k, rows)
            if wm:                      # never persist the 0 = "at head" marker
                self.db.set_state(f"wm_{fam}", last_wm=wm)

        _items, new_wm, more = self._guard(c.sync_by_watermark, sub, field, last,
                                           max_pages=400, on_page=on_page)
        if new_wm and new_wm != last:
            self.db.set_state(f"wm_{fam}", last_wm=new_wm)
        log(f"  wm_{fam}: pages={stats['pages']} rows={stats['rows']} cursor={new_wm}"
            + (" (at head)" if not more else " (more pending)"))
        return stats["rows"]

    def _window_topup(self, hours):
        now = int(time.time() * 1000)
        start = now - hours * 3600000
        written = 0

        def work(key):
            c = self._client()
            stats = {"rows": 0, "newest": 0}

            def on_page(batch, _nk, _page):
                if not batch:
                    return
                stats["rows"] += self.db.upsert("fitness", key, batch)
                stats["newest"] = max(stats["newest"], max((x.get("_t") or 0) for x in batch))

            self._guard(c.get_fitness_data_all, key, start, now, max_pages=200,
                        on_page=on_page)
            if stats["newest"]:
                prev = self.db.get_state(f"ts_fitness_{key}", "last_ts") or 0
                if stats["newest"] > prev:
                    self.db.set_state(f"ts_fitness_{key}", last_ts=stats["newest"])
            return stats["rows"]

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = [pool.submit(work, k) for k in FITNESS_KEYS]
            for f in as_completed(futs):
                try:
                    written += f.result()
                except SystemExit:
                    self.stop.set()
                    for x in futs: x.cancel()
                    raise
                except Exception as e:
                    log("  topup failed:", e)

        c = self._client()
        try:
            sp = {"rows": 0}

            def on_sport_page(batch, _nk, _page):
                if batch:
                    sp["rows"] += self._upsert_sport(batch)

            self._guard(c.get_sport_records_all, start, now, max_pages=50,
                        on_page=on_sport_page)
            written += sp["rows"]
        except SystemExit:
            raise
        except Exception as e:
            log("  sport topup failed:", e)
        try:
            res = self._guard(c.get_aggregated_all, None, now - 30 * DAY, now, "daily_fitness", 100)
            written += self.db.upsert("goal", "goal", res["data_list"])
        except SystemExit:
            raise
        except Exception as e:
            log("  goals topup failed:", e)
        return written

    # ------------------------------------------------------------ reporting
    def status(self):
        st = self.db.states()
        print(f"db: {self.db.path}  ({os.path.getsize(self.db.path)//1024} KB)")
        print("cursors:")
        for k, v in sorted(st.items()):
            ts = v["last_ts"]                       # seconds
            wm = v["last_wm"]
            human = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"
            print(f"  {k:28} last_ts={human:16} last_wm={wm}")
        print("rows:")
        tot = 0
        for r in self.db.stats():
            tot += r["count"]
            first = time.strftime("%Y-%m-%d", time.localtime(r["first"])) if r["first"] else "-"
            last = time.strftime("%Y-%m-%d", time.localtime(r["last"])) if r["last"] else "-"
            print(f"  {r['family']:8} {r['key']:18} {r['count']:>8}  {first} -> {last}")
        print(f"  TOTAL {tot}")


def make_refresh(config_path=None):
    """Returns a refresh callable that re-reads credentials from the device."""
    def try_refresh(client):
        try:
            sys.path.insert(0, os.path.join(HERE, "tools"))
            import importlib
            import refresh_credentials as rc
            importlib.reload(rc)
            creds = rc.fetch_from_device()
            if not creds:
                return False
            rc.write_config(creds, config_path)
            client.reload_credentials(creds["ssecurity"], creds["service_token"],
                                      creds["cuser_id"])
            return True
        except Exception as e:
            log("  refresh failed:", e)
            return False
    return try_refresh


def main():
    ap = argparse.ArgumentParser(description="Mi Health data sync")
    ap.add_argument("command", choices=["backfill", "incremental", "status", "export"],
                    nargs="?", default="incremental")
    ap.add_argument("--db", default=os.path.join(HERE, "mihealth.db"))
    ap.add_argument("--config", help="凭据文件路径（默认 ./config.json）")
    ap.add_argument("--keys", help="逗号分隔的数据键（默认全部）")
    ap.add_argument("--days", type=int, default=0, help="backfill 窗口天数（0=全历史）")
    ap.add_argument("--hours", type=int, default=72, help="incremental 时间窗补拉小时数")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--min-interval", type=float, default=0.4, help="请求最小间隔秒")
    ap.add_argument("--no-auto-refresh", action="store_true")
    # export
    ap.add_argument("--family")
    ap.add_argument("--key")
    ap.add_argument("--csv")
    ap.add_argument("--start"); ap.add_argument("--end")
    a = ap.parse_args()

    cfg = load_config(a.config)
    refresh = None if a.no_auto_refresh else make_refresh(a.config)
    S = Syncer(a.db, workers=a.workers, min_interval=a.min_interval,
               refresh=refresh, config_path=a.config)

    if a.command == "status":
        S.status()
    elif a.command == "backfill":
        keys = [k.strip() for k in a.keys.split(",")] if a.keys else None
        S.backfill(keys=keys, days=a.days)
        S.status()
    elif a.command == "export":
        if not (a.family and a.csv):
            raise SystemExit("export 需要 --family 与 --csv")
        n = S.db.to_csv(a.csv, a.family, a.key,
                        int(a.start) if a.start else None,
                        int(a.end) if a.end else None)
        print(f"wrote {n} rows -> {a.csv}")
    else:
        S.incremental(hours=a.hours)
    S.db.close()


if __name__ == "__main__":
    main()
