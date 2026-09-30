#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米账号登录（passport）→ 换取米健康 serviceToken。

两条路径：

1. **免密（推荐）**：已有 `pass_token` 时直接换票，不需要密码、不需要模拟器。
   ```
   GET  https://account.xiaomi.com/pass/serviceLogin?sid=<sid>&_json=true   (带 passToken/cUserId/userId cookie)
        -> {"code":0, "ssecurity":..., "location":"https://sts-hlth.io.mi.com/healthapp/sts?..."}
   GET  <location>  -> Set-Cookie: serviceToken=...   ← 米健康接口就用它
   ```

2. **账号密码**（首次登录）：SDK 里 `XMPassport.loginByPassword` 的实现——
   `hash = MD5( MD5(password).upper() ).upper()`，POST `serviceLoginAuth2`
   带 `user/hash/sid/_sign/qs/callback`，可能要求验证码（`icode`）或二次验证。
   成功后拿到 `pass_token`，之后走免密路径即可（passToken 有效期约 1~2 个月）。

安全约定：密码**只用于当次登录请求**，不写入磁盘；落盘的是 `pass_token`（可撤销、
可过期，等价于 App 里的登录态）。配置文件权限建议 600。
"""
import hashlib, json, os, re, time

import requests

ACCOUNT_HOST = "https://account.xiaomi.com"
SID_HEALTH = "miothealth"          # 米健康（回调 https://sts-hlth.io.mi.com/healthapp/sts）
SID_IOT = "xiaomiio"               # 米家（对照用）
UA = ("MiFit/3.59.0 (Linux; U; Android 9; zh_CN; MI 8 Build/PKQ1.180729.001) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/80 Mobile Safari/537.36")


class LoginError(Exception):
    def __init__(self, msg, code=None, captcha_url=None, raw=None):
        super().__init__(msg)
        self.code = code
        self.captcha_url = captcha_url
        self.raw = raw or {}


def _json_body(text):
    """passport 的 JSON 响应前面常有 '&&&START&&&' 前缀。"""
    i = text.find("{")
    return json.loads(text[i:]) if i >= 0 else {}


def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def hash_password(password: str) -> str:
    """XMPassport.loginByPassword 的回退算法（无 EUI 加密器时）。"""
    return md5_upper(md5_upper(password))


def _session(proxies=None):
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if proxies:
        s.proxies.update(proxies)
    return s


def service_login(S, sid=SID_HEALTH):
    """第一步：拿 _sign / qs / callback。"""
    r = S.get(f"{ACCOUNT_HOST}/pass/serviceLogin",
              params={"sid": sid, "_json": "true"}, timeout=20)
    j = _json_body(r.text)
    if not j.get("_sign"):
        raise LoginError(f"serviceLogin 未返回 _sign（HTTP {r.status_code}）", raw=j)
    return j


def exchange_pass_token(pass_token, cuser_id, user_id, sid=SID_HEALTH, proxies=None):
    """免密换票：pass_token -> {ssecurity, service_token, cuser_id, user_id}。

    注意调用序列（实测）：serviceLogin 必须**带 passToken cookie** 发一次，
    它才会返回 code=0 与 STS location；不带 cookie 的匿名调用即便返回 location，
    跟随后也拿不到 serviceToken。STS 那一步不要自动跟随跳转。
    """
    S = _session(proxies)
    ck = {"passToken": pass_token, "cUserId": cuser_id,
          "userId": "" if user_id is None else str(user_id)}
    for k, v in ck.items():
        if v:
            S.cookies.set(k, v, domain=".account.xiaomi.com")
    r = S.get(f"{ACCOUNT_HOST}/pass/serviceLogin",
              params={"sid": sid, "_json": "true"}, cookies=ck, timeout=20)
    j = _json_body(r.text)
    if j.get("code") != 0 or not j.get("location"):
        raise LoginError(f"passToken 无效或已过期（code={j.get('code')} {j.get('desc')}）", raw=j)

    r2 = S.get(j["location"], cookies=ck, allow_redirects=False, timeout=20)
    token = (S.cookies.get("serviceToken", domain="sts-hlth.io.mi.com")
             or S.cookies.get("serviceToken"))
    if not token:
        m = re.search(r"serviceToken=([^;]+)", r2.headers.get("Set-Cookie", ""))
        token = m.group(1) if m else None
    if not token:
        raise LoginError("STS 回调未返回 serviceToken", raw={"status": r2.status_code})
    return {"ssecurity": j.get("ssecurity"), "service_token": token,
            "cuser_id": j.get("cUserId") or cuser_id, "user_id": j.get("userId") or user_id,
            "sid": sid, "ts": int(time.time())}


def login_with_password(username, password, sid=SID_HEALTH, captcha=None, ick=None,
                        device_id=None, proxies=None):
    """账号密码登录（首次）。返回 session（含 pass_token）；可能抛 LoginError 且带 captcha_url。"""
    S = _session(proxies)
    j = service_login(S, sid)
    data = {"user": username, "hash": hash_password(password), "sid": sid,
            "_json": "true", "_sign": j["_sign"], "qs": j.get("qs") or f"?sid={sid}&_json=true",
            "callback": j.get("callback") or ""}
    if captcha:
        data["icode"] = captcha
    if ick:
        data["ick"] = ick
    if device_id:
        data["deviceId"] = md5_upper(device_id)      # CloudCoder.hashDeviceInfo 同族
    r = S.post(f"{ACCOUNT_HOST}/pass/serviceLoginAuth2", data=data, timeout=25)
    j2 = _json_body(r.text)
    code = j2.get("code")
    if code == 0 or j2.get("location"):
        # 第三步：跟 STS 回调拿 serviceToken
        loc = j2.get("location")
        token = None
        if loc:
            r3 = S.get(loc, allow_redirects=True, timeout=20)
            token = S.cookies.get("serviceToken")
            if not token:
                m = re.search(r"serviceToken=([^;]+)", r3.headers.get("Set-Cookie", ""))
                token = m.group(1) if m else None
        return {"ssecurity": j2.get("ssecurity"), "service_token": token,
                "cuser_id": j2.get("cUserId"), "user_id": j2.get("userId"),
                "pass_token": j2.get("passToken"), "sid": sid, "ts": int(time.time())}
    raise LoginError(j2.get("desc") or f"登录失败 code={code}", code=code,
                     captcha_url=j2.get("captchaUrl"), raw=j2)


# ---------------------------------------------------------------- config 落盘

def save_session(cfg_path, session, keep=None):
    """把会话写入 config.json（不含密码）。keep 里的原字段保留。"""
    cfg = {}
    if cfg_path and os.path.exists(cfg_path):
        try:
            cfg = json.load(open(cfg_path, encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["ssecurity"] = session.get("ssecurity") or cfg.get("ssecurity")
    cfg["service_token"] = session.get("service_token") or cfg.get("service_token")
    cfg["cuser_id"] = session.get("cuser_id") or cfg.get("cuser_id")
    if session.get("user_id"):
        cfg["user_id"] = str(session["user_id"])
    if session.get("pass_token"):
        cfg["pass_token"] = session["pass_token"]
    cfg["session_at"] = session.get("ts") or int(time.time())
    cfg["sid"] = session.get("sid", SID_HEALTH)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(cfg_path, 0o600)
    except Exception:
        pass
    return cfg_path


def refresh_session(cfg, cfg_path=None):
    """用 config 里的 pass_token 免密续期；成功返回新 session。"""
    if not cfg.get("pass_token") or not cfg.get("cuser_id"):
        raise LoginError("config 里没有 pass_token，无法免密续期（请先账号密码登录一次）")
    s = exchange_pass_token(cfg["pass_token"], cfg["cuser_id"], cfg.get("user_id") or "")
    if cfg_path:
        save_session(cfg_path, s)
    return s


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="小米账号登录 → 米健康 serviceToken")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--refresh", action="store_true", help="用已存的 pass_token 免密续期")
    ap.add_argument("--user", help="小米账号（手机号/邮箱/ID）")
    ap.add_argument("--password", help="密码（仅当次使用，不落盘）")
    ap.add_argument("--captcha", help="验证码（若上一步要求）")
    ap.add_argument("--sid", default=SID_HEALTH)
    a = ap.parse_args()
    cfg = {}
    if os.path.exists(a.config):
        cfg = json.load(open(a.config, encoding="utf-8"))
    try:
        if a.refresh:
            s = refresh_session(cfg, a.config)
            print("免密续期成功，serviceToken:", (s.get("service_token") or "")[:16], "...")
        elif a.user and a.password:
            s = login_with_password(a.user, a.password, a.sid, a.captcha)
            save_session(a.config, s)
            print("登录成功，已写入", a.config)
            print("  pass_token:", (s.get("pass_token") or "")[:16], "...（之后可免密续期）")
            print("  serviceToken:", (s.get("service_token") or "")[:16], "...")
        else:
            print("用法: python login.py --refresh  或  python login.py --user <账号> --password <密码>")
    except LoginError as e:
        print("失败:", e, "| code:", e.code, "| 验证码:", e.captcha_url)
