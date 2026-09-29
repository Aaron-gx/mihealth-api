#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mi Health (com.mi.health) cloud API client — reverse-engineered.

Protocol (from com.xiaomi.fitness.app.CloudInterceptor + zi4/di4/xj4/ffc/o0l):
  nonce       = b64( 8B random | int32(minutes_since_epoch + timeDiff) )
  sessionKey  = b64( SHA256( b64dec(ssecurity) + b64dec(nonce) ) )   # 32B RC4 key
  encrypted path (SecretData.encryptResponse=true):
      rc4_hash__ = b64( SHA1( METHOD&path&k=v...&sessionKey ) )  # plain params
      each param value -> b64( RC4(sessionKey, value) )          # RC4-drop1024
      signature  = b64( SHA1( METHOD&path&k=rc4(v)...&sessionKey ) )
      _nonce     = nonce
  response    = RC4-decrypt( b64dec(body) ) as UTF-8 JSON

Auth: cookies on .hlth.io.mi.com — cUserId / serviceToken / locale.

Paging semantics (measured):
  * cursor param is snake_case `next_key` (camelCase `nextKey` is ignored server-side)
  * `next_key` walks backwards through ALL history regardless of startTime; a
    startTime <= 1 freezes the cursor (same page forever) — so always pass a
    recent startTime and filter client-side.
  * watermark feeds are forward-only global change streams, per family, 502 cap.

Optional speedup: `pip install pycryptodome` enables C-speed RC4.
"""
import base64, hashlib, hmac as _hmac, json, os, random, struct, threading, time
import urllib.parse

try:  # optional C-speed RC4
    from Crypto.Cipher import ARC4 as _ARC4
except Exception:  # pragma: no cover
    _ARC4 = None


# ---------------------------------------------------------------- errors

class ApiError(Exception):
    """Non-zero business code returned by the server."""

class AuthError(ApiError):
    """401 / code=2 auth err — credentials likely expired."""


# ---------------------------------------------------------------- crypto

def b64e(b: bytes) -> str: return base64.b64encode(b).decode()


def b64d(s: str) -> bytes: return base64.b64decode(s)


class RC4:
    """RC4 with 1024-byte drop (o0l drops o0l.b = 1024 zeros).

    Uses pycryptodome's C implementation when available, else a tight pure
    Python PRGA that skips the drop phase by advancing the state instead of
    encrypting 1024 zero bytes.
    """

    DROP = 1024

    def __init__(self, key: bytes, drop: int = DROP):
        if len(key) != 32:
            raise ValueError("rc4Key length is invalid")
        self._c = None
        if _ARC4 is not None:
            try:
                c = _ARC4.new(bytes(key))
                if drop:
                    c.encrypt(b"\x00" * drop)
                self._c = c
                return
            except Exception:
                self._c = None
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) & 0xFF
            s[i], s[j] = s[j], s[i]
        self._s, self._i, self._j = bytearray(s), 0, 0
        if drop:
            self._skip(drop)

    def _skip(self, n: int) -> None:
        s, i, j = self._s, self._i, self._j
        for _ in range(n):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
        self._i, self._j = i, j

    def crypt(self, data: bytes) -> bytes:
        if self._c is not None:
            return self._c.encrypt(bytes(data))
        data = bytes(data)
        s, i, j = self._s, self._i, self._j
        out = bytearray(len(data))
        for n in range(len(data)):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            out[n] = data[n] ^ s[(s[i] + s[j]) & 0xFF]
        self._i, self._j = i, j
        return bytes(out)


def gen_nonce(time_diff: int = 0) -> str:
    rand8 = random.getrandbits(64).to_bytes(8, "big")
    minutes = int((time.time() * 1000 + time_diff) // 60000)
    return b64e(rand8 + struct.pack(">i", minutes))


def session_key(ssecurity: str, nonce: str) -> str:
    return b64e(hashlib.sha256(b64d(ssecurity) + b64d(nonce)).digest())


def _path_of(path: str) -> str:
    return urllib.parse.urlparse(path).path or path


def sha1_b64(items) -> str:
    return b64e(hashlib.sha1("&".join(items).encode("utf-8")).digest())


def hmac_sha256_b64(text: str, key_b64: str) -> str:
    return b64e(_hmac.new(b64d(key_b64), text.encode(), hashlib.sha256).digest())


def signed_encrypted(method: str, path: str, params: dict, nonce: str, security: str) -> dict:
    """zi4.c — encryptResponse=true endpoints (rc4_hash__ is inserted into the
    plain map BEFORE the encrypt loop, so it is rc4-encrypted like the others)."""
    skey = session_key(security, nonce)
    plain = {k: v for k, v in params.items() if k and v != "" and v is not None}
    p = _path_of(path)
    rc4_hash = sha1_b64([method.upper(), p] +
                        [f"{k}={v}" for k, v in sorted(plain.items())] + [skey])
    plain["rc4_hash__"] = rc4_hash
    rc4 = RC4(b64d(skey))
    enc = {k: b64e(rc4.crypt(plain[k].encode("utf-8"))) for k in sorted(plain)}
    sig = sha1_b64([method.upper(), p] +
                   [f"{k}={v}" for k, v in enc.items()] + [skey])
    out = dict(enc)
    out["signature"] = sig
    out["_nonce"] = nonce
    return out


def signed_plain(method: str, path: str, params: dict, nonce: str, security: str) -> dict:
    """zi4.d — encryptResponse=false endpoints (hmac signature)."""
    skey = session_key(security, nonce)
    items = []
    if method:
        items.append(method)
    items += [skey, nonce]
    clean = {k: v for k, v in sorted(params.items()) if k and v != "" and v is not None}
    items += [f"{k}={v}" for k, v in clean.items()] if clean else ["data="]
    out = dict(clean)
    out["signature"] = hmac_sha256_b64("&".join(items), skey)
    out["_nonce"] = nonce
    return out


def decrypt_response(body_b64: str, nonce: str, security: str) -> str:
    return RC4(b64d(session_key(security, nonce))).crypt(b64d(body_b64)).decode("utf-8")


# ---------------------------------------------------------------- dedup

SCALAR_FIELDS = {
    "steps": ("steps",),
    "calories": ("calories",),
    "heart_rate": ("bpm", "heart_rate"),
    "spo2": ("spo2",),
    "stress": ("stress", "stress_value"),
    "vo2_max": ("vo2_max",),
    "pai": ("daily_pai", "pai", "value"),
    "weight": ("weight",),
    "energy": ("energy",),
}


def canonical_number(key: str, item: dict):
    """Best-effort numeric value of a record (used for cross-source dedup)."""
    for f in SCALAR_FIELDS.get(key, ()):
        v = item.get(f)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None


def dedup_items(items, key=None):
    """Collapse (sid,time) duplicates from paging overlap and keep one record
    per minute — the record with the largest canonical numeric value."""
    by_t = {}
    for x in items:
        t = x.get("_t")
        if t is None:
            continue
        cur = by_t.get(t)
        if cur is None:
            by_t[t] = dict(x)
            continue
        n_new, n_cur = canonical_number(key or "", x), canonical_number(key or "", cur)
        if n_new is not None and (n_cur is None or n_new > n_cur):
            merged = dict(cur)
            merged.update(x)
            by_t[t] = merged
        else:
            for k, v in x.items():
                if k not in cur:
                    cur[k] = v
    return sorted(by_t.values(), key=lambda x: x["_t"])


# ---------------------------------------------------------------- client

class MiHealthClient:
    HOSTS = {
        "cn": "https://hlth.io.mi.com",          # 中国大陆
        "abroad": "https://region.hlth.io.mi.com",  # 海外区
    }
    PREFIX = "/app/v1/"
    RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504)

    def __init__(self, ssecurity, service_token, cuser_id, region="cn", host=None,
                 time_diff=0, proxies=None, timeout=25, max_retries=3,
                 min_interval=0.0, on_auth_error=None):
        self.security = ssecurity
        self.service_token = service_token
        self.cuser = cuser_id
        self.base = (host or self.HOSTS.get(region) or self.HOSTS["cn"]).rstrip("/")
        self.time_diff = time_diff
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self.on_auth_error = on_auth_error
        self._lock = threading.Lock()
        self._last = 0.0
        import requests
        self.http = requests.Session()
        if proxies:
            self.http.proxies.update(proxies)

    # -- internals -------------------------------------------------
    def _throttle(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _cookies(self):
        return {"cUserId": self.cuser, "serviceToken": self.service_token, "locale": "zh_cn"}

    def _send(self, method, url, q, retries):
        last = None
        for attempt in range(retries + 1):
            self._throttle()
            try:
                if method.upper() == "GET":
                    return self.http.get(url, params=q, cookies=self._cookies(), timeout=self.timeout)
                return self.http.post(url, data=q, cookies=self._cookies(), timeout=self.timeout)
            except Exception as e:          # connection / timeout
                last = e
            if attempt < retries:
                time.sleep(min(2 ** attempt + random.random(), 8))
        raise ApiError(f"request failed after {retries + 1} tries: {last}")

    # -- public API ------------------------------------------------
    def call(self, subpath, params, method="GET", decrypt=True, retries=None):
        """One signed API call. Returns parsed JSON (decrypted) or the raw text.

        On auth failure the optional `on_auth_error` hook runs (e.g. re-read
        credentials from the device) and the request is retried once with fresh
        credentials — the self-heal path.
        """
        retries = self.max_retries if retries is None else retries
        refreshed = False
        while True:
            nonce = gen_nonce(self.time_diff)
            q = signed_encrypted(method, self.PREFIX + subpath, params, nonce, self.security)
            url = self.base + self.PREFIX + subpath
            r = self._send(method, url, q, retries)

            auth_problem = r.status_code in (401, 403)
            data = None
            if not auth_problem:
                if r.status_code >= 400:
                    raise ApiError(f"HTTP {r.status_code}: {r.text[:200]}")
                if not decrypt:
                    return r.text
                try:
                    data = json.loads(decrypt_response(r.text, nonce, self.security))
                except Exception:
                    try:
                        data = json.loads(r.text)   # plaintext error body
                    except Exception:
                        raise ApiError(f"undecodable response: {r.text[:200]}")
                code = data.get("code")
                if code == 2 or (isinstance(code, int)
                                 and "auth" in str(data.get("message", "")).lower()):
                    auth_problem = True

            if not auth_problem:
                return data

            why = f"HTTP {r.status_code}" if data is None else f"code={data.get('code')}"
            if not refreshed and callable(self.on_auth_error):
                refreshed = True
                if self._auth_failed(why):
                    continue                        # retry with fresh credentials
            raise AuthError(f"auth failed ({why})")

    def _auth_failed(self, why):
        """Run the auth-error hook; True when it reports a successful refresh."""
        if callable(self.on_auth_error):
            try:
                return bool(self.on_auth_error(self, why))
            except Exception:
                return False
        return False

    def reload_credentials(self, ssecurity=None, service_token=None, cuser_id=None):
        if ssecurity:
            self.security = ssecurity
        if service_token:
            self.service_token = service_token
        if cuser_id:
            self.cuser = cuser_id

    # -- paging ----------------------------------------------------
    def _paged_all(self, fetch_page, list_field, start_ms, end_ms, max_pages,
                   stop_below_start, filter_window=True, key=None, on_page=None):
        """Walk next_key. With `on_page`, rows stream to the callback per page and
        are NOT accumulated (keeps memory bounded on high-overlap keys like
        headset, where the same rows repeat across many pages)."""
        items, nk, pages, streamed = [], None, 0, 0

        def result():
            return (dedup_items(items, key) if not on_page else [])

        for _ in range(max_pages):
            res = (fetch_page(nk) or {}).get("result") or {}
            batch = res.get(list_field) or []
            for it in batch:                      # normalise the time key before dedup
                it.setdefault("_t", it.get("time"))
            if filter_window:
                kept = [it for it in batch
                        if (it.get("time") or 0) * 1000 >= start_ms
                        and (it.get("time") or 0) * 1000 <= end_ms]
            else:
                kept = list(batch)
            if on_page:
                streamed += len(kept)
                on_page(kept, nk, pages)
                pages += 1
            else:
                items += kept
            nk = res.get("next_key")
            if not res.get("has_more") or not nk:
                return result(), False
            # Whole page older than the window -> the window is fully covered.
            # (Pages are time-ordered backwards, so this is exact; sparse keys
            # simply span a long time per page, which is fine.)
            if (stop_below_start and batch
                    and max((it.get("time") or 0) for it in batch) * 1000 < start_ms):
                return result(), False
        return result(), True

    def get_fitness_data_by_time(self, key, start_ms, end_ms, reverse=True, next_key=None):
        data = {"key": key, "startTime": start_ms, "endTime": end_ms,
                "reverse": reverse, "next_key": next_key}
        return self.call("data/get_fitness_data_by_time",
                         {"data": json.dumps(data, separators=(",", ":"))})

    def get_fitness_data_all(self, key, start_ms, end_ms, max_pages=200, reverse=True,
                             full_history=False, on_page=None):
        """Fetch a time window (or full history) for one key.

        on_page(batch, next_key, page_index) streams rows per page and keeps
        memory bounded — recommended for backfill and for high-overlap keys.
        """
        api_start = max(start_ms, end_ms - 86400000)   # recent start keeps cursor alive
        items, more = self._paged_all(
            lambda nk: self.get_fitness_data_by_time(key, api_start, end_ms, reverse, nk),
            "data_list", start_ms, end_ms, max_pages,
            stop_below_start=not full_history, key=key, on_page=on_page)
        return {"data_list": items, "has_more": more}

    def get_sport_records_by_time(self, start_ms, end_ms, next_key=None, limit=50):
        data = {"startTime": start_ms, "endTime": end_ms, "next_key": next_key, "limit": limit}
        return self.call("data/get_sport_records_by_time",
                         {"data": json.dumps(data, separators=(",", ":"))})

    def get_sport_records_all(self, start_ms, end_ms, max_pages=200, limit=50,
                              full_history=False, on_page=None):
        api_start = max(start_ms, end_ms - 86400000)
        items, more = self._paged_all(
            lambda nk: self.get_sport_records_by_time(api_start, end_ms, nk, limit),
            "sport_records", start_ms, end_ms, max_pages,
            stop_below_start=not full_history, on_page=on_page)
        return {"sport_records": items, "has_more": more}

    def get_aggregated_by_time(self, key, start_ms, end_ms, tag="daily_fitness",
                               cursor=None, reverse=True):
        data = {"tag": tag, "key": key, "startTime": start_ms, "endTime": end_ms,
                "cursor": cursor, "reverse": reverse}
        return self.call("data/get_aggregated_fitness_data_by_time",
                         {"data": json.dumps(data, separators=(",", ":"))})

    def get_aggregated_all(self, key=None, start_ms=0, end_ms=None, tag="daily_fitness",
                           max_pages=30):
        end_ms = end_ms or int(time.time() * 1000)
        start_ms = start_ms or (end_ms - 86400000)
        items, nk = [], None
        for _ in range(max_pages):
            res = (self.get_aggregated_by_time(key, start_ms, end_ms, tag, nk) or {}).get("result") or {}
            batch = res.get("data_list") or []
            items += [it for it in batch if start_ms <= (it.get("time") or 0) * 1000 <= end_ms]
            nk = res.get("next_key")
            if not res.get("has_more") or not nk:
                break
            if batch and max((it.get("time") or 0) for it in batch) * 1000 < start_ms:
                break                      # window covered; cursor walks all history otherwise
        seen, out = set(), []
        for it in items:
            if it.get("time") in seen:
                continue
            seen.add(it.get("time"))
            out.append(it)
        return {"data_list": sorted(out, key=lambda x: x.get("time") or 0)}

    def get_latest_fitness_data(self, keys_with_limit):
        dl = [{"key": k, "limit": l, "time": None} for k, l in keys_with_limit]
        return self.call("data/get_latest_fitness_data",
                         {"data": json.dumps({"dataList": dl}, separators=(",", ":"))})

    # -- watermark (incremental) -----------------------------------
    def get_max_watermark(self, type_=0):
        r = self.call("data/get_max_watermark",
                      {"data": json.dumps({"type": type_}, separators=(",", ":"))})
        return ((r or {}).get("result") or {}).get("watermark") or 0

    def sync_by_watermark(self, subpath, list_field="data_list", start_wm=0,
                          max_pages=500, key=None, on_page=None):
        """Walk one family's forward-only watermark feed.

        `waterMark=0` is a seek: the server answers with the stream's first
        watermark and no rows. Returns (items, last_watermark, has_more).
        """
        items, wm, last_good = [], start_wm, start_wm
        for page in range(max_pages):
            res = (self.call(subpath, {"data": json.dumps({"waterMark": wm},
                                                          separators=(",", ":"))}) or {}).get("result") or {}
            batch = res.get(list_field) or []
            nwm = res.get("watermark") or 0
            for it in batch:
                it.setdefault("_t", it.get("time"))
            items += batch
            # A 0 here means "you are at the head of the stream" — never let it
            # overwrite the stored cursor, or records arriving in the gap would
            # be skipped by the next run's re-seed.
            if not res.get("has_more") or not nwm or nwm == wm:
                return dedup_items(items, key), (last_good or wm), bool(res.get("has_more"))
            last_good = nwm
            if on_page:
                on_page(batch, nwm, page)
            wm = nwm
        return dedup_items(items, key), last_good, True

    # -- convenience ------------------------------------------------
    def get_fitness_data_by_watermark(self, phone_id="", water_mark=0, limit=None):
        d = {"phoneId": phone_id, "waterMark": water_mark}
        if limit:
            d["limit"] = limit
        return self.call("data/get_fitness_data_by_watermark",
                         {"data": json.dumps(d, separators=(",", ":"))})


# ---------------------------------------------------------------- config

def load_config(path=None):
    """Credentials: explicit path > ./config.json > env MIH_*."""
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
        "region": cfg.get("region") or os.environ.get("MIH_REGION") or "cn",
        "host": cfg.get("host"),
        "auto_refresh": bool(cfg.get("auto_refresh")),
        "adb_serial": cfg.get("adb_serial"),
        "db": cfg.get("db"),
        "min_interval": float(cfg.get("min_interval") or 0.0),
        "config_path": cand,
    }


def client_from_config(cfg=None, **kw):
    """Build a client from config; explicit kwargs override config values."""
    cfg = cfg or load_config()
    params = {"region": cfg.get("region") or "cn", "host": cfg.get("host"),
              "min_interval": cfg.get("min_interval") or 0.0}
    params.update(kw)
    return MiHealthClient(cfg["ssecurity"], cfg["service_token"], cfg["cuser_id"], **params)


if __name__ == "__main__":
    cfg = load_config()
    C = client_from_config(cfg)
    now = int(time.time() * 1000)
    day = 86400000
    print(f"host={C.base}  pycryptodome={'yes' if _ARC4 else 'no'}")
    for key in ["steps", "heart_rate", "sleep", "spo2", "calories"]:
        try:
            r = C.get_fitness_data_all(key, now - day, now)
            items = r["data_list"]
            print(f"[{key}] items={len(items)}" + (f"  last={items[-1].get('value','')[:90]}" if items else ""))
        except Exception as e:
            print(f"[{key}] ERR {e}")
