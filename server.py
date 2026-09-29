#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mi Health 数据仪表盘后端 — 直连 hlth.io.mi.com，代理签名/加密。"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flask import Flask, jsonify, request, send_from_directory
from mihealth_client import MiHealthClient, load_config

app = Flask(__name__, static_folder=None)
WEB = os.path.dirname(os.path.abspath(__file__))
_cfg = load_config()
client = MiHealthClient(_cfg["ssecurity"], _cfg["service_token"], _cfg["cuser_id"])

DAY = 86400000
_cache = {}
def cached(key, ttl_ms, fn):
    ent = _cache.get(key)
    if ent and time.time() * 1000 - ent[0] < ttl_ms:
        return ent[1]
    val = fn()
    _cache[key] = (time.time() * 1000, val)
    return val

def ms_now():
    return int(time.time() * 1000)

def _unwrap(resp):
    """return (ok, result dict | error)"""
    if isinstance(resp, dict) and resp.get("code") == 0:
        return True, resp.get("result") or {}
    return False, resp

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

def _dedup_minute(items, fields=("steps", "calories", "bpm", "spo2")):
    """分页重叠会产生大量 (sid,time) 重复；同一分钟多个 sid 源可能各报一份，
    数值型字段取每分钟最大值，避免重复求和。"""
    by_t = {}
    for x in items:
        t = x.get("_t")
        if t is None:
            continue
        cur = by_t.get(t)
        if cur is None:
            by_t[t] = dict(x)
        else:
            for f in fields:
                if (x.get(f) or 0) > (cur.get(f) or 0):
                    cur[f] = x.get(f)
            # 非数值字段留首个非空
            for k, v in x.items():
                if k not in cur and v is not None:
                    cur[k] = v
    return sorted(by_t.values(), key=lambda x: x["_t"])

def fetch_series_range(key, start_ms, end_ms):
    res = client.get_fitness_data_all(key, start_ms, end_ms)
    if res is None:
        return {"ok": False, "err": "null"}
    items = _dedup_minute(_parse_items(res))
    return {"ok": True, "key": key, "items": items, "has_more": res.get("has_more")}

def fetch_series(key, hours):
    end = ms_now(); start = end - int(hours * 3600000)
    return fetch_series_range(key, start, end)

def today_start_ms():
    """自然日 0 点（设备上报时区 Asia/Shanghai, UTC+8）。"""
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)

@app.route("/")
def index():
    return send_from_directory(os.path.join(WEB, "static"), "index.html")

@app.route("/vendor/<path:f>")
def vendor(f):
    return send_from_directory(os.path.join(WEB, "static"), f)

@app.route("/api/series/<key>")
def api_series(key):
    hours = float(request.args.get("hours", 24))
    return jsonify(cached(f"s:{key}:{int(hours)}", 5 * 60000, lambda: fetch_series(key, hours)))

@app.route("/api/sport_records")
def api_sport():
    days = int(request.args.get("days", 30))
    def go():
        # 运动记录稀疏：窗口内为空时自动向前扩，最多到全历史
        for d in [days, 90, 365, 3650]:
            if d < days: continue
            res = client.get_sport_records_all(ms_now() - d * DAY, ms_now(),
                                               full_history=(d >= 3650))
            if res is None: continue
            if res.get("sport_records"):
                seen, out = set(), []
                for it in res["sport_records"]:
                    k = (it.get("key"), it.get("time"))
                    if k in seen: continue
                    seen.add(k)
                    try: inner = json.loads(it.get("value") or "{}")
                    except Exception: inner = {}
                    inner["_key"] = it.get("key"); inner["_t"] = it.get("time")
                    out.append(inner)
                out.sort(key=lambda x: x.get("_t") or 0)
                return {"ok": True, "items": out, "days_used": d,
                        "has_more": res.get("has_more")}
        return {"ok": True, "items": [], "days_used": days}
    return jsonify(cached(f"sport:{days}", 10 * 60000, go))

@app.route("/api/overview")
def api_overview():
    now = ms_now()
    t0 = today_start_ms()
    def grab(key, hours):
        r = fetch_series(key, hours)
        return r.get("items", []) if r.get("ok") else []
    steps24 = grab("steps", 24)
    cals24 = grab("calories", 24)
    steps_today_items = fetch_series_range("steps", t0, now).get("items", [])
    cals_today_items = fetch_series_range("calories", t0, now).get("items", [])
    hr = grab("heart_rate", 24)
    spo2 = grab("spo2", 48)
    sleep = grab("sleep", 72)
    weight = grab("weight", 90 * 24)
    pai = grab("pai", 24 * 14)
    steps_total = sum(int(x.get("steps") or 0) for x in steps_today_items)
    cal_total = round(sum(float(x.get("calories") or 0) for x in cals_today_items), 1)
    last_hr = next((x["bpm"] for x in reversed(hr) if x.get("bpm")), None)
    resting = min([x["bpm"] for x in hr if x.get("bpm")] or [None])
    last_spo2 = next((x["spo2"] for x in reversed(spo2) if x.get("spo2")), None)
    last_sleep = sleep[-1] if sleep else None
    last_weight = next((x for x in reversed(weight) if x.get("weight")), None)
    last_pai = next((x for x in reversed(pai) if x.get("pai") or x.get("value")), None)
    return jsonify({
        "ok": True, "ts": now, "today_start": t0,
        "steps_today": steps_total, "calories_today": cal_total,
        "steps_today_points": len(steps_today_items),
        "hr_last": last_hr, "hr_min_24h": resting,
        "spo2_last": last_spo2,
        "sleep_last": last_sleep,
        "weight_last": (last_weight or {}).get("weight"),
        "pai": last_pai,
        "counts": {"steps": len(steps24), "hr": len(hr), "sleep": len(sleep), "spo2": len(spo2)},
    })

@app.route("/api/keys")
def api_keys():
    return jsonify({"keys": ["steps","calories","heart_rate","sleep","spo2","stress",
        "pai","weight","energy","vo2_max","blood_pressure","blood_sugar","valid_stand",
        "intensity","menstruation","headset","goal"]})

# ---------- 全量数据接口 ----------
def J(x): return json.dumps(x, separators=(",", ":"))

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

FITNESS_KEYS = ["steps","calories","sleep","heart_rate","stress","spo2","intensity",
    "valid_stand","energy","goal","pai","blood_pressure","blood_sugar","headset",
    "weight","vo2_max","menstruation"]

@app.route("/api/fitness/<key>")
def api_fitness(key):
    """分钟级健康数据。?start=<ms>&end=<ms>&pages=<n>；start<=0 时拉全量历史"""
    start = _ms("start", 0); end = _ms("end", ms_now())
    full = start <= 0 or request.args.get("full") == "1"
    res = client.get_fitness_data_all(key, start, end, max_pages=_ms("pages", 400),
                                    full_history=full)
    if res is None: return jsonify({"ok": False, "err": "null"})
    items = _dedup_minute(_parse_items(res))
    return jsonify({"ok": True, "key": key, "count": len(items),
                    "items": items, "has_more": res.get("has_more")})

@app.route("/api/medical")
def api_medical():
    """医疗记录(ECG/血压等)。?start=&end=&key="""
    start=_ms("start",0); end=_ms("end",ms_now()); key=request.args.get("key") or None
    return jsonify(call_paged("data/get_medical_data_by_time",
        lambda nk:{"key":key,"startTime":start,"endTime":end,"reverse":True,"nextKey":nk},"data_list"))

@app.route("/api/project")
def api_project():
    """项目数据(睡眠节律/日记/经期等)。?start=&end=&key="""
    start=_ms("start",0); end=_ms("end",ms_now()); key=request.args.get("key") or None
    return jsonify(call_paged("data/get_project_data_by_time",
        lambda nk:{"key":key,"startTime":start,"endTime":end,"reverse":True,"nextKey":nk},"data_list"))

@app.route("/api/stat/<key>")
def api_stat(key):
    """统计数据(活力/能量等)。?tag=daily&start=&end="""
    tag=request.args.get("tag","daily"); start=_ms("start",0); end=_ms("end",ms_now())
    return jsonify(call_paged("statistics/get_stat_data_by_time",
        lambda nk:{"tag":tag,"key":key,"nextKey":nk,"startTime":start,"endTime":end,
                   "limit":50,"reverse":True},"data_list"))

@app.route("/api/aggregated")
def api_aggregated():
    """聚合日报(三环目标等)。?tag=daily_fitness&key=goal&start=&end="""
    tag=request.args.get("tag","daily_fitness"); key=request.args.get("key") or None
    start=_ms("start",0); end=_ms("end",ms_now())
    return jsonify(call_paged("data/get_aggregated_fitness_data_by_time",
        lambda nk:{"tag":tag,"key":key,"startTime":start,"endTime":end,
                   "cursor":nk,"reverse":True},"data_list"))

@app.route("/api/daily_goals")
def api_daily_goals():
    """健康摘要：每日目标达成（步数/热量/活动/站立）。?days=14"""
    days=_ms("days",14); start=ms_now()-days*DAY; end=ms_now()
    r=call_paged("data/get_aggregated_fitness_data_by_time",
        lambda nk:{"tag":"daily_fitness","key":"goal","startTime":start,"endTime":end,
                   "cursor":nk,"reverse":True},"data_list")
    if not r.get("ok"): return jsonify(r)
    FIELD={1:"steps",2:"calories",3:"stand_hours",4:"active_minutes"}
    seen,days_out=set(),[]
    for it in r["items"]:
        t=it.get("time")
        if t in seen: continue
        seen.add(t)
        try: v=json.loads(it.get("value") or "{}")
        except Exception: v={}
        rows={}
        for g in v.get("goal_items",[]):
            f=FIELD.get(g.get("field"),f"field_{g.get('field')}")
            rows[f]={"achieved":g.get("achieved_value"),"target":g.get("target_value")}
        sg=v.get("stand_goal") or {}
        rows["stand_hours"]={"achieved":sg.get("achieved_value"),"target":sg.get("target_value")}
        days_out.append({"time":t,"date_time":v.get("date_time"),"goals":rows})
    days_out.sort(key=lambda x:x.get("time") or 0,reverse=True)
    return jsonify({"ok":True,"count":len(days_out),"items":days_out})

@app.route("/api/sport_summary")
def api_sport_summary():
    """运动汇总。?start=&end="""
    start=_ms("start",0); end=_ms("end",ms_now())
    return jsonify(call_paged("statistics/scan_sport_summary",
        lambda nk:{"startTime":start,"endTime":end,"nextKey":nk},"data_list"))

@app.route("/api/max_watermark")
def api_maxwm():
    """数据水位(增量同步锚点)。?type=0..5"""
    t=_ms("type",0)
    r=client.call("data/get_max_watermark",{"data":J({"type":t})})
    return jsonify(r)

@app.route("/api/latest")
def api_latest():
    """各数据键最新一条。?keys=steps,sleep"""
    keys=(request.args.get("keys") or ",".join(FITNESS_KEYS)).split(",")
    dl=[{"key":k,"limit":1,"time":None} for k in keys]
    r=client.call("data/get_latest_fitness_data",{"data":J({"dataList":dl})})
    return jsonify(r)

@app.route("/api/sport_categories")
def api_sport_cat():
    return jsonify(client.call("data/get_sport_category_list",{"data":"{}"}))

@app.route("/api/relatives/<sub>")
def api_relatives(sub):
    """亲友数据: fitness_data|latest_data|aggregated_data。?start=&end=&target="""
    start=_ms("start",0); end=_ms("end",ms_now())
    params={"startTime":start,"endTime":end,"reverse":True,"nextKey":None}
    tgt=request.args.get("target");
    if tgt: params["target"]=tgt
    path=f"relatives/get_{sub}"
    return jsonify(call_paged(path,lambda nk:dict(params,nextKey=nk),"data_list"))

@app.route("/api/watermark/<key>")
def api_watermark(key):
    """按水位增量。?wm=<long>&limit=50"""
    wm=_ms("wm",0); limit=_ms("limit",50)
    r=client.call("data/get_fitness_data_by_watermark",
        {"data":J({"key":key,"waterMark":wm,"limit":limit})})
    return jsonify(r)

@app.route("/api/raw/<path:sub>")
def api_raw(sub):
    """调试透传: /api/raw/data/get_fitness_data_by_time?data=..."""
    data = request.args.get("data")
    params = {"data": data} if data is not None else {}
    return jsonify(client.call(sub, params, decrypt=False))

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8567, debug=False)
