#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米运动健康 全量健康数据导出 — 所有云端数据落盘 tools/export/*.json（config.json 放仓库根目录）"""
import json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from mihealth_client import MiHealthClient, load_config

_cfg = load_config()
SSECURITY = _cfg["ssecurity"]
SERVICE_TOKEN = _cfg["service_token"]
CUSER_ID = _cfg["cuser_id"]

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "export")
os.makedirs(OUT, exist_ok=True)
client = MiHealthClient(SSECURITY, SERVICE_TOKEN, CUSER_ID)

FITNESS_KEYS = ["steps","calories","sleep","heart_rate","stress","spo2","intensity",
    "valid_stand","energy","goal","pai","blood_pressure","blood_sugar","headset",
    "weight","vo2_max","menstruation"]

def J(x): return json.dumps(x, separators=(",", ":"))
manifest = {"exported_at": int(time.time()*1000), "datasets": {}}

def save(name, obj, count=None):
    p = os.path.join(OUT, name + ".json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    n = count if count is not None else len(obj.get("data_list") or obj.get("sport_records") or obj.get("items") or [])
    manifest["datasets"][name] = {"count": n, "bytes": os.path.getsize(p)}
    print(f"[saved] {name}: {n} items, {manifest['datasets'][name]['bytes']//1024}KB", flush=True)

now = int(time.time()*1000)

# 1) fitness 全部键 × 全历史
for key in FITNESS_KEYS:
    try:
        res = client.get_fitness_data_all(key, 1, now, max_pages=3000, full_history=True)
        save("fitness_" + key, {"data_list": res["data_list"]})
    except Exception as e:
        print(f"[skip] fitness {key}: {e}", flush=True)
    time.sleep(0.3)

# 2) 医疗
try:
    res = client.get_fitness_data_all("medical", 1, now, max_pages=500, full_history=True)
    # medical 用专门端点
    items, nk, pages = [], None, 0
    while pages < 500:
        r = client.call("data/get_medical_data_by_time",
            {"data": J({"key":None,"startTime":1,"endTime":now,"reverse":True,"next_key":nk})})
        res2 = (r or {}).get("result") or {}
        items += res2.get("data_list") or []
        nk = res2.get("next_key"); pages += 1
        if not res2.get("has_more") or not nk: break
    save("medical", {"data_list": items})
except Exception as e: print("[skip] medical:", e, flush=True)

# 3) 项目数据
try:
    items, nk, pages = [], None, 0
    while pages < 500:
        r = client.call("data/get_project_data_by_time",
            {"data": J({"key":None,"startTime":1,"endTime":now,"reverse":True,"next_key":nk})})
        res2 = (r or {}).get("result") or {}
        items += res2.get("data_list") or []
        nk = res2.get("next_key"); pages += 1
        if not res2.get("has_more") or not nk: break
    save("project_data", {"data_list": items})
except Exception as e: print("[skip] project:", e, flush=True)

# 4) 运动记录
try:
    res = client.get_sport_records_all(1, now, max_pages=500, full_history=True)
    save("sport_records", {"sport_records": res["sport_records"]})
except Exception as e: print("[skip] sport:", e, flush=True)

# 5) 运动汇总
try:
    items, nk, pages = [], None, 0
    while pages < 200:
        r = client.call("statistics/scan_sport_summary",
            {"data": J({"startTime":1,"endTime":now,"next_key":nk})})
        res2 = (r or {}).get("result") or {}
        items += res2.get("data_list") or []
        nk = res2.get("next_key"); pages += 1
        if not res2.get("has_more") or not nk: break
    save("sport_summary", {"data_list": items})
except Exception as e: print("[skip] sport_summary:", e, flush=True)

# 6) 聚合日报
try:
    items, nk, pages = [], None, 0
    while pages < 200:
        r = client.call("data/get_aggregated_fitness_data_by_time",
            {"data": J({"tag":"daily","key":None,"startTime":1,"endTime":now,"cursor":nk,"reverse":True})})
        res2 = (r or {}).get("result") or {}
        items += res2.get("data_list") or []
        nk = res2.get("next_key"); pages += 1
        if not res2.get("has_more") or not nk: break
    save("aggregated_daily", {"data_list": items})
except Exception as e: print("[skip] aggregated:", e, flush=True)

# 7) 统计数据
for key in ["energy","steps","sleep","vitality","pai"]:
    try:
        items, nk, pages = [], None, 0
        while pages < 200:
            r = client.call("statistics/get_stat_data_by_time",
                {"data": J({"tag":"daily","key":key,"next_key":nk,"startTime":1,"endTime":now,"limit":50,"reverse":True})})
            res2 = (r or {}).get("result") or {}
            items += res2.get("data_list") or []
            nk = res2.get("next_key"); pages += 1
            if not res2.get("has_more") or not nk: break
        save("stat_" + key, {"data_list": items})
    except Exception as e: print(f"[skip] stat {key}:", e, flush=True)

# 8) 类别与水位等元数据
try:
    save("meta_sport_categories", client.call("data/get_sport_category_list",{"data":"{}"}))
    save("meta_max_watermark", client.call("data/get_max_watermark",{"data":J({"type":0})}))
except Exception as e: print("[skip] meta:", e, flush=True)

with open(os.path.join(OUT, "_manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print("DONE ->", OUT, flush=True)
