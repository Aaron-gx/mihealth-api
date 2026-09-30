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


# ---------- 登录 / 会话管理 ----------
CFG_PATH = CFG.get("config_path")


def _apply_session(sess):
    """把新会话写回 config 并热更新 client。"""
    from login import save_session
    save_session(CFG_PATH, sess)
    client.reload_credentials(sess.get("ssecurity"), sess.get("service_token"),
                              sess.get("cuser_id"))
    _cache.clear()


def _auth_selfheal(cl, why):
    """401 自愈：优先 passToken 免密续期，其次从 adb 设备导入。"""
    print(f"[auth] {why} -> self-heal", flush=True)
    from login import LoginError, refresh_session
    cfg = load_config(CFG_PATH)
    if cfg.get("pass_token"):
        try:
            sess = refresh_session(cfg, CFG_PATH)
            cl.reload_credentials(sess["ssecurity"], sess["service_token"], sess["cuser_id"])
            _cache.clear()
            print("[auth] refreshed via passToken", flush=True)
            return True
        except LoginError as e:
            print(f"[auth] passToken refresh failed: {e}", flush=True)
    try:
        sys.path.insert(0, os.path.join(HERE, "tools"))
        import refresh_credentials as rc
        creds = rc.fetch_from_device()
        rc.write_config(creds, CFG_PATH)
        cl.reload_credentials(creds["ssecurity"], creds["service_token"], creds["cuser_id"])
        _cache.clear()
        print("[auth] refreshed from device", flush=True)
        return True
    except Exception as e:
        print(f"[auth] device import failed: {e}", flush=True)
        return False


client.on_auth_error = _auth_selfheal


@app.route("/api/login/status")
def api_login_status():
    """会话状态：能否免密续期、上次登录时间、账号 id。"""
    cfg = load_config(CFG_PATH)
    return jsonify({"ok": True, "can_refresh": bool(cfg.get("pass_token")),
                    "session_at": cfg.get("session_at"), "sid": cfg.get("sid") or "miothealth",
                    "user_id": cfg.get("user_id"), "cuser_id": cfg.get("cuser_id"),
                    "password_stored": False,
                    "modes": ["passToken 免密续期", "从 adb 设备导入", "账号密码登录"]})


@app.route("/api/login/refresh", methods=["POST"])
def api_login_refresh():
    """免密续期（用已保存的 passToken，不需要密码/模拟器）。"""
    cfg = load_config(CFG_PATH)
    if not cfg.get("pass_token"):
        return jsonify({"ok": False, "err": "config 里没有 pass_token：请先账号密码登录或从设备导入"}), 400
    from login import LoginError, refresh_session
    try:
        sess = refresh_session(cfg, CFG_PATH, proxies=cfg.get("proxies"))
    except LoginError as e:
        return jsonify({"ok": False, "err": str(e), "code": e.code}), 400
    _apply_session(sess)
    return jsonify({"ok": True, "mode": "passToken", "token_prefix": sess["service_token"][:14]})


_login_state = {"session": None, "captcha_url": None, "username": None, "2fa_context": None}


@app.route("/api/login", methods=["POST"])
def api_login_password():
    """账号密码登录（首次）。密码仅用于当次请求，不落盘；可能要求验证码。

    验证码流程：同一登录会话（requests.Session）在两次请求间保留，
    因为 Xiaomi 把验证码绑定在会话 cookie（ick）上。
    """
    body = request.get_json(force=True, silent=True) or {}
    username, password = body.get("username") or _login_state["username"], body.get("password")
    captcha, code2fa = body.get("captcha"), body.get("code")
    proxies = CFG.get("proxies") or None
    from login import (LoginError, Need2FA, finish_2fa, login_with_password,
                       new_login_session, start_2fa)

    # 第二段：带 2FA 验证码，直接走完换取 serviceToken
    if code2fa and _login_state.get("2fa_context"):
        try:
            sess = finish_2fa(_login_state["session"], code2fa, _login_state["2fa_context"])
        except LoginError as e:
            return jsonify({"ok": False, "err": str(e), "need_2fa": True,
                            "code": e.code}), 400
        _login_state.update(session=None, captcha_url=None, username=None,
                            **{"2fa_context": None})
        if not sess.get("service_token"):
            return jsonify({"ok": False, "err": "验证通过但未取得 serviceToken"}), 400
        _apply_session(sess)
        return jsonify({"ok": True, "mode": "password+2fa",
                        "pass_token_saved": bool(sess.get("pass_token")),
                        "token_prefix": sess["service_token"][:14]})

    if not (username and password):
        return jsonify({"ok": False, "err": "需要 username 与 password"}), 400
    if _login_state["session"] is None:
        _login_state["session"] = new_login_session(proxies=proxies)
    try:
        sess = login_with_password(username, password, captcha=captcha,
                                   proxies=proxies, session=_login_state["session"])
    except Need2FA as e:
        _login_state["username"] = username
        try:
            info = start_2fa(_login_state["session"], e.notification_url)
            _login_state["2fa_context"] = info["context"]
            msg = "需要二次验证：验证码已发送"
            if info.get("hint"):
                msg += f"（{info['hint']}）"
            return jsonify({"ok": False, "need_2fa": True, "err": msg,
                            "hint": info.get("hint")}), 400
        except LoginError as e2:
            _login_state["session"] = None
            return jsonify({"ok": False, "err": f"触发二次验证失败：{e2}"}), 400
    except LoginError as e:
        _login_state["username"] = username
        if e.captcha_url:
            _login_state["captcha_url"] = e.captcha_url
            return jsonify({"ok": False, "err": str(e), "code": e.code,
                            "captcha_url": "/api/login/captcha"}), 400
        _login_state["session"] = None
        return jsonify({"ok": False, "err": str(e), "code": e.code}), 400
    _login_state.update(session=None, captcha_url=None, username=None, **{"2fa_context": None})
    if not sess.get("service_token"):
        return jsonify({"ok": False, "err": "登录成功但未取得 serviceToken（可能需要二次验证）"}), 400
    _apply_session(sess)
    return jsonify({"ok": True, "mode": "password",
                    "pass_token_saved": bool(sess.get("pass_token")),
                    "token_prefix": sess["service_token"][:14]})


@app.route("/api/login/captcha")
def api_login_captcha():
    """代理小米验证码图片（必须用同一登录会话的 cookie 取，浏览器直连会失配）。"""
    url = _login_state.get("captcha_url")
    if not url or _login_state["session"] is None:
        return jsonify({"ok": False, "err": "没有待处理的验证码"}), 404
    if url.startswith("/"):
        url = "https://account.xiaomi.com" + url
    r = _login_state["session"].get(url, timeout=25)
    from flask import Response
    return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))


@app.route("/api/login/import_device", methods=["POST"])
def api_login_import_device():
    """从 adb 设备（已登录的 App）导入会话，含 passToken —— 之后即可免密续期。"""
    try:
        sys.path.insert(0, os.path.join(HERE, "tools"))
        import refresh_credentials as rc
        creds = rc.fetch_from_device(serial=request.args.get("serial") or None)
        rc.write_config(creds, CFG_PATH)
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)}), 400
    client.reload_credentials(creds["ssecurity"], creds["service_token"], creds["cuser_id"])
    _cache.clear()
    return jsonify({"ok": True, "mode": "device",
                    "pass_token_saved": bool(creds.get("pass_token")),
                    "token_prefix": (creds.get("service_token") or "")[:14]})


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
    stype = request.args.get("type")          # 运动类型过滤: outdoor_running / pool_swimming ...
    start = ms_now() - days * DAY

    def go():
        if _store is not None:
            items = _store.rows("sport", stype, start, ms_now())
            if items or stype or days >= 3650:
                return {"ok": True, "items": items, "type": stype, "source": "db"}
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
                             "headset", "goal"],
                    "families": ["fitness", "sport", "diet", "goal", "medical", "project"]})


# ---------- 通用多维查询（本地库） ----------
@app.route("/api/families")
def api_families():
    """库内数据清单：family / key / 行数 / 时间范围 + 数据源。

    全表分组约数秒，缓存 5 分钟（同步后会自动失效）。
    """
    if not _store:
        return jsonify({"ok": False, "err": "no db (run sync.py backfill)"}), 404
    return jsonify(cached("families", 5 * 60000, _families_payload))


def _families_payload():
    by_family = {}
    for r in _store.stats(dedup=True):
        f = by_family.setdefault(r["family"], {"family": r["family"], "rows": 0,
                                               "first": r["first"], "last": r["last"], "keys": []})
        f["rows"] += r["count"]
        f["keys"].append({"key": r["key"], "count": r["count"],
                          "first": r["first"], "last": r["last"]})
        f["first"] = min(f["first"] or r["first"] or 0, r["first"] or f["first"] or 0)
        f["last"] = max(f["last"] or 0, r["last"] or 0)
    return {"ok": True,
            "total_rows": _store.count(dedup=True),
            "raw_rows": _store.count(),
            "dedup": "materialized",
            "families": list(by_family.values()),
            "sources": _store.sources()}


@app.route("/api/db/<family>")
def api_db_family(family):
    """任意数据族的多维查询。

    ?key=     数据键/运动类型（可逗号分隔，省略=全部）
    ?start=&end=  毫秒时间戳（或 seconds=1 表示秒）
    ?sid=     只看某个数据源（手表/手机）
    ?dedup=0  返回原始行（含各源重复），默认 1=去重
    ?order=desc&limit=  倒序/条数上限
    """
    if not _store:
        return jsonify({"ok": False, "err": "no db (run sync.py backfill)"}), 404
    sec = request.args.get("seconds") == "1"
    start, end = _ms("start"), _ms("end")
    if sec:
        start = start * 1000 if start is not None else None
        end = end * 1000 if end is not None else None
    keys = [k for k in (request.args.get("key") or "").split(",") if k]
    dedup = request.args.get("dedup", "1") != "0"
    order = request.args.get("order", "asc")
    limit = _ms("limit")
    sid = request.args.get("sid")
    meta_only = request.args.get("fields") == "meta"
    if keys:
        # push the limit into SQL per key when a single key is requested;
        # multi-key merges keep the caller's limit as a post-slice
        per_key = limit if len(keys) == 1 else None
        items = []
        for k in keys:
            items += _store.rows(family, k, start, end, dedup=dedup, sid=sid,
                                 order=order, limit=per_key)
        items.sort(key=lambda x: x.get("_t") or 0, reverse=(order == "desc"))
        if limit:
            items = items[:limit]
    else:
        items = _store.rows(family, None, start, end, dedup=dedup, sid=sid,
                            order=order, limit=limit)
    if meta_only:
        items = [{k: v for k, v in it.items() if k.startswith("_")} for it in items]
    return jsonify({"ok": True, "family": family, "count": len(items),
                    "dedup": dedup, "items": items})


@app.route("/api/db/<family>/keys")
def api_db_keys(family):
    if not _store:
        return jsonify({"ok": False, "err": "no db"}), 404
    return jsonify(cached(f"keys:{family}", 5 * 60000,
                          lambda: {"ok": True, "family": family,
                                   "keys": _store.keys_of(family),
                                   "sources": _store.sources(family)}))


@app.route("/api/agg/<family>/<key>")
def api_agg(family, key):
    """服务端聚合（不解码行，一次 SQL）：任意键 × 任意内层字段 × 任意桶宽。

    ?field=steps&agg=sum|max|min|avg|count&bucket=3600&start=&end=&sid=&dedup=0|1
    例：/api/agg/fitness/steps?field=steps&agg=sum&bucket=86400&start=...&end=...
    """
    if not _store:
        return jsonify({"ok": False, "err": "no db"}), 404
    field = request.args.get("field")
    if not field:
        return jsonify({"ok": False, "err": "need field=<inner json field>"}), 400
    points = _store.aggregate(family, key, field,
                              bucket_s=_ms("bucket", 3600),
                              agg=request.args.get("agg", "sum"),
                              start_ms=_ms("start"), end_ms=_ms("end"),
                              sid=request.args.get("sid"),
                              dedup=request.args.get("dedup", "1") != "0")
    return jsonify({"ok": True, "family": family, "key": key, "field": field,
                    "agg": request.args.get("agg", "sum"),
                    "bucket": _ms("bucket", 3600), "count": len(points),
                    "points": points})


@app.route("/api/export.csv")
def api_export_csv():
    """任意族/键导出 CSV：/api/export.csv?family=fitness&key=steps&start=&end="""
    if not _store:
        return jsonify({"ok": False, "err": "no db"}), 404
    import tempfile
    family = request.args.get("family", "fitness")
    key = request.args.get("key")
    path = os.path.join(tempfile.gettempdir(), f"mih_export_{family}_{key or 'all'}.csv")
    n = _store.to_csv(path, family, key, _ms("start"), _ms("end"),
                      dedup=request.args.get("dedup", "1") != "0")
    return send_from_directory(os.path.dirname(path), os.path.basename(path),
                               as_attachment=True)


@app.route("/api/sport_types")
def api_sport_types():
    """运动类型清单（库内实测有数据的）。"""
    if _store:
        return jsonify({"ok": True, "types": _store.keys_of("sport"),
                        "sources": _store.sources("sport")})
    return jsonify(call_paged("data/get_sport_records_by_time",
                              lambda nk: {"startTime": 1, "endTime": ms_now(),
                                          "next_key": nk, "limit": 100, "reverse": True},
                              "sport_records", max_pages=1))


@app.route("/api/sport_detail")
def api_sport_detail():
    """单条运动扩展数据（轨迹引用 route_info / 课程 course_data）。
    ?sid=&key=outdoor_running&start=&end=（秒）"""
    sid = request.args.get("sid")
    key = request.args.get("key")
    start = _ms("start"); end = _ms("end")
    if not (sid and key and start and end):
        return jsonify({"ok": False, "err": "need sid,key,start,end (seconds)"}), 400
    return jsonify(client.get_sport_operational_data(sid, key, start, end))


@app.route("/api/routes")
def api_routes():
    """轨迹库（GPS 轨迹列表）。?start=<ms>&limit=&origin_type="""
    start = _ms("start", 0)
    limit = _ms("limit", 50)
    ot = _ms("origin_type", 0)
    return jsonify(client.get_routes_list(start, limit, True, ot))


@app.route("/api/routes/info")
def api_routes_info():
    ids = [i for i in (request.args.get("ids") or "").split(",") if i]
    if not ids:
        return jsonify({"ok": False, "err": "need ids=route_id,..."}), 400
    return jsonify(client.get_routes_info(ids))


@app.route("/api/fds_url")
def api_fds_url():
    """FDS 预签名下载 URL。?sid=<数据源id>&suffix=csv|route&time=<秒>"""
    items = []
    for suffix in (request.args.get("suffix") or "csv").split(","):
        items.append({"suffix": suffix, "timeStamp": _ms("time", int(time.time()))})
    return jsonify(client.gen_fds_download_url(request.args.get("sid") or "", items))


@app.route("/api/diet")
def api_diet():
    """饮食记录（秒级时间窗）。本地库优先，未同步时走实时。"""
    days = int(request.args.get("days", 30))
    start_ms = ms_now() - days * DAY
    if _store:
        items = _store.rows("diet", None, start_ms, ms_now())
        return jsonify({"ok": True, "items": items, "source": "db", "count": len(items)})
    res = client.get_diet_records_by_time(start_ms // 1000, ms_now() // 1000)
    return jsonify({"ok": True, "items": res["diet_records"], "source": "live"})


@app.route("/api/watermark_feed/<family>")
def api_watermark_feed(family):
    """通用水位流：?wm=<游标>，walk 到最新。
    family: fitness | sport | medical | project"""
    import sync as _sync
    spec = {
        "fitness": ("data/get_fitness_data_by_watermark", "data_list", 0),
        "sport": ("data/get_sport_records_by_watermark", "sport_records", 1),
        "medical": ("data/get_medical_data_by_watermark", "data_list", 5),
        "project": ("data/get_project_data_by_watermark", "data_list", 4),
    }.get(family)
    if not spec:
        return jsonify({"ok": False, "err": "unknown family"}), 400
    sub, field, wtype = spec
    wm = _ms("wm")
    if not wm:
        return jsonify({"ok": True, "cursor": client.get_max_watermark(wtype),
                        "items": [], "note": "no wm given: cursor seeded at head"})
    items, new_wm, more = client.sync_by_watermark(sub, field, wm,
                                                   max_pages=_ms("pages", 50))
    return jsonify({"ok": True, "cursor": new_wm, "count": len(items),
                    "has_more": more, "items": items})


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
