#!/usr/bin/env python3
"""Stateless GitHub push worker: device flow -> push sekali -> token dibuang dari memori.

Token TIDAK pernah ditulis ke disk. Script keluar sendiri setelah push.
"""
import json
import subprocess
import time
import urllib.parse
import urllib.request

CLIENT_ID = "178c6fc778ccc68e1d6a"  # GitHub CLI OAuth app (public client, device flow)
SCOPE = "repo"
REPO = "https://github.com/nurarifmuhamad002-coder/asisten-browser.git"
BRANCH = "main"


def post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


print("[1/4] Minta kode device ke GitHub...", flush=True)
d = post("https://github.com/login/device/code", {"client_id": CLIENT_ID, "scope": SCOPE})
if "device_code" not in d:
    print("GAGAL minta kode:", d, flush=True)
    raise SystemExit(1)

print("=" * 60, flush=True)
print(f"  BUKA: {d['verification_uri']}", flush=True)
print(f"  KODE: {d['user_code']}", flush=True)
print("=" * 60, flush=True)
print("Menunggu otorisasi...", flush=True)

interval = int(d.get("interval", 5))
expires = time.time() + int(d.get("expires_in", 900))
token = None

print("[2/4] Polling token...", flush=True)
while time.time() < expires:
    time.sleep(interval)
    try:
        t = post("https://github.com/login/oauth/access_token",
                 {"client_id": CLIENT_ID, "device_code": d["device_code"],
                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    except Exception as e:
        print("  (poll error, lanjut):", e, flush=True)
        continue
    if "access_token" in t:
        token = t["access_token"]
        break
    err = t.get("error", "")
    if err in ("authorization_pending", "slow_down"):
        if err == "slow_down":
            interval += 5
        continue
    print("GAGAL:", t, flush=True)
    raise SystemExit(1)

if not token:
    print("TIMEOUT: kode kedaluwarsa.", flush=True)
    raise SystemExit(1)

print("[3/4] Token diterima. Push...", flush=True)
url = f"https://x-access-token:{token}@{REPO.split('://')[1]}"
env = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin"}
r = subprocess.run(["git", "-C", "/home/user", "push", url, f"HEAD:{BRANCH}"],
                   capture_output=True, text=True, env=env, timeout=180)
out = (r.stdout + r.stderr).strip()
# Jangan pernah log token
print("PUSH", "OK" if r.returncode == 0 else f"FAILED ({r.returncode})", flush=True)
print(out.replace(token, "***")[:1500], flush=True)

if r.returncode == 0:
    head = subprocess.run(["git", "-C", "/home/user", "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    print(f"PUSH OK: HEAD {head} -> origin/{BRANCH}", flush=True)

token = None
del token
print("[4/4] Selesai. Token dibuang dari memori.", flush=True)
