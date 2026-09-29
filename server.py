#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mi Health HTTP gateway + dashboard.

Reads from the local SQLite mirror (`sync.py backfill/incremental`) when the
database exists and falls back to live API calls otherwise — so the dashboard
stays fast and only sync.py talks to the cloud in bulk.

Routes
------
  GET  /                        dashboard
  GET  /api/health              auth + storage status
  GET  /api/overview            today's cards
  GET  /api/series/<key>        deduped series (?hours=24|168|720)
  GET  /api/daily_goals         daily goal summary (?days=14)
  GET  /api/sport_records       sport list (?days=30)
  GET  /api/sync/status         cursors + row counts
  POST /api/sync                run incremental sync in background
  GET  /api/fitness/<key>       live: raw minute data (?start=&end=)
  GET  /api/medical /api/project /api/stat/<key> /api/aggregated
  GET  /api/sport_summary /api/max_watermark /api/watermark/<key>
  GET  /api/latest /api/relatives/<sub> /api/raw/<path> /api/keys
"""
import json, os, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from flask import Flask, jsonify, request, send_from_directory

from mihealth_client import (AuthError, MiHealthClient, SCALAR_FIELDS,
                             client_from_config, load_config)
from store import Store

app = Flask(__name__, static_folder=None)
CFG = load_config()
client = client_from_config(CFG)

DAY = 86400000
DB_PATH = CFG.get("db") or os.path.join(HERE, "mihealth.db")
_store = None
try:
    if os.path.exists(DB_PATH):
        _store = Store(DB_PATH)
        print(f"[store] using {DB_PATH}", flush=True)
    else:
        print("[store] no db yet — run `python sync.py backfill` for cached mode; "
              "serving live API", flush=True)
except Exception as e:                                          # pragma: no cover
    print(f"[store] cannot open {DB_PATH}: {e}", flush=True)

_cache, _sync_job = {}, {"running": False, "started": None, "result": None}


def cached(key, ttl_ms, fn):
    ent = _cache.get(key)
    if ent and time.time() * 1000 - ent[0] < ttl_ms:
        return ent[1]
    val = fn()
    _cache[key] = (time.time() * 1000, val)
    return val


def ms_now():
    return int(time.time() * 1000)


def today_start_ms():
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)


def _parse_items(res):
    items = []
    for it in res.get("data_list", []):
        try:
            inner = json.loads(it.get("value") or "{}")
        except Exception:
            inner = {}
        inner["_t"] = it.get("time")
        inner["_sid"] = it.get("sid")
        items.append(inner)
    return items


def fetch_series_range(key, start_ms, end_ms):
    """DB first, live fallback. Items are deduped in both paths."""
    if _store is not None:
        return {"ok": True, "key": key, "items": _store.rows("fitness", key, start_ms, end_ms),
                "source": "db"}
    res = client.get_fitness_data_all(key, start_ms, end_ms, max_pages=400,
                                      full_history=start_ms <= 0)
    if res is None:
        return {"ok": False, "err": "null"}
    return {"ok": True, "key": key, "items": _parse_items(res), "source": "live",
            "has_more": res.get("has_more")}


def fetch_series(key, hours):
    end = ms_now()
    return fetch_series_range(key, 0 if hours <= 0 else end - int(hours * 3600000), end)


# ---------------------------------------------------------------- page/status

@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.route("/vendor/<path:f>")
def vendor(f):
    return send_from_directory(os.path.join(HERE, "static"), f)


@app.route("/api/health")
def api_health():
    st = {"ok": True, "mode": "db" if _store else "live",
          "db": DB_PATH if _store else None,
          "host": client.base, "cuser_id": client.cuser,
          "token_prefix": (client.service_token or "")[:16] + "...",
          "sseccurity_set": bool(client.security)}
    if _store:
        st["rows"] = _store.count()
        st["cursors"] = _store.states()
    try:
        client.call("data/get_max_watermark", {"data": '{"type":0}'}, retries=0)
        st["auth"] = "ok"
    except AuthError as e:
        st["auth"] = "expired"
        st["auth_error"] = str(e)
    except Exception as e:
        st["auth"] = "error"
        st["auth_error"] = str(e)
    if _sync_job["running"]:
        st["syncing"] = True
    return jsonify(st)


@app.route("/api/sync/status")
def api_sync_status():
    out = {"ok": True, "running": _sync_job["running"], "started": _sync_job["started"],
           "result": _sync_job["result"]}
    if _store:
        out["rows"] = _store.count()
        out["cursors"] = _store.states()
    return jsonify(out)


@app.route("/api/sync", methods=["POST"])
def api_sync_run():
    if _sync_job["running"]:
        return jsonify({"ok": False, "err": "already running"}), 409

    def job():
        _sync_job.update(running=True, started=ms_now(), result=None)
        try:
            import importlib
            import sync as sync_mod
            importlib.reload(sync_mod)
            S = sync_mod.Syncer(DB_PATH, workers=2, min_interval=0.5,
                                refresh=sync_mod.try_refresh)
            n = S.incremental(hours=72)
            S.db.close()
            _sync_job["result"] = {"ok": True, "rows": n}
            _cache.clear()
        except SystemExit as e:
            _sync_job["result"] = {"ok": False, "err": f"auth/credentials ({e})"}
        except Exception as e:
            _sync_job["result"] = {"ok": False, "err": str(e)}
        finally:
            _sync_job["running"] = False

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True, "started": True})


# ---------------------------------------------------------------- dashboard data

@app.route("/api/overview")
def api_overview():
    now = ms_now()
    t0 = today_start_ms()

    def grab(key, hours):
        r = fetch_series(key, hours)
        return r.get("items", []) if r.get("ok") else []

    steps_today = fetch_series_range("steps", t0, now).get("items", [])
    cals_today = fetch_series_range("calories", t0, now).get("items", [])
    hr = grab("heart_rate", 24)
    spo2 = grab("spo2", 48)
    sleep = grab("sleep", 72)
    weight = grab("weight", 90 * 24)
    pai = grab("pai", 24 * 14)
    return jsonify({
        "ok": True, "ts": now, "today_start": t0,
        "source": "db" if _store else "live",
        "steps_today": sum(int(x.get("steps") or 0) for x in steps_today),
        "calories_today": round(sum(float(x.get("calories") or 0) for x in cals_today), 1),
        "steps_today_points": len(steps_today),
        "hr_last": next((x["bpm"] for x in reversed(hr) if x.get("bpm")), None),
        "hr_min_24h": min([x["bpm"] for x in hr if x.get("bpm")] or [None]),
        "spo2_last": next((x["spo2"] for x in reversed(spo2) if x.get("spo2")), None),
        "sleep_last": sleep[-1] if sleep else None,
        "weight_last": next((x.get("weight") for x in reversed(weight) if x.get("weight")), None),
        "pai": next((x for x in reversed(pai) if x.get("daily_pai") or x.get("pai")), None),
        "counts": {"steps": len(steps_today), "hr": len(hr), "sleep": len(sleep), "spo2": len(spo2)},
    })


@app.route("/api/series/<key>")
def api_series(key):
    hours = float(request.args.get("hours", 24))
    return jsonify(cached(f"s:{key}:{int(hours)}", 5 * 60000,
                          lambda: fetch_series(key, 0 if hours <= 0 else hours)))


@app.route("/api/daily_goals")
def api_daily_goals():
    days = int(request.args.get("days", 14))
    start = ms_now() - days * DAY

    def go():
        FIELD = {1: "steps", 2: "calories", 3: "stand_hours", 4: "active_minutes"}
        if _store is not None:
            raw = _store.rows("goal", "goal", start, ms_now())
        else:
            res = client.get_aggregated_all(None, start, ms_now(), "daily_fitness", 100)
            raw = []
            for it in res["data_list"]:
                try:
                    obj = json.loads(it.get("value") or "{}")
                except Exception:
                    obj = {}
                obj["_t"] = it.get("time")
                raw.append(obj)
        out, seen = [], set()
        for it in sorted(raw, key=lambda x: x.get("_t") or 0, reverse=True):
            t = it.get("_t")
            if t in seen:
                continue
            seen.add(t)
            rows = {}
            for g in it.get("goal_items", []):
                f = FIELD.get(g.get("field"), f"field_{g.get('field')}")
                rows[f] = {"achieved": g.get("achieved_value"), "target": g.get("target_value")}
            sg = it.get("stand_goal") or {}
            rows["stand_hours"] = {"achieved": sg.get("achieved_value"),
                                   "target": sg.get("target_value")}
            out.append({"time": t, "goals": rows})
        return {"ok": True, "count": len(out), "items": out}

    return jsonify(cached(f"goals:{days}", 10 * 60000, go))


@app.route("/api/sport_records")
def api_sport():
    days = int(request.args.get("days", 30))
    start = ms_now() - days * DAY

    def go():
        if _store is not None:
            items = _store.rows("sport", "sport_records", start, ms_now())
            for it in items:
                it.setdefault("_key", it.get("key") or "sport")
            if items or days >= 3650:
                return {"ok": True, "items": items, "source": "db"}
        for d in sorted({days, 90, 365, 3650}):
            res = client.get_sport_records_all(ms_now() - d * DAY, ms_now(),
                                               full_history=(d >= 3650))
            if res and res.get("sport_records"):
                out = []
                for it in res["sport_records"]:
                    try:
                        inner = json.loads(it.get("value") or "{}")
                    except Exception:
                        inner = {}
                    inner["_key"] = it.get("key")
                    inner["_t"] = it.get("time")
                    out.append(inner)
                return {"ok": True, "items": out, "days_used": d, "source": "live"}
        return {"ok": True, "items": [], "source": "live"}

    return jsonify(cached(f"sport:{days}", 10 * 60000, go))


@app.route("/api/keys")
def api_keys():
    return jsonify({"keys": ["steps", "calories", "heart_rate", "sleep", "spo2", "stress",
                             "pai", "weight", "energy", "vo2_max", "blood_pressure",
                             "blood_sugar", "valid_stand", "intensity", "menstruation",
                             "headset", "goal"]})


# ---------------------------------------------------------------- live passthrough

def J(x):
    return json.dumps(x, separators=(",", ":"))


def call_paged(sub, mk_params, list_field, max_pages=100):
    items, nk = [], None
    for _ in range(max_pages):
        r = client.call(sub, {"data": J(mk_params(nk))})
        res = (r or {}).get("result") or {}
        items += res.get(list_field) or []
        nk = res.get("next_key")
        if not res.get("has_more") or not nk:
            return {"ok": True, "items": items, "has_more": False}
    return {"ok": True, "items": items, "has_more": True}


def _ms(arg, default=None):
    v = request.args.get(arg)
    return int(v) if v not in (None, "") else default


@app.route("/api/fitness/<key>")
def api_fitness(key):
    """Raw minute data straight from the cloud (?start=&end=)."""
    start = _ms("start", 0)
    end = _ms("end", ms_now())
    res = client.get_fitness_data_all(key, start, end, max_pages=_ms("pages", 400),
                                      full_history=start <= 0 or request.args.get("full") == "1")
    if res is None:
        return jsonify({"ok": False, "err": "null"})
    items = res["data_list"]
    return jsonify({"ok": True, "key": key, "count": len(items),
                    "items": items, "has_more": res.get("has_more")})


@app.route("/api/medical")
def api_medical():
    start = _ms("start", 0); end = _ms("end", ms_now())
    key = request.args.get("key") or None
    return jsonify(call_paged("data/get_medical_data_by_time",
                              lambda nk: {"key": key, "startTime": max(1, start), "endTime": end,
                                          "reverse": True, "next_key": nk}, "data_list"))


@app.route("/api/project")
def api_project():
    start = _ms("start", 0); end = _ms("end", ms_now())
    key = request.args.get("key") or None
    return jsonify(call_paged("data/get_project_data_by_time",
                              lambda nk: {"key": key, "startTime": max(1, start), "endTime": end,
                                          "reverse": True, "next_key": nk}, "data_list"))


@app.route("/api/stat/<key>")
def api_stat(key):
    tag = request.args.get("tag", "daily")
    start = _ms("start", 0); end = _ms("end", ms_now())
    return jsonify(call_paged("statistics/get_stat_data_by_time",
                              lambda nk: {"tag": tag, "key": key, "next_key": nk,
                                          "startTime": max(1, start), "endTime": end,
                                          "limit": 50, "reverse": True}, "data_list"))


@app.route("/api/aggregated")
def api_aggregated():
    tag = request.args.get("tag", "daily_fitness")
    key = request.args.get("key") or None
    start = _ms("start", 0); end = _ms("end", ms_now())
    return jsonify(call_paged("data/get_aggregated_fitness_data_by_time",
                              lambda nk: {"tag": tag, "key": key, "startTime": max(1, start),
                                          "endTime": end, "cursor": nk, "reverse": True}, "data_list"))


@app.route("/api/sport_summary")
def api_sport_summary():
    start = _ms("start", 0); end = _ms("end", ms_now())
    return jsonify(call_paged("statistics/scan_sport_summary",
                              lambda nk: {"startTime": max(1, start), "endTime": end,
                                          "next_key": nk}, "data_list"))


@app.route("/api/max_watermark")
def api_maxwm():
    t = _ms("type", 0)
    return jsonify(client.call("data/get_max_watermark", {"data": J({"type": t})}))


@app.route("/api/watermark/<key>")
def api_watermark(key):
    wm = _ms("wm", 0); limit = _ms("limit")
    return jsonify(client.get_fitness_data_by_watermark("", wm, limit))


@app.route("/api/latest")
def api_latest():
    keys = (request.args.get("keys") or ",".join(SCALAR_FIELDS)).split(",")
    return jsonify(client.get_latest_fitness_data([(k, 1) for k in keys]))


@app.route("/api/sport_categories")
def api_sport_cat():
    return jsonify(client.call("data/get_sport_category_list", {"data": "{}"}))


@app.route("/api/relatives/<sub>")
def api_relatives(sub):
    start = _ms("start", 0); end = _ms("end", ms_now())
    params = {"startTime": max(1, start), "endTime": end, "reverse": True}
    tgt = request.args.get("target")
    if tgt:
        params["target"] = tgt
    return jsonify(call_paged(f"relatives/get_{sub}",
                              lambda nk: dict(params, next_key=nk), "data_list"))


@app.route("/api/raw/<path:sub>")
def api_raw(sub):
    data = request.args.get("data")
    params = {"data": data} if data is not None else {}
    return jsonify(client.call(sub, params, decrypt=False))


if __name__ == "__main__":
    port = int(os.environ.get("MIH_PORT", "8567"))
    app.run(host="127.0.0.1", port=port, debug=False)
