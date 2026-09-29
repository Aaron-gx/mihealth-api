#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Refresh Mi Health credentials from a rooted device/emulator via adb.

The app keeps its session in the WebView cookie DB, which is plain SQLite:

  /data/data/com.mi.health/app_webview/Default/Cookies
      sts-hlth.io.mi.com            -> serviceToken, cUserId
      *.yrn.net / *.iot.mi.com      -> ssecurity

Requires: adb on PATH (or config `adb_path`), root on the device, and the app
to have been launched at least once with a signed-in account.

  python tools/refresh_credentials.py            # update config.json
  python tools/refresh_credentials.py --print     # print only
  python tools/refresh_credentials.py --serial emulator-5554
"""
import argparse, json, os, sqlite3, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PKG = "com.mi.health"
COOKIE_DB = f"/data/data/{PKG}/app_webview/Default/Cookies"
TOKEN_HOST = "sts-hlth.io.mi.com"      # serviceToken / cUserId
SEC_HOSTS = (".wear.mi.com.internal.yrn.net", ".iot.mi.com", ".hlth.io.mi.com")


def _run(cmd, binary=False, timeout=60):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        return None, (p.stderr or b"").decode(errors="replace") if binary else p.stderr
    return (p.stdout if binary else p.stdout.decode(errors="replace")), None


def pick_serial(adb, serial=None):
    if serial:
        return serial
    out, err = _run([adb, "devices"])
    if out is None:
        raise RuntimeError(f"adb failed: {err}")
    devs = [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip().endswith("device")]
    if not devs:
        raise RuntimeError("no adb device attached (start the emulator/device first)")
    return devs[0]


SQLITE_MAGIC = b"SQLite format 3\x00"


def normalize_sqlite(data: bytes) -> bytes:
    """Trim pty noise / trailing junk: slice from the SQLite magic and cut the
    file to page_size*page_count as declared in the header."""
    i = data.find(SQLITE_MAGIC)
    if i < 0:
        return data
    data = data[i:]
    if len(data) < 32:
        return data
    page_size = int.from_bytes(data[16:18], "big")
    if page_size == 1:
        page_size = 65536
    page_count = int.from_bytes(data[28:32], "big")
    if 512 <= page_size <= 65536 and page_count > 0:
        declared = page_size * page_count
        if 0 < declared < len(data):
            data = data[:declared]
    return data


def _run_bytes(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return (p.stdout or b""), p.returncode


def pull_bytes(adb, serial, remote):
    """Binary-safe read of a root-owned file.

    `adb exec-out su -c cat` is NOT safe here: su runs the command under a pty,
    whose line discipline rewrites \\n to \\r\\n and corrupts binary payloads
    (seen on LDPlayer: a 36864-byte SQLite file arrived as 36887 bytes and
    SQLite rejected it). base64 avoids the pty entirely; cp+pull is the
    fallback for devices without base64.
    """
    out, rc = _run_bytes([adb, "-s", serial, "exec-out", "su", "-c", f"base64 {remote}"])
    if rc == 0 and out.strip():
        try:
            import base64 as _b64
            data = _b64.b64decode(b"".join(out.split()), validate=False)
            if data.startswith(SQLITE_MAGIC):
                return normalize_sqlite(data)
        except Exception:
            pass
    tmp = "/data/local/tmp/.mih_cookies"
    out, rc = _run_bytes([adb, "-s", serial, "exec-out", "su", "-c",
                          f"cp {remote} {tmp} && chmod 644 {tmp}"])
    if rc != 0:
        return None
    local = tempfile.mktemp(prefix="mih_")
    p = subprocess.run([adb, "-s", serial, "pull", tmp, local], capture_output=True, timeout=120)
    _run([adb, "-s", serial, "shell", "rm", "-f", tmp])
    if p.returncode != 0:
        return None
    with open(local, "rb") as f:
        data = f.read()
    os.remove(local)
    return normalize_sqlite(data)


def fetch_from_device(serial=None, adb=None):
    """Returns {'ssecurity','service_token','cuser_id','user_id'} or raises."""
    adb = adb or os.environ.get("ADB") or "adb"
    serial = pick_serial(adb, serial)
    tmpdir = tempfile.mkdtemp(prefix="mih_cookie_")
    base = os.path.join(tmpdir, "Cookies")
    data = pull_bytes(adb, serial, COOKIE_DB)
    if not data or not data.startswith(SQLITE_MAGIC):
        raise RuntimeError(
            f"cannot read {COOKIE_DB} — device needs root (su) and the app must "
            "have been opened once while signed in")
    with open(base, "wb") as f:
        f.write(data)
    # WAL sidecars (only exist when the DB uses WAL; rollback-journal devices
    # carry a -journal instead, which is not needed)
    for suffix in ("-wal", "-shm"):
        side = pull_bytes(adb, serial, COOKIE_DB + suffix)
        if side:
            with open(base + suffix, "wb") as f:
                f.write(side)

    db = sqlite3.connect(base)
    rows = db.execute("SELECT host_key,name,value FROM cookies").fetchall()
    db.close()

    creds = {"service_token": None, "cuser_id": None, "ssecurity": None, "user_id": None}
    for host, name, value in rows:
        if host == TOKEN_HOST:
            if name == "serviceToken":
                creds["service_token"] = value
            elif name == "cUserId":
                creds["cuser_id"] = value
            elif name == "userId":
                creds["user_id"] = value
        if name == "ssecurity" and creds["ssecurity"] is None:
            creds["ssecurity"] = value
        elif name == "ssecurity" and host in SEC_HOSTS:
            creds["ssecurity"] = value
    missing = [k for k in ("service_token", "cuser_id", "ssecurity") if not creds[k]]
    if missing:
        raise RuntimeError(f"missing cookies: {missing} (hosts seen: "
                           f"{sorted({h for h, _, _ in rows})[:6]}...)")
    return creds


def write_config(creds, path=None):
    path = path or os.path.join(ROOT, "config.json")
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg["ssecurity"] = creds["ssecurity"]
    cfg["service_token"] = creds["service_token"]
    cfg["cuser_id"] = creds["cuser_id"]
    if creds.get("user_id"):
        cfg.setdefault("user_id", creds["user_id"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return path


def main():
    ap = argparse.ArgumentParser(description="refresh Mi Health credentials from device")
    ap.add_argument("--serial")
    ap.add_argument("--adb")
    ap.add_argument("--config")
    ap.add_argument("--print", action="store_true", dest="print_only")
    a = ap.parse_args()
    try:
        creds = fetch_from_device(a.serial, a.adb)
    except Exception as e:
        print("FAILED:", e, file=sys.stderr)
        return 1
    if a.print_only:
        for k, v in creds.items():
            print(f"{k} = {v}")
    else:
        p = write_config(creds, a.config)
        print("updated", p)
        print("  service_token:", creds["service_token"][:24], "...")
        print("  cuser_id     :", creds["cuser_id"])
        print("  ssecurity    :", creds["ssecurity"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
