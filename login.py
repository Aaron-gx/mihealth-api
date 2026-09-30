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
    """passport/identity 的 JSON 响应前面常带 '&&&START&&&' 前缀（r.json() 会解析失败）。"""
    i = (text or "").find("{")
    if i < 0:
        return {}
    try:
        return json.loads(text[i:])
    except Exception:
        return {}


def _req(session, method, url, tries=3, **kw):
    """带重试的请求：passport/STS 直连偶发超时很常见。"""
    kw.setdefault("timeout", 25)
    last = None
    for attempt in range(tries):
        try:
            return getattr(session, method)(url, **kw)
        except requests.RequestException as e:
            last = e
            time.sleep(1 + attempt)
    raise LoginError(f"网络请求失败（{tries} 次）：{type(last).__name__}: {last}")


def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def hash_password(password: str) -> str:
    """XMPassport.loginByPassword 的明文回退算法：**单次** MD5 大写。

    反编译 `XMPassport.loginByPassword` 为
        if (encryptor == null) hash = CloudCoder.getMd5DigestUpperCase(password)
    与社区在用实现（Xiaomi-cloud-tokens-extractor）一致：
        hashlib.md5(password.encode()).hexdigest().upper()
    注意网上流传的"双重 MD5"是另一套（多为某些 Web 端），用错会被判 70016 登录验证失败。
    """
    return md5_upper(password)


def hash_password_variants(password: str):
    """排错用：服务端若拒绝，可依次尝试这些变体（谨慎，避免多次失败触发风控）。"""
    h1 = md5_upper(password)
    return {
        "md5_upper": h1,                                     # SDK / 社区在用（默认）
        "md5_md5_upper_upper": md5_upper(h1),                # 双重（部分 Web 端）
        "md5_lower": hashlib.md5(password.encode()).hexdigest(),
    }


SDK_VERSION = "accountsdk-18.8.15"      # 必须的 sdkVersion cookie，缺了会被判 70016
AGENT = ("XiaomiHealthApi-ABCDE APP/com.xiaomi.mihome APPV/10.5.201")


def _session(proxies=None, device_id=None):
    """passport 要求客户端自称一个 SDK 版本并携带 deviceId，否则密码登录会被拒绝。"""
    s = requests.Session()
    s.headers.update({"User-Agent": AGENT,
                      "Content-Type": "application/x-www-form-urlencoded"})
    for dom in ("mi.com", "xiaomi.com"):
        s.cookies.set("sdkVersion", SDK_VERSION, domain=dom)
        if device_id:
            s.cookies.set("deviceId", device_id, domain=dom)
    if proxies:
        s.proxies.update(proxies)
    return s


def service_login(S, sid=SID_HEALTH):
    """第一步：拿 _sign / qs / callback。"""
    r = _req(S, "get", f"{ACCOUNT_HOST}/pass/serviceLogin",
             params={"sid": sid, "_json": "true"})
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
    r = _req(S, "get", f"{ACCOUNT_HOST}/pass/serviceLogin",
             params={"sid": sid, "_json": "true"}, cookies=ck)
    j = _json_body(r.text)
    if j.get("code") != 0 or not j.get("location"):
        raise LoginError(f"passToken 无效或已过期（code={j.get('code')} {j.get('desc')}）", raw=j)

    r2 = _req(S, "get", j["location"], cookies=ck, allow_redirects=False)
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


def new_login_session(proxies=None, device_id=None):
    """建立带 sdkVersion/deviceId cookie 的会话；验证码/2FA 流程需要复用它。"""
    s = _session(proxies, device_id or _device_id())
    s.__dict__["_mih_device_id"] = md5_upper(device_id or _device_id())
    return s


def login_with_password(username, password, sid=SID_HEALTH, captcha=None, ick=None,
                        device_id=None, proxies=None, hash_variant="md5_upper",
                        locale="zh_CN", session=None):
    """账号密码登录（首次）。返回 session（含 pass_token）；可能抛 LoginError 且带 captcha_url。

    字段名与发送位置对齐 SDK + 社区在用实现：
      * `hash`      = MD5(password).upper()          （单次，见 hash_password）
      * `captCode`  = 验证码（**不是** icode），`ick` 在 cookie 里
      * 字段放在 **query**（社区实现 post(params=fields) 验证可用）
      * deviceId 走 cookie（SDK 的 addDeviceIdInCookies），非必填
    """
    S = session or _session(proxies, device_id or _device_id())
    # ① 取 _sign / qs / callback（带 userId cookie）
    r1 = _req(S, "get", f"{ACCOUNT_HOST}/pass/serviceLogin",
              params={"sid": sid, "_json": "true"}, cookies={"userId": username})
    j = _json_body(r1.text)
    if not j.get("_sign"):
        raise LoginError(f"serviceLogin 未返回 _sign（HTTP {r1.status_code}）", raw=j)

    # ② 提交密码（字段放 query，与在用实现一致）
    fields = {"user": username, "hash": hash_password_variants(password)[hash_variant],
              "sid": sid, "_json": "true", "_sign": j["_sign"],
              "qs": j.get("qs") or f"?sid={sid}&_json=true",
              "callback": j.get("callback") or "", "locale": locale}
    if captcha:
        fields["captCode"] = captcha
    cookies = {"ick": ick} if ick else {}
    r2 = _req(S, "post", f"{ACCOUNT_HOST}/pass/serviceLoginAuth2", params=fields,
              cookies=cookies, allow_redirects=False)
    j2 = _json_body(r2.text)

    # 需要验证码：把验证码图片地址回传给调用方（网页端展示给人填）
    if j2.get("captchaUrl"):
        raise LoginError("需要验证码", code=j2.get("code"),
                         captcha_url=j2["captchaUrl"], raw=j2)
    # 需要二次验证（邮件/短信）
    if not (isinstance(j2.get("ssecurity"), str) and len(j2["ssecurity"]) > 4):
        if j2.get("notificationUrl"):
            raise Need2FA(j2["notificationUrl"], raw=j2)
        raise LoginError(j2.get("desc") or f"登录失败 code={j2.get('code')}",
                         code=j2.get("code"), captcha_url=None, raw=j2)

    # ③ 跟随 STS 回调拿 serviceToken
    token = None
    if j2.get("location"):
        r3 = _req(S, "get", j2["location"], allow_redirects=True)
        token = S.cookies.get("serviceToken") or S.cookies.get("serviceToken", domain="sts-hlth.io.mi.com")
        if not token:
            m = re.search(r"serviceToken=([^;]+)", r3.headers.get("Set-Cookie", ""))
            token = m.group(1) if m else None
    return {"ssecurity": j2.get("ssecurity"), "service_token": token,
            "cuser_id": j2.get("cUserId"), "user_id": j2.get("userId"),
            "pass_token": j2.get("passToken"), "sid": sid, "ts": int(time.time())}


class Need2FA(LoginError):
    """账号需要二次验证：start_2fa 触发验证码 → finish_2fa 提交。"""
    def __init__(self, notification_url, raw=None):
        super().__init__("账号需要二次验证（2FA）", raw=raw)
        self.notification_url = notification_url


IDENTITY = "https://account.xiaomi.com/identity"

# identity/* 是"App 侧"端点：UA 必须像米家 App（带 DeviceId/UserId 段），
# 否则会返回空响应体（实测拿到 {} 就是这个原因）。deviceId 也要显式带 cookie。
UA_APP = ("MiHome/11.3.203 (com.xiaomi.mihome; build:11.3.203; Android 9) "
          "APP/com.xiaomi.mihome APPV/11.3.203 DeviceId/{dev} UserId/{uid} "
          "Platform/Android Region/CN L/zh_CN")


def _identity_headers(session):
    dev = session.__dict__.get("_mih_device_id") or ""
    uid = session.__dict__.get("_mih_user_id") or ""
    return {"User-Agent": UA_APP.format(dev=dev, uid=uid),
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://account.xiaomi.com/"}


def _identity_cookies(session):
    ck = {}
    dev = session.__dict__.get("_mih_device_id")
    if dev:
        ck["deviceId"] = dev
    return ck


def _id_call(session, method, url, step, **kw):
    """identity 步骤的调用：带 App UA/cookie，失败时把 HTTP 状态与原始体带到错误里。"""
    kw.setdefault("headers", _identity_headers(session))
    kw.setdefault("cookies", _identity_cookies(session))
    r = _req(session, method, url, **kw)
    text = (r.text or "").strip()
    if not text:
        raise LoginError(f"[{step}] 服务端返回空响应（HTTP {r.status_code}）——通常是 UA/deviceId 不被接受",
                         raw={"status": r.status_code, "step": step})
    return r


def start_2fa(session, notification_url, sid=SID_HEALTH, locale="zh_CN"):
    """触发二次验证码。返回 {context, flag, method, hint}，给 finish_2fa 用。

    实测要点（`identity/list` 的 flag 决定通道：**4=手机短信，8=邮箱**）：
      GET  authStart(notificationUrl)           建立验证会话
      GET  /identity/list?context=...&supportedMask=0   → flag
      GET  /identity/auth/verify{Phone|Email}?_flag=<flag>&_json=true   触发
      POST /identity/auth/sendPhoneTicket (手机时)       真正下发短信
    此前按固定"邮箱"通道实现，账号要求 flag=4 时会返回
      {"code":2,"flag":4,"options":[4],"option":4} —— 即"请改用手机验证"。
    """
    from urllib.parse import parse_qs, urlparse
    if not notification_url.startswith("http"):
        notification_url = ACCOUNT_HOST + notification_url
    _id_call(session, "get", notification_url, "authStart")
    q = parse_qs(urlparse(notification_url).query)
    ctx = (q.get("context") or [""])[0]
    sid = (q.get("sid") or [sid])[0]
    if q.get("userId"):
        session.__dict__["_mih_user_id"] = q["userId"][0]

    r = _id_call(session, "get", f"{IDENTITY}/list", "identity/list",
                 params={"sid": sid, "supportedMask": "0", "_locale": locale, "context": ctx})
    idata = _json_body(r.text)      # 注意 &&&START&&& 前缀，不能直接 r.json()
    flag = idata.get("flag", 4)
    method = "Email" if flag == 8 else "Phone"

    _id_call(session, "get", f"{IDENTITY}/auth/verify{method}", f"verify{method}(trigger)",
             params={"_flag": str(flag), "_json": "true"})
    if method == "Phone":       # 手机通道需要真正触发下发
        _id_call(session, "post", f"{IDENTITY}/auth/sendPhoneTicket", "sendPhoneTicket",
                 data={"retry": "0", "icode": "", "_json": "true"})

    hint = ""
    for k in ("mask", "notification", "email", "phone", "address"):
        if idata.get(k):
            hint = idata[k]
            break
    return {"context": ctx, "flag": flag, "method": method, "hint": hint, "raw": idata}


def finish_2fa(session, code, context, flag=4, sid=SID_HEALTH, locale="zh_CN"):
    """提交二次验证码并完成登录。

    POST /identity/auth/verify{Phone|Email}?_dc=<ms>  {_flag, ticket, trust:false}
      → 响应里的 location 先访问一次（建立已认证会话）
      → **重做 serviceLogin**（此时返回 code=0，带 ssecurity/passToken/location）
      → 跟随 STS 回调拿 serviceToken
    """
    method = "Email" if flag == 8 else "Phone"
    r = _id_call(session, "post", f"{IDENTITY}/auth/verify{method}", f"verify{method}(submit)",
                 params={"_dc": str(int(time.time() * 1000)), "_flag": str(flag),
                         "_json": "true", "sid": sid, "context": context},
                 data={"_flag": str(flag), "ticket": code.strip(),
                       "trust": "false", "_json": "true"})
    vresp = _json_body(r.text)      # 同上：小米响应带 &&&START&&& 前缀
    if vresp.get("code") not in (0, None) and not vresp.get("location"):
        raise LoginError(f"验证码校验未通过（code={vresp.get('code')} {vresp.get('desc') or ''}）",
                         code=vresp.get("code"), raw=vresp)
    loc = vresp.get("location") or r.headers.get("Location")
    if not loc:
        m = re.search(r"location\s*[:=]\s*[\"']([^\"']+)", r.text or "")
        loc = m.group(1) if m else None
    if not loc:
        raise LoginError(f"验证码校验后未拿到跳转地址（HTTP {r.status_code}，响应：{(r.text or '')[:200]!r}）",
                         raw=vresp)
    if not loc.startswith("http"):
        loc = ACCOUNT_HOST + loc
    _req(session, "get", loc)          # 访问一次，建立已认证会话

    # 会话已认证：重做 serviceLogin 拿 ssecurity / passToken / STS 地址
    r3 = _req(session, "get", f"{ACCOUNT_HOST}/pass/serviceLogin",
              params={"sid": sid, "_json": "true"})
    j3 = _json_body(r3.text)
    if j3.get("code") != 0 or not j3.get("ssecurity"):
        raise LoginError(f"二次验证后登录未完成（code={j3.get('code')} {j3.get('desc') or ''}）",
                         code=j3.get("code"), raw=j3)

    token = None
    if j3.get("location"):
        r4 = _req(session, "get", j3["location"], allow_redirects=False)
        token = (session.cookies.get("serviceToken", domain="sts-hlth.io.mi.com")
                 or session.cookies.get("serviceToken"))
        if not token:
            m = re.search(r"serviceToken=([^;]+)", r4.headers.get("Set-Cookie", ""))
            token = m.group(1) if m else None
    return {"ssecurity": j3.get("ssecurity"), "service_token": token,
            "cuser_id": j3.get("cUserId") or session.cookies.get("cUserId"),
            "user_id": j3.get("userId") or session.cookies.get("userId"),
            "pass_token": j3.get("passToken") or session.cookies.get("passToken"),
            "sid": sid, "ts": int(time.time())}


def _device_id():
    """稳定伪设备号（也可写在 config 的 device_id 里）。"""
    import random, string
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(16))


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


def refresh_session(cfg, cfg_path=None, proxies=None):
    """用 config 里的 pass_token 免密续期；成功返回新 session。"""
    if not cfg.get("pass_token") or not cfg.get("cuser_id"):
        raise LoginError("config 里没有 pass_token，无法免密续期（请先账号密码登录一次）")
    s = exchange_pass_token(cfg["pass_token"], cfg["cuser_id"], cfg.get("user_id") or "",
                            proxies=proxies or cfg.get("proxies"))
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
