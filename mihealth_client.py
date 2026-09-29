#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mi Health (com.mi.health) cloud API client — reverse-engineered.

Protocol (from com.xiaomi.fitness.app.CloudInterceptor + zi4/di4/xj4/ffc/o0l):
  nonce       = b64( 8B random | int32(minutes_since_epoch + timeDiff) )
  sessionKey  = b64( SHA256( b64dec(ssecurity) + b64dec(nonce) ) )   # 32B RC4 key
  encrypt path (SecretData.encryptResponse=true):
      rc4_hash__ = b64( SHA1( METHOD&path&k=v...&sessionKey ) )  # plain params
      each param value -> b64( RC4(sessionKey, value) )          # RC4-drop1024
      signature  = b64( SHA1( METHOD&path&k=rc4(v)...&sessionKey ) )
      _nonce     = nonce
  response    = RC4-decrypt( b64dec(body) ) as UTF-8 JSON

Auth cookies on .hlth.io.mi.com: cUserId / serviceToken(=sid) / locale.
"""
import base64, hashlib, hmac as _hmac, json, os, struct, time
import urllib.parse

# ---------------- crypto ----------------

class RC4:
    """RC4 with 1024-byte drop (o0l: ctor drops o0l.b = 1024 zeros)."""
    def __init__(self, key: bytes, drop: int = 1024):
        if len(key) != 32:
            raise ValueError("rc4Key length is invalid")
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) & 0xFF
            s[i], s[j] = s[j], s[i]
        self.s, self.i, self.j = s, 0, 0
        self.crypt(bytes(drop))

    def crypt(self, data: bytes) -> bytes:
        s = self.s
        i, j = self.i, self.j
        out = bytearray(len(data))
        for n, b in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            out[n] = b ^ s[(s[i] + s[j]) & 0xFF]
        self.i, self.j = i, j
        return bytes(out)

def b64e(b: bytes) -> str: return base64.b64encode(b).decode()
def b64d(s: str) -> bytes: return base64.b64decode(s)

def gen_nonce(time_diff: int = 0) -> str:
    import secrets
    rand8 = secrets.token_bytes(8)
    minutes = int((time.time() * 1000 + time_diff) // 60000)
    return b64e(rand8 + struct.pack(">i", minutes))

def session_key(ssecurity: str, nonce: str) -> str:
    return b64e(hashlib.sha256(b64d(ssecurity) + b64d(nonce)).digest())

def sha1_b64(items) -> str:
    text = "&".join(items)
    return b64e(hashlib.sha1(text.encode("utf-8")).digest())

def hmac_sha256_b64(text: str, key_b64: str) -> str:
    return b64e(_hmac.new(b64d(key_b64), text.encode(), hashlib.sha256).digest())

# ---------------- request builders (mirror zi4.c / zi4.d) ----------------

def signed_encrypted(method: str, path: str, params: dict, nonce: str, security: str):
    """zi4.c — encryptResponse=true endpoints.

    Order matters (mirrors decompiled zi4.c):
      1. rc4_hash__ = sha1_b64(METHOD & path & sorted-plain-k=v & sessionKey)
      2. rc4_hash__ is inserted into the PLAIN map, then ALL values (params +
         rc4_hash__ itself) are RC4-encrypted into the output map
      3. signature = sha1_b64(METHOD & path & sorted-ENCRYPTED-k=v & sessionKey)
      4. output = encrypted params(+enc rc4_hash__) + signature + _nonce
    """
    skey = session_key(security, nonce)
    plain = {k: v for k, v in params.items() if k and v != "" and v is not None}
    rc4_hash = sha1_b64([method.upper(), urllib.parse.urlparse(path).path or path] +
                        [f"{k}={v}" for k, v in sorted(plain.items())] + [skey])
    # zi4.c puts rc4_hash__ into the plain map BEFORE the encrypt loop,
    # so it is rc4-encrypted like every other value.
    plain["rc4_hash__"] = rc4_hash
    rc4 = RC4(b64d(skey))
    enc_sorted = {}
    for k in sorted(plain):
        enc_sorted[k] = b64e(rc4.crypt(plain[k].encode("utf-8")))
    sig = sha1_b64([method.upper(), urllib.parse.urlparse(path).path or path] +
                   [f"{k}={v}" for k, v in enc_sorted.items()] + [skey])
    out = dict(enc_sorted)
    out["signature"] = sig
    out["_nonce"] = nonce
    return out

def signed_plain(method: str, path: str, params: dict, nonce: str, security: str):
    """zi4.d — encryptResponse=false endpoints (hmac signature)."""
    skey = session_key(security, nonce)
    items = []
    if method:  # zi4.d adds p6 (subpath?) first if non-null — see note below
        items.append(method)
    items += [skey, nonce]
    clean = {k: v for k, v in sorted(params.items()) if k and v != "" and v is not None}
    if clean:
        items += [f"{k}={v}" for k, v in clean.items()]
    else:
        items.append("data=")
    out = dict(clean)
    out["signature"] = hmac_sha256_b64("&".join(items), skey)
    out["_nonce"] = nonce
    return out

def decrypt_response(body_b64: str, nonce: str, security: str) -> str:
    skey = session_key(security, nonce)
    return RC4(b64d(skey)).crypt(b64d(body_b64)).decode("utf-8")

# ---------------- client ----------------

class MiHealthClient:
    BASE = "https://hlth.io.mi.com"
    PREFIX = "/app/v1/"

    def __init__(self, ssecurity, service_token, cuser_id,
                 time_diff=0, proxies=None):
        self.security = ssecurity
        self.service_token = service_token   # the long token (sts-hlth domain value)
        self.cuser = cuser_id
        self.time_diff = time_diff
        import requests
        self.http = requests.Session()
        if proxies: self.http.proxies.update(proxies)

    def _cookies(self):
        return {"cUserId": self.cuser, "serviceToken": self.service_token, "locale": "zh_cn"}

    def call(self, subpath: str, params: dict, method="GET", decrypt=True):
        nonce = gen_nonce(self.time_diff)
        q = signed_encrypted(method, self.PREFIX + subpath, params, nonce, self.security)
        url = self.BASE + self.PREFIX + subpath
        if method.upper() == "GET":
            r = self.http.get(url, params=q, cookies=self._cookies(), timeout=20)
        else:
            r = self.http.post(url, data=q, cookies=self._cookies(), timeout=20)
        if decrypt and r.ok:
            return json.loads(decrypt_response(r.text, nonce, self.security))
        return r.text if not decrypt else (r.status_code, r.text[:500])

    # ---- convenience ----
    def get_fitness_data_by_time(self, key, start_ms, end_ms, reverse=True, next_key=None):
        # 游标参数实测是 snake_case "next_key"（nextKey 会被服务端忽略，永远返回第一页）
        data = {"key": key, "startTime": start_ms, "endTime": end_ms,
                "reverse": reverse, "next_key": next_key}
        return self.call("data/get_fitness_data_by_time", {"data": json.dumps(data, separators=(",", ":"))})

    def _paged_all(self, fetch_page, list_field, start_ms, end_ms,
                   max_pages, stop_below_start, filter_window=True):
        """Generic next_key follower.

        Server semantics (measured): next_key walks backwards through ALL
        history regardless of startTime; with startTime<=1 or =0 the cursor
        freezes (same page forever). So always pass a *recent* startTime and
        collect pages until has_more=False; filter to window client-side.
        stop_below_start: stop paging once a whole page is older than start_ms
        (saves calls when you only want a window, not full history).
        """
        items, nk = [], None
        for _ in range(max_pages):
            res = (fetch_page(nk) or {}).get("result") or {}
            batch = res.get(list_field, [])
            if filter_window:
                items += [it for it in batch
                          if (it.get("time") or 0) * 1000 >= start_ms
                          and (it.get("time") or 0) * 1000 <= end_ms]
            else:
                items += batch
            nk = res.get("next_key")
            if not res.get("has_more") or not nk:
                return items, False
            if (stop_below_start and batch
                    and max((it.get("time") or 0) for it in batch) * 1000 < start_ms):
                return items, False
        return items, True

    def get_fitness_data_all(self, key, start_ms, end_ms, max_pages=200, reverse=True,
                             full_history=False):
        # Server freezes next_key when startTime<=1; always send a recent start.
        api_start = max(start_ms, end_ms - 86400000)
        items, more = self._paged_all(
            lambda nk: self.get_fitness_data_by_time(key, api_start, end_ms, reverse, nk),
            "data_list", start_ms, end_ms, max_pages,
            stop_below_start=not full_history, filter_window=True)
        return {"data_list": items, "has_more": more}

    def get_sport_records_all(self, start_ms, end_ms, max_pages=200, limit=50,
                              full_history=False):
        api_start = max(start_ms, end_ms - 86400000)
        items, more = self._paged_all(
            lambda nk: self.get_sport_records_by_time(api_start, end_ms, nk, limit),
            "sport_records", start_ms, end_ms, max_pages,
            stop_below_start=not full_history)
        return {"sport_records": items, "has_more": more}

    def get_latest_fitness_data(self, keys_with_limit):
        dl = [{"key": k, "limit": l, "time": None} for k, l in keys_with_limit]
        return self.call("data/get_latest_fitness_data", {"data": json.dumps({"dataList": dl}, separators=(",", ":"))})

    def get_aggregated_by_time(self, key, start_ms, end_ms, tag="daily", cursor=None, reverse=True):
        data = {"tag": tag, "key": key, "startTime": start_ms, "endTime": end_ms,
                "cursor": cursor, "reverse": reverse}
        return self.call("data/get_aggregated_fitness_data_by_time", {"data": json.dumps(data, separators=(",", ":"))})

    def get_sport_records_by_time(self, start_ms, end_ms, next_key=None, limit=50):
        data = {"startTime": start_ms, "endTime": end_ms, "next_key": next_key, "limit": limit}
        return self.call("data/get_sport_records_by_time", {"data": json.dumps(data, separators=(",", ":"))})


def load_config(path=None):
    """凭证读取顺序: 参数路径 > 同目录 config.json > 环境变量 MIH_*"""
    cfg = {}
    cand = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(cand, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    return {
        "ssecurity": cfg.get("ssecurity") or os.environ.get("MIH_SSECURITY"),
        "service_token": cfg.get("service_token") or os.environ.get("MIH_SERVICE_TOKEN"),
        "cuser_id": cfg.get("cuser_id") or os.environ.get("MIH_CUSER_ID"),
    }


if __name__ == "__main__":
    cfg = load_config()
    C = MiHealthClient(cfg["ssecurity"], cfg["service_token"], cfg["cuser_id"])
    now = int(time.time() * 1000)
    day = 86400000
    for key in ["steps", "heart_rate", "sleep", "spo2", "calories"]:
        try:
            r = C.get_fitness_data_by_time(key, now - day, now)
            n = len(r.get("result", {}).get("data_list", [])) if isinstance(r, dict) else -1
            print(f"[{key}] items={n}")
            if n:
                item = r["result"]["data_list"][0]
                print("   ", item.get("time"), item.get("value", "")[:160])
        except Exception as e:
            print(f"[{key}] ERR {e}")
