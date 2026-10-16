"""The owner-unlock agent (3.1, #295; design: hive-mind-private docs/design/owner_unlock_agent_3_1.md).

`hive-mind owner unlock` prompts for the passphrase once, unseals the owner seed and hands it, over an anonymous pipe,
to a fresh per-user process running THIS file. The agent keeps the seed in its own memory only (never a file, argv, env or
log), listens on a private Unix socket, and answers one question: "sign these exact canonical bytes?" It signs only when
the bytes parse as an allowlisted governance act (`Policy`), and says no to everything else; the CLI then falls back to
today's per-command passphrase prompt. The seed is never sent to a client, and there is no op that unlocks, returns a
seed, or signs arbitrary bytes.

It is a second `Signer` backend behind the signer seam: `ChainSigner` asks the agent first and falls back to a
`FileSigner`. This file produces signatures only through `hivemind.crypto.signer`; verification stays `ed25519.verify`.
`ownerkey.sign_governance` and the seam itself are unchanged, and no journal, entry, signature, governance, projection or
sync byte changes.

**Control plane only** (2.0 S2): `hv` never imports this file. It is also an entry point (`python3 -I owner_agent.py`),
so the path setup below is inside `_agent_main`, never at import.

**Gated mode** (4.0, owner approval slice A1, `owner unlock --gated`): the agent signs a body only if an enrolled approver's
passkey approved its hash (`present`, then `sign` with the ticket id) or a live delegation covers it, and it is the one place
that checks the WebAuthn assertion, the approver set and the delegation scope, cap and rate. It adds one file to the "no file
but the socket" rule: the used-ticket set, `<key dir>/used-tickets.json` (0600, no secret), written before a ticket's first
signature so a restart cannot reopen it. Approver and delegation records are signed under `hive-approver-v1` only by the
`member` op (after the membership rule of design 4.3) and by the membership verbs' break-glass path. Design and shared
pieces: `approvers.py`.

Best effort and stated as such: `PR_SET_DUMPABLE=0`, no core dump, `mlock` of the seed page. The bundled pure-Python
signer builds big integers on the ordinary heap and `FileSigner` keeps an immutable copy of the seed; CPython cannot zero
or lock either, so swap and hibernation are a residual (docs/THREAT_MODEL.md). Process exit is the real guarantee.
"""

import argparse
import base64
import ctypes
import hashlib
import json
import mmap
import os
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time
from collections import deque

if __name__ == "__main__":      # entry point only (`python3 -I` puts no directory on sys.path): this file's own, unresolved
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import approvers
import hive_runtime
import keystore
import merkle
import vocabulary
from hivemind.crypto import signer as _sigseam

try:
    import ed25519 as _ed25519
except Exception:                   # no bundled crypto: no agent
    _ed25519 = None

# ---- bounds (design §1, §2, §4) ----------------------------------------------------------------------------------
IDLE_DEFAULT, IDLE_MIN, IDLE_MAX = 15 * 60, 60, 8 * 3600
HARD_CAP = 12 * 3600
MAX_FRAME = 64 * 1024
READ_DEADLINE = 5.0
MAX_CONN = 8
SIGNS_PER_MINUTE, SIGNS_TOTAL = 20, 500
REFUSALS_PER_MINUTE, BACKOFF = 10, 30.0
RING = 200
GATE_MIN_IDLE = 60.0
GATE_MIN_BUDGET = SIGNS_TOTAL // 5
BOOT_JUMP = 5.0
SOCKET_CHECK = 5.0
UNIX_PATH_MAX = 100
TICKET_LIVE = 15 * 60                  # a presented ticket stays usable this long (design 4.3)
USED_KEEP = 24 * 3600                  # the used-ticket set prunes entries older than this
BATCH_MAX = 10
STDIN_MAX = 1 << 20                     # `owner present` and `owner member` read at most 1 MiB of JSON on stdin
USED_FILE, APPROVERS_FILE, DELEGATIONS_FILE = "used-tickets.json", "approvers.json", "delegations.json"


class AgentError(Exception):
    """The agent cannot be reached or used: no agent, a bad socket directory, a bad reply. Never fatal to the
    caller: the CLI falls back to the passphrase prompt."""


# ---- the socket path and directory checks (client and agent share them) -------------------------------------------

def _runtime_dir(env=None):
    env = os.environ if env is None else env
    xdg = hive_runtime.runtime_dir(env)          # a session without XDG_RUNTIME_DIR finds the login's (#522)
    if xdg:
        return os.path.join(xdg, "hive-mind")
    return os.path.join(env.get("TMPDIR") or "/tmp", f"hive-mind-{os.getuid()}")


def socket_path(hive_home, env=None):
    """`<runtime dir>/owner-agent-<h>.sock`, `<h>` the first 12 hex of sha256(realpath(HIVE_HOME)): one agent per
    user per hive home. The name is a hash, so the path stays under the AF_UNIX limit and discloses nothing."""
    h = hashlib.sha256(os.path.realpath(str(hive_home)).encode()).hexdigest()[:12]
    path = os.path.join(_runtime_dir(env), f"owner-agent-{h}.sock")
    if len(path.encode()) > UNIX_PATH_MAX:
        raise AgentError(f"the socket path is too long for AF_UNIX ({len(path)} > {UNIX_PATH_MAX}): set XDG_RUNTIME_DIR "
                         "or TMPDIR to a shorter directory")
    return path


def check_dir(path, create=False):
    """The socket's directory must be a real directory (not a symlink) owned by this uid at mode exactly 0700, or the
    caller refuses: a lookalike directory someone else pre-created cannot capture requests."""
    if create:
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError as e:
            raise AgentError(f"cannot create {path}: {e}")
    try:
        st = os.lstat(path)
    except OSError as e:
        raise AgentError(f"{path}: {e.strerror}")
    if not stat.S_ISDIR(st.st_mode):
        raise AgentError(f"{path} is not a directory")
    if st.st_uid != os.getuid():
        raise AgentError(f"{path} is not owned by this user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise AgentError(f"{path} is mode {stat.S_IMODE(st.st_mode):04o}, not 0700")


# ---- framing: a 4-byte length, then JSON; one request per connection ------------------------------------------------

def _recv_exact(conn, n, deadline):
    buf = b""
    while len(buf) < n:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("read deadline")
        conn.settimeout(left)
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def read_frame(conn, deadline):
    (n,) = struct.unpack(">I", _recv_exact(conn, 4, deadline))
    if n > MAX_FRAME:
        raise ValueError("oversize")
    obj = json.loads(_recv_exact(conn, n, deadline))
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


def write_frame(conn, obj):
    data = json.dumps(obj).encode()          # a frame, not a signed body: the signed bytes are `merkle._canonical`'s
    conn.sendall(struct.pack(">I", len(data)) + data)


# ---- the allowlist (design §3) ---------------------------------------------------------------------------------------

_KEYS = {"admit": {"action", "device_id", "module", "principal", "owner_pub"},
         "revoke": {"action", "device_id", "owner_pub"},
         "set-config": {"action", "key", "value", "owner_pub"}}


def _device_id_ok(d):
    return (isinstance(d, str) and d.startswith("k1:") and len(d) == 19
            and all(c in "0123456789abcdef" for c in d[3:]))


class Policy:
    """Decides whether the agent signs a body. `check` is pure (it mutates nothing); `commit` records an admit once
    the signature has been made. `probe(device_id)` returns "admitted", "purged", "other" or None (cannot tell: the
    act is refused). `installed(module)` says a plain unlock may touch that module's config."""

    def __init__(self, owner_pub_b64, modules, probe, installed):
        self.owner_pub = owner_pub_b64
        self.modules = frozenset(modules)
        self.probe = probe
        self.installed = installed
        self.admitted_session = set()

    def check(self, body, ticketed=False):
        """(ok, why, act) for the exact canonical bytes `body`. `act` is a short description, never a value.
        `ticketed` (gated mode: a presented ticket, or a delegation that then adds its own scope) keeps every shape
        rule and drops the `--module` and admitted-this-session scope of a plain unlock: a person approved those
        exact bytes."""
        try:
            obj = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False, "not JSON", "?"
        if not isinstance(obj, dict):
            return False, "not an object", "?"
        try:
            if merkle._canonical(obj) != body:        # what is inspected is what is signed
                return False, "not canonical", "?"
        except (TypeError, ValueError):
            return False, "not canonical", "?"
        action = obj.get("action")
        if not isinstance(action, str):
            return False, "no action", "?"
        keys = _KEYS.get(action)
        if keys is None:
            return False, "not on the allowlist", action[:32]
        if set(obj) != keys:
            return False, "wrong key set", action
        if obj["owner_pub"] != self.owner_pub:
            return False, "owner_pub is not this key", action
        if action == "admit":
            return self._admit(obj, ticketed)
        if action == "revoke":
            d = obj["device_id"]
            if not _device_id_ok(d):
                return False, "bad device id", action
            if d not in self.admitted_session and not ticketed:
                return False, "device not admitted this session", f"revoke {d}"
            return True, "", f"revoke {d}"
        return self._set_config(obj, ticketed)

    def _admit(self, obj, ticketed=False):
        d, m = obj["device_id"], obj["module"]
        if not _device_id_ok(d):
            return False, "bad device id", "admit"
        if not isinstance(obj["principal"], str) or not obj["principal"] or len(obj["principal"]) > 256:
            return False, "bad principal", "admit"
        if not vocabulary.valid_module_name(m):
            return False, "bad module name", "admit"
        act = f"admit {d} (module {m})"
        if m not in self.modules and not ticketed:
            return False, "module not named by --module", act
        if self.probe(d) != "other":
            return False, "device already admitted or purged, or governance unreadable", act
        return True, "", act

    def _set_config(self, obj, ticketed=False):
        k, v = obj["key"], obj["value"]
        sp = vocabulary.split_module_name(k)
        if sp is None:
            if ticketed and isinstance(k, str) and k.isidentifier() and len(k) <= 64 \
                    and (isinstance(v, str) and len(v) <= vocabulary.MODULE_VALUE_MAX
                         or isinstance(v, (int, float)) and not isinstance(v, bool)):
                return True, "", f"set-config {k}"        # a core governed key: a ticket only, never a delegation
            return False, "not a module config key", "set-config"
        act = f"set-config {k}"
        if not isinstance(v, str) or len(v) > vocabulary.MODULE_VALUE_MAX:
            return False, "bad value", act
        m = sp[0]
        if ticketed:
            return True, "", act
        if self.modules:
            if m not in self.modules:
                return False, "module not named by --module", act
        elif not self.installed(m):
            return False, "module not installed", act
        return True, "", act

    def commit(self, body):
        obj = json.loads(body.decode("utf-8"))
        if obj.get("action") == "admit":
            self.admitted_session.add(obj["device_id"])


# ---- clocks (injectable for tests) ----------------------------------------------------------------------------------

class Clocks:
    def mono(self):
        return time.monotonic()

    def wall(self):
        return time.time()

    def boot(self):
        try:
            return time.clock_gettime(time.CLOCK_BOOTTIME)
        except (AttributeError, OSError):
            return None


def peer_uid(conn):
    """The connecting process's uid, or None if the platform cannot say (the connection is then refused)."""
    try:
        if hasattr(socket, "SO_PEERCRED"):
            _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            return uid
        if sys.platform == "darwin":                        # LOCAL_PEERCRED: struct xucred {version, uid, ...}
            data = conn.getsockopt(0, 1, 76)
            return struct.unpack_from("=II", data)[1]
    except OSError:
        pass
    return None


def peer_pid(conn):
    try:
        if hasattr(socket, "SO_PEERCRED"):
            return struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[0]
    except OSError:
        pass
    return None


def _peer_exe(pid):
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except (OSError, TypeError):
        return "?"


# ---- the agent ------------------------------------------------------------------------------------------------------

class Agent:
    """Holds the signer and answers on `sock_path`. `run()` returns once locked; the caller then exits the process."""

    def __init__(self, seed, sock_path, idle, policy, clocks=None, uid=None, mlocked=False, peer_uid_fn=peer_uid,
                 gated=False, state_dir=None, rp_id=None, origin=None, approvers_min=1, allow_synced=False, keystore=None):
        if keystore and not gated:
            raise ValueError("a keystore-sourced seed is held only by a gated agent")
        self.keystore = keystore
        self.signer = _sigseam.FileSigner(seed)
        if base64.b64encode(self.signer.public_key()).decode() != policy.owner_pub:
            raise ValueError("the seed is not the owner key")
        self.sock_path, self.idle, self.policy = sock_path, idle, policy
        self.clk = clocks or Clocks()
        self.uid = os.getuid() if uid is None else uid
        self.mlocked = mlocked
        self._peer_uid = peer_uid_fn
        self.t0, self.w0 = self.clk.mono(), self.clk.wall()
        self.last_mono, self.last_wall = self.t0, self.w0
        self.boot_off = self._boot_off()
        self.signed = 0
        self.sign_times = deque()
        self.refusals = deque()
        self.backoff_until = 0.0
        self.ring = deque(maxlen=RING)
        self.lock = threading.Lock()
        self.conns = 0
        self.stop = threading.Event()
        self.why_locked = None
        self.sock = None
        self.ino = None
        self.buf = None                               # the mmap page holding the seed
        # gated mode (owner approval slice A1): signs only what a presented ticket or a live delegation covers
        self.gated, self.state_dir = gated, state_dir
        self.rp_id, self.origin = rp_id, origin
        self.approvers_min, self.allow_synced = approvers_min, allow_synced
        self.approvers = {}                           # credential id -> verified approver record
        self.delegations = {}                         # id -> verified, live delegation record
        self.dead_delegations = set()                 # revoked or cleared this run: a restored file cannot bring one back
        self.counters = {}                            # approver id -> last WebAuthn sign count
        self.used = {}                                # ticket id -> {acts_hash, used_at, approver}
        self.uses = {}                                # delegation id -> wall times of its signed acts (last 24 h)
        self.live = {}                                # ticket id -> {acts, signed, exp, approver}
        if gated:
            self.load_state()

    # -- lifetime
    def _boot_off(self):
        b = self.clk.boot()
        return None if b is None else b - self.clk.mono()

    # -- gated state: the used-ticket set, the approver and delegation records
    def _file(self, name):
        return os.path.join(self.state_dir, name)

    def load_state(self):
        """Read approvers.json, delegations.json and used-tickets.json from the state dir. A record whose owner signature
        does not verify is dropped. A gated agent refuses to start with fewer than `approvers_min` unrevoked approvers."""
        if self.gated and (not self.state_dir or not self.rp_id or not self.origin):
            raise ValueError("gated mode needs a state directory, an rpId and an origin")
        if self.state_dir:
            pub, now = self.policy.owner_pub, self.clk.wall()
            self.approvers = {r["id"]: r for r in approvers.load_records(self._file(APPROVERS_FILE), pub, approvers.approver_ok)}
            self.delegations = {r["id"]: r for r in approvers.load_records(
                self._file(DELEGATIONS_FILE), pub, lambda r: approvers.delegation_ok(r, now, new=False))
                if "revoked" not in r and r["expires"] > now}
            try:
                data = json.loads(open(self._file(USED_FILE), "rb").read().decode())
                for name, kind in (("used", dict), ("counters", dict), ("uses", dict)):
                    if isinstance(data.get(name), kind):
                        setattr(self, name, data[name])
            except (OSError, ValueError, AttributeError):
                pass
            self._prune(now)
        if self.gated and len(approvers.live_approvers(self.approvers.values())) < self.approvers_min:
            raise ValueError(f"gated mode needs {self.approvers_min} enrolled approver(s)")

    def _prune(self, now):
        self.used = {i: u for i, u in self.used.items() if isinstance(u, dict) and now - u.get("used_at", 0) < USED_KEEP}
        self.uses = {i: [t for t in ts if now - t < USED_KEEP] for i, ts in self.uses.items() if isinstance(ts, list)}
        self.live = {i: t for i, t in self.live.items() if t["exp"] > now}

    def _save_used(self):
        if self.state_dir:
            approvers.write_json_0600(self._file(USED_FILE), {"used": self.used, "counters": self.counters, "uses": self.uses})

    def _save_records(self, approver_recs, delegation_recs):
        if self.state_dir:
            approvers.write_json_0600(self._file(APPROVERS_FILE), list(approver_recs))
            approvers.write_json_0600(self._file(DELEGATIONS_FILE), list(delegation_recs))

    def idle_left(self):
        if self.gated:
            return float("inf")             # no idle timeout in gated mode: the gate, not time, bounds the seed
        return min(self.last_mono + self.idle - self.clk.mono(), self.last_wall + self.idle - self.clk.wall())

    def hard_left(self):
        if self.gated:
            return float("inf")
        return min(self.t0 + HARD_CAP - self.clk.mono(), self.w0 + HARD_CAP - self.clk.wall())

    def tick(self):
        """Lock on expiry, a suspend/resume jump or a vanished socket. A gated agent has no timer: `owner lock` and a
        vanished socket still end it."""
        if self.gated:
            return None
        if self.idle_left() <= 0:
            return self.do_lock("idle expiry")
        if self.hard_left() <= 0:
            return self.do_lock("hard cap")
        off = self._boot_off()
        if off is not None and self.boot_off is not None and abs(off - self.boot_off) > BOOT_JUMP:
            return self.do_lock("suspend/resume")
        return None

    def socket_alive(self):
        try:
            st = os.lstat(self.sock_path)
        except OSError:
            return False
        return stat.S_ISSOCK(st.st_mode) and st.st_ino == self.ino

    def do_lock(self, why):
        if self.why_locked is None:
            self.why_locked = why
            self.stop.set()
        return why

    def shutdown(self):
        """Zero the seed page, drop the signer, unlink our socket. The caller exits the process (the real guarantee)."""
        self.signer = None
        if self.buf is not None:
            try:
                ctypes.memset(ctypes.addressof(ctypes.c_char.from_buffer(self.buf)), 0, len(self.buf))
            except Exception:
                pass
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            try:
                if self.socket_alive():
                    os.unlink(self.sock_path)
            except OSError:
                pass

    # -- the socket
    def bind(self):
        check_dir(os.path.dirname(self.sock_path))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            s.bind(self.sock_path)
        finally:
            os.umask(old)
        st = os.lstat(self.sock_path)
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != self.uid:
            s.close()
            raise AgentError("the socket is not ours")
        os.chmod(self.sock_path, 0o600)
        s.listen(MAX_CONN)
        self.sock, self.ino = s, st.st_ino

    def run(self):
        last_check = self.clk.mono()
        try:
            while not self.stop.is_set():
                r, _, _ = select.select([self.sock], [], [], 0.5)
                self.tick()
                if self.clk.mono() - last_check >= SOCKET_CHECK:
                    last_check = self.clk.mono()
                    if not self.socket_alive():
                        self.do_lock("socket removed")
                if not r or self.stop.is_set():
                    continue
                try:
                    conn, _ = self.sock.accept()
                except OSError:
                    continue
                with self.lock:
                    busy = self.conns >= MAX_CONN
                    if not busy:
                        self.conns += 1
                if busy:
                    self._reply_close(conn, {"ok": False, "why": "busy"})
                    continue
                threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        finally:
            self.shutdown()
        return self.why_locked

    @staticmethod
    def _reply_close(conn, obj):
        try:
            conn.settimeout(1.0)
            write_frame(conn, obj)
        except OSError:
            pass
        finally:
            conn.close()

    def _serve(self, conn):
        try:
            if self._peer_uid(conn) != self.uid:     # before reading a byte; closed without a reply
                return
            pid = peer_pid(conn)
            try:
                req = read_frame(conn, time.monotonic() + READ_DEADLINE)
            except (OSError, ValueError, TimeoutError):
                return
            with self.lock:
                if self.signer is None:
                    return
                reply = self._handle(req, pid)
            try:
                conn.settimeout(READ_DEADLINE)
                write_frame(conn, reply)
            except OSError:
                pass
        finally:
            conn.close()
            with self.lock:
                self.conns -= 1

    # -- the ops: status, sign, lock. There is no unlock and no op that returns the seed.
    def _log(self, pid, op, act, ok, why, **who):
        self.ring.append({"t": int(self.clk.wall()), "pid": pid, "exe": _peer_exe(pid), "op": op,
                          "act": act, "ok": ok, "why": why, **who})

    def _handle(self, req, pid):
        op = req.get("op")
        if op == "status":
            return self.status()
        if op == "lock":
            self._log(pid, "lock", "-", True, "")
            self.do_lock("owner lock")
            return {"ok": True}
        if op == "sign":
            return self._sign(req, pid)
        if op == "present":
            return self._present(req, pid)
        if op == "member":
            return self._member(req, pid)
        if op == "reload":
            return self._reload(pid)
        return {"ok": False, "why": "unknown op"}

    def status(self):
        now = self.clk.mono()
        out = {"ok": True, "public_key": base64.b64encode(self.signer.public_key()).decode(),
               "idle_left": None if self.gated else max(0, int(self.idle_left())),
               "hard_left": None if self.gated else max(0, int(self.hard_left())),
               "idle": self.idle, "modules": sorted(self.policy.modules), "gated": self.gated,
               "admitted": sorted(self.policy.admitted_session), "signed": self.signed,
               "budget_left": None if self.gated else SIGNS_TOTAL - self.signed,
               "mlocked": self.mlocked, "keystore": self.keystore, "backoff": max(0, int(self.backoff_until - now)),
               "allowlist": (["a presented ticket's exact bodies", "a live delegation's scope"] if self.gated else
                             ["admit (module device, --module only)", "revoke (device admitted this session)",
                              "set-config x-<module>:<key>"]),
               "ring": list(self.ring)}
        if self.gated:
            wall = self.clk.wall()
            out.update({"approvers": [{"id": r["id"], "label": r["label"], "kind": r["kind"], "revoked": "revoked" in r}
                                      for r in self.approvers.values()],
                        "delegations": [{"id": d["id"], "scope": d["scope"], "per_day": d["per_day"], "expires": d["expires"],
                                         "used_today": len([t for t in self.uses.get(d["id"], []) if wall - t < USED_KEEP])}
                                        for d in self.delegations.values()],
                        "tickets": sorted(i for i, t in self.live.items() if t["exp"] > wall),
                        "used_tickets": len(self.used), "rp_id": self.rp_id, "approvers_min": self.approvers_min})
        return out

    def _refuse(self, pid, act, why, op="sign", **who):
        now = self.clk.mono()
        while self.refusals and now - self.refusals[0] > 60:
            self.refusals.popleft()
        self.refusals.append(now)
        if len(self.refusals) >= REFUSALS_PER_MINUTE:
            self.backoff_until = now + BACKOFF
        self._log(pid, op, act, False, why, **who)
        return {"ok": False, "why": why}

    def _sign(self, req, pid):
        now = self.clk.mono()
        if now < self.backoff_until:
            return {"ok": False, "why": "busy"}
        try:
            body = base64.b64decode(req.get("body") or "", validate=True)
        except (ValueError, TypeError):
            return self._refuse(pid, "?", "bad body")
        ticket = req.get("ticket")
        while self.sign_times and now - self.sign_times[0] > 60:
            self.sign_times.popleft()
        if len(self.sign_times) >= SIGNS_PER_MINUTE or (self.signed >= SIGNS_TOTAL and not self.gated):
            self._log(pid, "sign", "-", False, "rate limit")
            return {"ok": False, "why": "busy"}
        who = {}
        if ticket is not None:
            if not self.gated:
                return self._refuse(pid, "?", "a ticket was presented but the agent is not in gated mode")
            ok, why, act, who = self._ticket_check(ticket, body)
        elif self.gated:
            ok, why, act, who = self._delegation_check(body)
        else:
            ok, why, act = self.policy.check(body)
        if not ok:
            return self._refuse(pid, act, why, **who)
        sig = self.signer.sign(body)
        self.policy.commit(body)
        self.signed += 1
        self.sign_times.append(now)
        self.last_mono, self.last_wall = now, self.clk.wall()          # a signed act resets the idle timer
        self._log(pid, "sign", act, True, "", **who)
        return {"ok": True, "sig": base64.b64encode(sig).decode(), "act": act}

    # -- gated mode (owner approval slice A1)
    def _ticket_check(self, ticket, body):
        """A body is signed under a ticket only if its hash is in that live, presented ticket and was not signed yet."""
        t = self.live.get(ticket) if isinstance(ticket, str) else None
        wall = self.clk.wall()
        if t is None or t["exp"] <= wall:
            self.live.pop(ticket, None) if isinstance(ticket, str) else None
            return False, "the ticket is not presented, already expired, or lost in a restart", "?", {"ticket": str(ticket)[:64]}
        who = {"ticket": ticket, "approver": t["approver"]}
        h = hashlib.sha256(body).hexdigest()
        if h not in t["acts"]:
            return False, "this body is not one of the ticket's approved acts", "?", who
        if h in t["signed"]:
            return False, "this act was already signed under the ticket", "?", who
        ok, why, act = self.policy.check(body, ticketed=True)
        if ok:
            t["signed"].add(h)
        return ok, why, act, who

    def _delegation_check(self, body):
        """Without a ticket a gated agent signs only an act inside a live delegation's scope, within its daily cap."""
        ok, why, act = self.policy.check(body, ticketed=True)
        if not ok:
            return False, why, act, {}
        obj = json.loads(body.decode("utf-8"))
        wall, capped = self.clk.wall(), False
        for d in self.delegations.values():
            sc = d["scope"]
            if d["id"] in self.dead_delegations or d["expires"] <= wall or sc["acts"] != [obj["action"]]:
                continue
            if obj["action"] == "set-config" and not approvers.key_in_scope(obj["key"], sc["keys"]):
                continue
            if obj["action"] == "admit" and obj["module"] not in sc["admit_modules"]:
                continue
            if obj["action"] not in approvers.DELEGATION_ACTS:
                continue
            uses = [t for t in self.uses.get(d["id"], []) if wall - t < USED_KEEP]
            if len(uses) >= d["per_day"]:
                capped = True
                continue
            before = self.uses.get(d["id"])
            self.uses[d["id"]] = uses + [wall]
            try:
                self._save_used()                         # the count survives a restart
            except OSError:
                self.uses[d["id"]] = before if before is not None else []
                return False, "cannot persist the delegation count", act, {"delegation": d["id"]}
            return True, "", act, {"delegation": d["id"]}
        return False, "the delegation's daily cap is reached" if capped else "no live delegation covers this act", act, {}

    def _present(self, req, pid):
        """`present`: a passkey assertion approves one ticket or a batch of up to BATCH_MAX. The agent never sees a body
        here, only the act hashes; it checks the assertion over exactly those hashes, then writes the ticket ids to the
        used set BEFORE the first signature so a restart cannot reopen them."""
        if not self.gated:
            return self._refuse(pid, "-", "not in gated mode", op="present")
        tickets, assertion = req.get("tickets"), req.get("assertion")
        if not isinstance(tickets, list) or not 0 < len(tickets) <= BATCH_MAX or not isinstance(assertion, dict):
            return self._refuse(pid, "-", "bad present request", op="present")
        pairs, norm = [], {}
        for t in tickets:
            acts = t.get("acts") if isinstance(t, dict) else None
            if (not isinstance(t, dict) or set(t) != {"id", "acts"} or not approvers.id_ok(t["id"]) or t["id"] in norm
                    or not isinstance(acts, list) or not 0 < len(acts) <= 64 or not all(approvers.hex64_ok(a) for a in acts)
                    or len(set(acts)) != len(acts)):
                return self._refuse(pid, "-", "bad ticket", op="present")
            norm[t["id"]] = set(acts)
            pairs.append((t["id"], approvers.acts_hash(acts)))
        wall = self.clk.wall()
        self._prune(wall)
        for i in norm:
            if i in self.used:
                return self._refuse(pid, i, "ticket already used", op="present", ticket=i)
        ap = self.approvers.get(assertion.get("credentialId")) if isinstance(assertion.get("credentialId"), str) else None
        if ap is None or "revoked" in ap:
            return self._refuse(pid, "-", "not an enrolled approver", op="present")
        ok, why, count = approvers.verify_assertion(ap, assertion, approvers.ticket_challenge(pairs), self.rp_id, self.origin,
                                                    self.allow_synced, self.counters.get(ap["id"], 0))
        if not ok:
            return self._refuse(pid, "-", why, op="present", approver=ap["id"])
        before = (dict(self.used), dict(self.counters))
        for i, h in pairs:
            self.used[i] = {"acts_hash": h, "used_at": int(wall), "approver": ap["id"]}
        self.counters[ap["id"]] = count
        try:
            self._save_used()
        except OSError:
            self.used, self.counters = before
            return self._refuse(pid, "-", "cannot persist the used-ticket set", op="present")
        for i, acts in norm.items():
            self.live[i] = {"acts": acts, "signed": set(), "exp": wall + TICKET_LIVE, "approver": ap["id"]}
        self.last_mono, self.last_wall = self.clk.mono(), wall
        self._log(pid, "present", ",".join(norm)[:120], True, "", approver=ap["id"])
        return {"ok": True, "tickets": list(norm), "expires_in": TICKET_LIVE}

    def _member(self, req, pid):
        """`member`: add or revoke an approver, add or revoke a delegation, under the section 4.3 rule. The ONLY place the
        agent signs under the record tag; no ticket, delegation or `sign` op reaches it."""
        kind, rec, asserts = req.get("kind"), req.get("record"), req.get("assertions")
        if (not self.gated or kind not in ("approver-add", "approver-revoke", "delegate-add", "delegate-revoke")
                or not isinstance(rec, dict) or not isinstance(asserts, list) or not 0 < len(asserts) <= 2
                or not all(isinstance(a, dict) for a in asserts)):
            return self._refuse(pid, str(kind)[:32], "bad membership request", op="member")
        rec = {k: v for k, v in rec.items() if k != "owner_sig"}
        wall = self.clk.wall()
        live = approvers.live_approvers(self.approvers.values())
        act = f"{kind} {str(rec.get('id'))[:64]}"
        need, exclude = (1 if len(live) == 1 else 2), None
        old = self.approvers.get(rec.get("id")) if kind == "approver-revoke" else self.delegations.get(rec.get("id"))
        if kind == "approver-add":
            err = approvers.approver_ok(rec) if "revoked" not in rec else "a new approver is not revoked"
            if not err and rec["id"] in self.approvers:
                err = "that credential id is already enrolled (ids are never reused)"
            if not err and abs(rec["enrolled"] - wall) > approvers.FRESH:
                err = "enrolled is not now"
        elif kind == "delegate-add":
            err = approvers.delegation_ok(rec, wall) if "revoked" not in rec else "a new delegation is not revoked"
            if not err and (rec["id"] in self.delegations or rec["id"] in self.dead_delegations):
                err = "that delegation id was used"
        else:
            need = 1
            if old is None or "revoked" in old:
                err = "no such live record"
            else:
                want = {**{k: v for k, v in old.items() if k != "owner_sig"}, "revoked": rec.get("revoked")}
                ok_ts = isinstance(rec.get("revoked"), int) and not isinstance(rec["revoked"], bool) \
                    and abs(rec["revoked"] - wall) <= approvers.FRESH
                err = None if rec == want and ok_ts else "a revoke must be the stored record plus a current `revoked`"
            if not err and kind == "approver-revoke":
                exclude = rec["id"]
                if len(live) <= 1:
                    err = "revoking the only approver is break-glass: use the owner passphrase at a terminal"
                elif len(live) - 1 < self.approvers_min:
                    err = f"that would leave fewer than approvers.min ({self.approvers_min})"
        if err:
            return self._refuse(pid, act, err, op="member")
        challenge, signers, counters = approvers.member_challenge(kind, rec), [], dict(self.counters)
        for a in asserts:
            ap = self.approvers.get(a.get("credentialId")) if isinstance(a.get("credentialId"), str) else None
            if ap is None or "revoked" in ap or ap["id"] == exclude or ap["id"] in signers:
                return self._refuse(pid, act, "each assertion must come from a different enrolled approver (not the one being revoked)", op="member")
            ok, why, count = approvers.verify_assertion(ap, a, challenge, self.rp_id, self.origin, self.allow_synced,
                                                        counters.get(ap["id"], 0))
            if not ok:
                return self._refuse(pid, act, why, op="member", approver=ap["id"])
            counters[ap["id"]] = count
            signers.append(ap["id"])
        if len(signers) != need:
            return self._refuse(pid, act, f"this change needs {need} approver assertion(s), got {len(signers)}", op="member")
        if kind in ("approver-add", "delegate-add") and rec["by"] not in signers:
            return self._refuse(pid, act, "`by` must be one of the approvers who asserted", op="member")
        signed = approvers.sign_record(rec, self.signer)
        apps, dels = dict(self.approvers), dict(self.delegations)
        dead = set(self.dead_delegations)
        if kind.startswith("approver"):
            apps[signed["id"]] = signed
            if kind == "approver-revoke":              # any approver revoke clears every delegation
                dead |= set(dels)
                dels = {}
        elif kind == "delegate-add":
            dels[signed["id"]] = signed
        else:
            dels.pop(signed["id"], None)
            dead.add(signed["id"])
        counters_before = self.counters
        self.counters = counters
        try:
            self._save_records(apps.values(), dels.values())
            self._save_used()
        except OSError:
            self.counters = counters_before
            return self._refuse(pid, act, "cannot persist the records", op="member")
        self.approvers, self.delegations, self.dead_delegations = apps, dels, dead
        self._log(pid, "member", act, True, "", approver=",".join(signers))
        return {"ok": True, "record": signed}

    def _reload(self, pid):
        """`reload`: re-read the record files after a break-glass change. Only owner-signed records count, a revoke is
        sticky, and a revoked or cleared delegation never returns."""
        if not self.gated or not self.state_dir:
            return {"ok": False, "why": "not in gated mode"}
        pub, wall = self.policy.owner_pub, self.clk.wall()
        apps = dict(self.approvers)
        revoked_new = False
        for r in approvers.load_records(self._file(APPROVERS_FILE), pub, approvers.approver_ok):
            cur = apps.get(r["id"])
            if cur is None:
                apps[r["id"]] = r
            elif "revoked" in r and "revoked" not in cur:
                apps[r["id"]] = r
                revoked_new = True
        dead, dels = set(self.dead_delegations), dict(self.delegations)
        for r in approvers.load_records(self._file(DELEGATIONS_FILE), pub, lambda r: approvers.delegation_ok(r, wall, new="revoked" not in r)):
            if "revoked" in r:
                dead.add(r["id"])
                dels.pop(r["id"], None)
            elif r["id"] not in dead and r["id"] not in dels and r["expires"] > wall:
                dels[r["id"]] = r
        if revoked_new:
            dead |= set(dels)
            dels = {}
        self.approvers, self.delegations, self.dead_delegations = apps, dels, dead
        self._log(pid, "reload", "-", True, "")
        return {"ok": True}


# ---- the client ----------------------------------------------------------------------------------------------------

class Client:
    def __init__(self, path):
        self.path = path
        self.last_act = ""

    def request(self, obj, timeout=READ_DEADLINE):
        d = os.path.dirname(self.path)
        try:
            check_dir(d)
            st = os.lstat(self.path)
        except (AgentError, OSError) as e:
            raise AgentError(f"no agent ({e})")
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            raise AgentError("the agent socket is not ours")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.path)
            try:
                write_frame(s, obj)
            except OSError:         # the agent answered "busy" and closed before we finished writing: read that answer
                pass
            return read_frame(s, time.monotonic() + timeout)
        except (OSError, ValueError, TimeoutError, ConnectionError) as e:
            raise AgentError(f"agent unreachable ({e.__class__.__name__})")
        finally:
            s.close()

    def status(self):
        r = self.request({"op": "status"})
        if not r.get("ok"):
            raise AgentError(r.get("why", "status refused"))
        return r

    def sign(self, body, ticket=None):
        """The 64-byte signature, or raises AgentError with the agent's reason. `ticket` names a presented ticket."""
        req = {"op": "sign", "body": base64.b64encode(body).decode()}
        if ticket is not None:
            req["ticket"] = ticket
        r = self.request(req)
        if not r.get("ok"):
            raise AgentError(r.get("why", "refused"))
        self.last_act = r.get("act", "")
        return base64.b64decode(r["sig"])

    def lock(self):
        return self.request({"op": "lock"}).get("ok", False)

    def present(self, tickets, assertion):
        """Approve `tickets` ([{id, acts: [sha256 hex]}]) with one passkey assertion. Raises AgentError if refused."""
        r = self.request({"op": "present", "tickets": tickets, "assertion": assertion})
        if not r.get("ok"):
            raise AgentError(r.get("why", "refused"))
        return r

    def member(self, kind, record, assertions):
        """The owner-signed record, or raises AgentError with the agent's reason."""
        r = self.request({"op": "member", "kind": kind, "record": record, "assertions": assertions})
        if not r.get("ok"):
            raise AgentError(r.get("why", "refused"))
        return r["record"]

    def reload(self):
        return self.request({"op": "reload"}).get("ok", False)


class ChainSigner(_sigseam.Signer):
    """The agent first, then the file key. `public_key()` comes from the agent's `status` (a public value, so no
    prompt is needed to learn the node holds the owner key). `sign` tries the agent; on a refusal or an unreachable
    agent it asks `fallback()` for a FileSigner (one passphrase prompt per process, as today), saying why on stderr.
    A refusal is never an error and never a silent drop."""

    __slots__ = ("_client", "_pub", "_fallback", "_say", "_fb", "_ticket", "_fail")

    def __init__(self, client, public_key, fallback, say=None, ticket=None, fail=RuntimeError):
        self._client, self._pub, self._fallback, self._fb = client, public_key, fallback, None
        self._ticket, self._fail = ticket, fail
        self._say = say or (lambda m: print(m, file=sys.stderr))   # every agent-signed act and every fallback is said

    def public_key(self):
        return self._pub

    def sign(self, message):
        if self._ticket is not None:
            # A presented ticket is a HARD gate (owner approval slice A1): the agent signs these exact bytes or nothing is
            # signed. No seed, no passphrase prompt, no second try, whatever the reason the agent said no.
            try:
                sig = self._client.sign(message, self._ticket)
                if _ed25519 is not None and not _ed25519.verify(message, sig, self._pub):
                    raise AgentError("the agent returned a signature that does not verify")
            except AgentError as e:
                raise self._fail(f"ticket {self._ticket} refused by the owner agent ({e}); nothing was signed and no "
                                 "passphrase fallback is used under a ticket")
            self._say(f"signed by agent under ticket {self._ticket}: {getattr(self._client, 'last_act', '')}")
            return sig
        if self._fb is None:
            try:
                sig = self._client.sign(message)
                if _ed25519 is not None and not _ed25519.verify(message, sig, self._pub):
                    raise AgentError("the agent returned a signature that does not verify")
                self._say(f"signed by agent: {getattr(self._client, 'last_act', '')}")
                return sig
            except AgentError as e:
                self._say(f"owner agent did not sign ({e}): using the passphrase")
        if self._fb is None:
            self._fb = self._fallback()
            if self._fb is None or self._fb.public_key() != self._pub:
                self._fb = None
                raise RuntimeError("the owner key could not be unlocked, so nothing was signed")
        return self._fb.sign(message)

    def __repr__(self):
        return "<ChainSigner>"


def gate_ok(status, acts):
    """Whether the agent would sign every act in `acts` for a whole command, with margin (design §3 Gates).
    `acts` is a list of (action, arg): ("admit", module), ("set-config", module), ("revoke", device_id or None for the device the same run
    just admitted). An empty list is the public-key check alone."""
    if status.get("gated"):
        return False        # a gated agent signs a ticket's bodies or a delegation's scope, never "whatever this command does"
    if status.get("idle_left", 0) < GATE_MIN_IDLE or status.get("hard_left", 0) < GATE_MIN_IDLE:
        return False
    if status.get("budget_left", 0) < GATE_MIN_BUDGET or status.get("backoff", 0) > 0:
        return False
    for action, arg in acts:
        if action == "admit":
            if arg not in status.get("modules", ()):
                return False
        elif action == "revoke":
            if arg is not None and arg not in status.get("admitted", ()):
                return False
        elif action == "set-config":
            if status.get("modules") and arg not in status["modules"]:     # no --module: the policy checks "installed"
                return False
        else:
            return False
    return True


# ---- the CLI: hive-mind owner unlock | lock | status -----------------------------------------------------------------

_DUR = {"s": 1, "m": 60, "h": 3600}


def parse_duration(text):
    """`15m`, `90s`, `2h` (a bare number is seconds), within the 1m to 8h bounds; ValueError otherwise."""
    t = str(text).strip().lower()
    try:
        secs = float(t[:-1]) * _DUR[t[-1]] if t[-1:] in _DUR else float(t)
    except (ValueError, IndexError):
        raise ValueError(f"{text!r} is not a duration (15m, 2h)")
    if not IDLE_MIN <= secs <= IDLE_MAX:
        raise ValueError("--for must be between 1m and 8h")
    return secs


def _clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _read_unlock_passphrase(fd):
    """The passphrase for `unlock`: `--passphrase-fd N` (one line, the automation path the tests use), else
    getpass on /dev/tty. $HIVE_OWNER_KEY_PASSPHRASE is ignored (env is readable by same-uid and child processes). No
    tty and no fd: refuse. Returns the passphrase, or None to cancel."""
    if fd is not None:
        data = bytearray()
        while True:
            c = os.read(fd, 1)
            if not c or c == b"\n":
                break
            data += c
        pw = data.decode("utf-8", "replace")
        for i in range(len(data)):
            data[i] = 0
        return pw or None
    try:
        tty = open("/dev/tty", "r+")
        tty.close()
    except OSError:
        raise AgentError("no terminal to ask for the passphrase on: run `owner unlock` at a terminal")
    import getpass
    try:
        pw = getpass.getpass("Owner key passphrase (blank line / Ctrl-C to cancel): ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return pw or None


def cmd_unlock(lib, argv, out=print):
    import argparse
    ap = argparse.ArgumentParser(prog="hive-mind owner unlock")
    ap.add_argument("--for", dest="idle", default=None, help="idle timeout, reset by each signed act (1m to 8h; default 15m)")
    ap.add_argument("--module", action="append", default=[], help="a module the agent may admit and configure (repeatable)")
    ap.add_argument("--gated", action="store_true",
                    help="gated mode: sign only what an approver's passkey approved (a presented ticket) or a live "
                         "delegation covers; no idle timeout and no 12h cap")
    ap.add_argument("--rp-id", help="--gated: the WebAuthn rpId, the owner node's MagicDNS name")
    ap.add_argument("--port", type=int, default=8444, help="--gated: the approval page's HTTPS port (default 8444)")
    ap.add_argument("--approvers-min", type=int, default=1, help="--gated: unrevoked approvers required (default 1)")
    ap.add_argument("--allow-synced", action="store_true", help="--gated: accept backup-eligible (synced) passkeys")
    ap.add_argument("--passphrase-source", choices=("prompt",) + keystore.SOURCES, default="prompt",
                    help="where the passphrase comes from: prompt (default), or the OS keystore it was stored in with "
                         "`owner keystore store`; a keystore source needs --gated (boot unlock)")
    ap.add_argument("--passphrase-fd", type=int, default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.passphrase_source != "prompt" and not a.gated:
        print("hive-mind: --passphrase-source other than prompt needs --gated: a stored passphrase may only unlock an agent "
              "that signs nothing without an approved ticket or a live delegation", file=sys.stderr)
        return 2
    if a.passphrase_source != "prompt" and a.passphrase_fd is not None:
        print("hive-mind: --passphrase-source and --passphrase-fd are exclusive", file=sys.stderr)
        return 2
    if a.gated and (a.idle or a.module):
        print("hive-mind: --gated has no --for and no --module: the gate, not time or a module list, bounds the agent",
              file=sys.stderr)
        return 2
    if not a.gated and (a.rp_id or a.allow_synced or a.approvers_min != 1 or a.port != 8444):
        print("hive-mind: --rp-id, --port, --approvers-min and --allow-synced belong to --gated", file=sys.stderr)
        return 2
    if a.gated and (not a.rp_id or not 0 < a.port < 65536 or a.approvers_min < 1
                    or not all(c.isalnum() or c in ".-" for c in a.rp_id)):
        print("hive-mind: --gated needs --rp-id HOST (the owner node's MagicDNS name), a valid --port, and --approvers-min >= 1",
              file=sys.stderr)
        return 2
    a.idle = a.idle or "15m"
    try:
        idle = parse_duration(a.idle)
    except ValueError as e:
        print(f"hive-mind: {e}", file=sys.stderr)
        return 2
    for m in a.module:
        if not vocabulary.valid_module_name(m):
            print(f"hive-mind: {m!r} is not a valid module name", file=sys.stderr)
            return 2
    if os.environ.get(lib._KEY_PASSPHRASE_ENV) is not None:
        print("hive-mind: $HIVE_OWNER_KEY_PASSPHRASE is ignored by `owner unlock` (the environment is readable by "
              "other processes); type the passphrase at the prompt.", file=sys.stderr)
    path = socket_path(lib.HIVE_HOME)
    check_dir(os.path.dirname(path), create=True)
    client = Client(path)
    try:
        st = client.status()
        if bool(st.get("gated")) != a.gated:
            print(f"hive-mind: an agent is already running in {'gated' if st.get('gated') else 'ungated'} mode: "
                  "`hive-mind owner lock` first", file=sys.stderr)
            return 1
        print("already unlocked (gated: no timer)" if st.get("gated") else
              f"already unlocked (idle expiry in {int(st['idle_left'] // 60)}m, hard cap in {int(st['hard_left'] // 3600)}h)")
        return 0
    except AgentError:
        pass
    try:
        lst = os.lstat(path)
        if not stat.S_ISSOCK(lst.st_mode) or lst.st_uid != os.getuid():
            print(f"hive-mind: {path} exists and is not our socket; refusing to start over it", file=sys.stderr)
            return 1
        os.unlink(path)                                   # a stale socket: connect was refused, and it is ours
    except FileNotFoundError:
        pass
    if not lib._is_file(lib.OWNER_SEALED_PATH):
        print("hive-mind: no sealed owner key on this device (run `hive-mind owner seal` first, or this is not the owner "
              "machine); nothing to unlock", file=sys.stderr)
        return 1
    now = time.time()
    if a.gated:
        print("Owner unlock, GATED: until `owner lock` or a reboot (no idle timeout, no 12h cap), the agent signs only acts\n"
              "an enrolled approver's passkey approved (a presented ticket) and acts inside a live delegation's scope.\n"
              "The seed stays in memory for the uptime. Nothing else is signed (transfer, rotate, forget, links, ...): "
              "each of those still asks for the passphrase.\n")
    else:
        print("Owner unlock: for the next %s of inactivity (at most 12h), a process running as you can obtain signatures for:\n"
              "  - admit a MODULE device for: %s\n  - revoke a module device this session admitted\n"
              "  - set x-<module>: config for: %s\n"
              "Nothing else is signed (transfer, rotate, forget, voting devices, links): each of those still asks for the "
              "passphrase. Expires about %s.\n"
              % (a.idle, ", ".join(a.module) or "(no module: a plain unlock admits nothing)",
                 ", ".join(a.module) or "installed modules", _clock(now + idle)))
    seed = None
    for attempt in range(1, 4):
        if a.passphrase_source != "prompt":
            src = str(a.passphrase_source)
            try:
                pw = keystore.fetch(src, lib._ensure_key_dir(), lib.HIVE_HOME)
            except keystore.KeystoreError as e:
                print(f"hive-mind: could not read the owner key's secret from the OS keystore: "
                      f"{keystore.REASONS.get(e.code, keystore.REASONS['failed'])}", file=sys.stderr)
                return 1
        else:
            pw = _read_unlock_passphrase(a.passphrase_fd)
        if pw is None:
            print("Cancelled: the owner key stays sealed.", file=sys.stderr)
            return 1
        try:
            import ownerkey
            seed = ownerkey.load_sealed(lib.OWNER_SEALED_PATH, pw, lib._owner_unseal)
            break
        except (ValueError, KeyError) as e:
            if attempt == 3 or a.passphrase_fd is not None or a.passphrase_source != "prompt":
                print(f"hive-mind: could not unlock the owner key ({e})"
                      + ("; the stored passphrase may be stale: `owner keystore store` again" if a.passphrase_source != "prompt" else ""),
                      file=sys.stderr)
                return 1
            print("Wrong passphrase, try again.", file=sys.stderr)
    pub = base64.b64encode(lib._sigseam.from_seed(seed).public_key()).decode()
    gov = lib._governance_state(merkle.read_all_entries(lib.JOURNAL_DIR))
    if gov.get("owner_pub") != pub:
        print("hive-mind: this key is not the hive's governance owner; not unlocking.", file=sys.stderr)
        return 1
    gated = None
    if a.gated:
        state_dir = str(lib._ensure_key_dir())
        n = len(approvers.live_approvers(approvers.load_records(os.path.join(state_dir, APPROVERS_FILE), pub,
                                                                approvers.approver_ok)))
        if n < a.approvers_min:
            print(f"hive-mind: gated mode needs {a.approvers_min} enrolled approver(s), {n} found: enrol one first with "
                  "`hive-mind owner approver add` (the owner passphrase, typed once at this terminal)", file=sys.stderr)
            return 1
        gated = {"state_dir": state_dir, "rp_id": a.rp_id, "origin": f"https://{a.rp_id}:{a.port}",
                 "approvers_min": a.approvers_min, "allow_synced": a.allow_synced}
    proc = spawn(seed, path, idle, pub, a.module, gated=gated,
                 keystore_source=None if a.passphrase_source == "prompt" else a.passphrase_source)
    for _ in range(100):
        try:
            client.status()
            break
        except AgentError:
            if proc.poll() is not None:
                print("hive-mind: the agent did not start", file=sys.stderr)
                return 1
            time.sleep(0.1)
    else:
        print("hive-mind: the agent did not start", file=sys.stderr)
        return 1
    if a.gated:
        print(f"unlocked (gated, agent pid {proc.pid}): no timer; `hive-mind owner lock` to end it")
    else:
        print(f"unlocked: idle expiry {a.idle}, hard cap 12h (agent pid {proc.pid}); `hive-mind owner lock` to end it")
    return 0


def cmd_lock(lib, argv, out=print):
    try:
        Client(socket_path(lib.HIVE_HOME)).lock()
    except AgentError:
        print("not unlocked")
        return 0
    print("locked: the agent has exited and its seed page is cleared")
    return 0


def _keystore_lines(lib, st):
    """Name the keystore in use (slice A3). A keystore source is a seed readable at rest by the same user: say so."""
    try:
        line = keystore.describe(lib._ensure_key_dir())
    except OSError:
        line = None
    if st is not None and st.get("keystore"):
        print(f"  keystore: the agent was unlocked from {st['keystore']}")
    if line:
        print(f"  keystore: {line}")
        if unit_installed():
            print("  boot unlock: the user unit is installed")
    elif st is not None and st.get("keystore"):
        print("  keystore: no record on this node (cleared since)")


def cmd_status(lib, argv, out=print):
    try:
        st = Client(socket_path(lib.HIVE_HOME)).status()
    except AgentError:
        print("locked (no agent)")
        _keystore_lines(lib, None)
        return 0
    print(f"unlocked{' (gated)' if st.get('gated') else ''}: public key {st['public_key']}")
    _keystore_lines(lib, st)
    if st.get("gated"):
        print(f"  no timer: ends at `owner lock` or a reboot; rpId {st['rp_id']}, approvers.min {st['approvers_min']}")
        print("  approvers: " + (", ".join(f"{r['label']} ({r['kind']}{', REVOKED' if r['revoked'] else ''})"
                                           for r in st["approvers"]) or "(none)"))
        for d in st["delegations"]:
            print(f"  delegation {d['id']}: {json.dumps(d['scope'], sort_keys=True)}, {d['used_today']}/{d['per_day']} today, "
                  f"expires {time.strftime('%Y-%m-%d', time.localtime(d['expires']))}")
        print(f"  tickets live: {', '.join(st['tickets']) or '(none)'}; used (24h): {st['used_tickets']}")
        print(f"  signed {st['signed']} act(s)")
    else:
        print(f"  idle expiry in {st['idle_left']}s (of {int(st['idle'])}s), hard cap in {st['hard_left']}s")
        print(f"  modules named: {', '.join(st['modules']) or '(none)'}; devices admitted this session: "
              f"{', '.join(st['admitted']) or '(none)'}")
        print(f"  signed {st['signed']} act(s), {st['budget_left']} left")
    if not st["mlocked"]:
        print("  seed page not locked in memory (mlock failed): swap may hold it; use encrypted swap")
    for r in st["ring"][-20:]:
        who = "".join(f" {k} {r[k]}" for k in ("ticket", "approver", "delegation") if k in r)
        print(f"  {_clock(r['t'])} pid {r['pid']} {r['exe']} {r['op']} {r['act']}{who} {'ok' if r['ok'] else 'REFUSED: ' + r['why']}")
    return 0


# ---- the membership verbs: hive-mind owner approver|delegate add|revoke|list (owner approval slice A1) ---------------------

def _owner_pub_b64(lib):
    return lib._governance_state(merkle.read_all_entries(lib.JOURNAL_DIR)).get("owner_pub")


def _records(lib, name, check):
    pub = _owner_pub_b64(lib)
    return approvers.load_records(os.path.join(str(lib.KEY_DIR), name), pub, check) if pub else []


def _need(kind, live):
    """How many approver assertions the section 4.3 table asks for, or 0 for the owner passphrase (break-glass)."""
    if not live:
        return 0
    if kind == "approver-revoke":
        return 1 if live > 1 else 0
    if kind == "delegate-revoke":
        return 1
    return 1 if live == 1 else 2


def _read_assertions(paths):
    out = []
    for p in paths:
        try:
            with open(p, "rb") as f:
                obj = json.loads(f.read(MAX_FRAME).decode())
        except (OSError, ValueError) as e:
            raise AgentError(f"cannot read assertion {p}: {e}")
        if not isinstance(obj, dict):
            raise AgentError(f"{p}: an assertion is a JSON object")
        out.append(obj)
    return out


def _member_cmd(lib, kind, rec, a, out=print):
    """Run one membership change. `rec` is the unsigned record. With enough approvers enrolled the gated agent checks the
    assertions and signs; with none (or `--break-glass`) the owner passphrase is typed at this terminal, once, and this
    process signs the record itself. `--plan --json` prints the record and the challenge an approver must sign."""
    apps = _records(lib, APPROVERS_FILE, approvers.approver_ok)
    live = len(approvers.live_approvers(apps))
    need = _need(kind, live)
    challenge = approvers.member_challenge(kind, rec)
    if a.plan:
        out(json.dumps({"schema": 1, "kind": kind, "record": rec, "challenge": approvers.b64u(challenge),
                        "assertions_needed": need, "break_glass": need == 0}, indent=1, sort_keys=True))
        return 0
    path = socket_path(lib.HIVE_HOME)
    client = Client(path)
    try:
        st = client.status()
    except AgentError:
        st = None
    if st is not None and st.get("gated") and not a.break_glass:
        if need == 0:
            print("hive-mind: this change is break-glass (the only approver, or none): add --break-glass to type the owner "
                  "passphrase at this terminal", file=sys.stderr)
            return 1
        try:
            signed = client.member(kind, rec, _read_assertions(a.assertion))
        except AgentError as e:
            print(f"hive-mind: refused: {e}", file=sys.stderr)
            return 1
        out(f"{kind} {rec['id']}: recorded (owner-signed by the gated agent)")
        return 0
    if need and not a.break_glass:
        print(f"hive-mind: {live} approver(s) are enrolled, so this needs {need} approver assertion(s) through a gated agent "
              "(`owner unlock --gated`); or add --break-glass to use the owner passphrase", file=sys.stderr)
        return 1
    return _break_glass(lib, kind, rec, a, client, st is not None and bool(st.get("gated")), out)


def _break_glass(lib, kind, rec, a, client, reload, out):
    """The owner passphrase, read on /dev/tty (never the environment), unseals the key in THIS process; the record is
    signed here and written to the key dir. A running gated agent is then told to reload (it accepts only owner-signed
    records, never un-revokes one)."""
    import ownerkey
    now, pub = int(time.time()), _owner_pub_b64(lib)
    apps = _records(lib, APPROVERS_FILE, approvers.approver_ok)
    dels = _records(lib, DELEGATIONS_FILE, lambda r: approvers.delegation_ok(r, now, new=False))
    err = None
    if kind == "approver-add":
        err = approvers.approver_ok(rec) or (f"{rec['id']} is already enrolled" if any(r["id"] == rec["id"] for r in apps) else None)
    elif kind == "delegate-add":
        err = approvers.delegation_ok(rec, now) or (f"{rec['id']} already exists" if any(r["id"] == rec["id"] for r in dels) else None)
    elif kind == "approver-revoke":
        err = None if any(r["id"] == rec["id"] and "revoked" not in r for r in apps) else "no such live approver"
    else:
        err = None if any(r["id"] == rec["id"] and "revoked" not in r for r in dels) else "no such live delegation"
    if err:
        print(f"hive-mind: {err}", file=sys.stderr)
        return 1
    if not lib._is_file(lib.OWNER_SEALED_PATH):
        print("hive-mind: no sealed owner key on this device: run this on the owner machine", file=sys.stderr)
        return 1
    print("Break-glass: the owner passphrase signs this one record. It is read here and nowhere else.")
    pw = _read_unlock_passphrase(a.passphrase_fd)
    if pw is None:
        print("Cancelled: nothing was signed.", file=sys.stderr)
        return 1
    try:
        seed = ownerkey.load_sealed(lib.OWNER_SEALED_PATH, pw, lib._owner_unseal)
    except (ValueError, KeyError) as e:
        print(f"hive-mind: could not unlock the owner key ({e}). Nothing was signed.", file=sys.stderr)
        return 1
    signer = lib._sigseam.from_seed(seed)
    if base64.b64encode(signer.public_key()).decode() != pub:
        print("hive-mind: this key is not the hive's governance owner; nothing was signed.", file=sys.stderr)
        return 1
    signed = approvers.sign_record(rec, signer)
    new_apps = [r for r in apps if r["id"] != signed["id"]] if kind.startswith("approver") else list(apps)
    new_dels = list(dels)
    if kind.startswith("approver"):
        new_apps.append(signed)
        if kind == "approver-revoke":          # any approver revoke clears every delegation (signed as revoked, so a reload sees it)
            new_dels = [approvers.sign_record({**{k: v for k, v in d.items() if k != "owner_sig"}, "revoked": rec["revoked"]}, signer)
                        for d in dels if "revoked" not in d]
    else:
        new_dels = [d for d in dels if d["id"] != signed["id"]] + [signed]
    d = str(lib._ensure_key_dir())
    approvers.write_json_0600(os.path.join(d, APPROVERS_FILE), new_apps)
    approvers.write_json_0600(os.path.join(d, DELEGATIONS_FILE), new_dels)
    if reload:
        client.reload()
    out(f"{kind} {rec['id']}: recorded (owner-signed with the passphrase)")
    return 0


def _common_flags(ap):
    ap.add_argument("--assertion", action="append", default=[], metavar="FILE",
                    help="a JSON file holding one passkey assertion {credentialId, authenticatorData, clientDataJSON, signature} (repeatable)")
    ap.add_argument("--plan", action="store_true", help="print the record and the challenge an approver must sign, and change nothing (needs --json)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--break-glass", action="store_true", help="use the owner passphrase (typed at this terminal) instead of approver assertions")
    ap.add_argument("--at", type=int, default=None, metavar="TS", help="the timestamp a plan printed (so the record is the one approved)")
    ap.add_argument("--by", help="the approver id this change is recorded under (default: the only approver)")
    ap.add_argument("--passphrase-fd", type=int, default=None, help=argparse.SUPPRESS)


def _by(lib, a):
    live = approvers.live_approvers(_records(lib, APPROVERS_FILE, approvers.approver_ok))
    if a.by:
        return a.by
    if len(live) == 1:
        return live[0]["id"]
    return "owner" if not live else None


def _finish(lib, a, kind, rec):
    if rec.get("by") is None:
        print("hive-mind: more than one approver is enrolled: name the one that will assert first with --by", file=sys.stderr)
        return 2
    if a.plan and not a.json:
        print("hive-mind: --plan prints JSON: add --json", file=sys.stderr)
        return 2
    return _member_cmd(lib, kind, rec, a)


def cmd_approver(lib, argv, out=print):
    import argparse
    ap = argparse.ArgumentParser(prog="hive-mind owner approver")
    sub = ap.add_subparsers(dest="verb", required=True)
    add = sub.add_parser("add", help="enrol an approver (a passkey): one tap while one is enrolled, two with two or more, "
                                     "the owner passphrase for the first")
    add.add_argument("--credential-id", required=True, help="the credential id (base64url), the approver's id")
    add.add_argument("--alg", choices=["es256", "eddsa"], required=True)
    add.add_argument("--pub", required=True, help="the raw public key, base64: 64 bytes x||y for es256, 32 for eddsa")
    add.add_argument("--label", required=True)
    add.add_argument("--kind", choices=approvers.KINDS, required=True)
    _common_flags(add)
    rev = sub.add_parser("revoke", help="revoke an approver (one OTHER approver's tap, or the owner passphrase); clears every delegation")
    rev.add_argument("--credential-id", required=True)
    _common_flags(rev)
    sub.add_parser("list", help="the enrolled approvers")
    a = ap.parse_args(argv)
    if a.verb == "list":
        for r in _records(lib, APPROVERS_FILE, approvers.approver_ok):
            out(f"{r['id']}  {r['label']}  {r['kind']}  {'ES256' if r['alg'] == -7 else 'EdDSA'}  enrolled "
                f"{time.strftime('%Y-%m-%d', time.localtime(r['enrolled']))}" + ("  REVOKED" if "revoked" in r else ""))
        return 0
    at = a.at or int(time.time())
    if a.verb == "add":
        try:
            raw = base64.b64decode(a.pub, validate=True)
        except ValueError:
            print("hive-mind: --pub is not base64", file=sys.stderr)
            return 2
        rec = {"id": a.credential_id, "alg": approvers.ALG_ES256 if a.alg == "es256" else approvers.ALG_EDDSA,
               "pub": base64.b64encode(raw).decode(), "label": a.label, "kind": a.kind, "enrolled": at, "by": _by(lib, a)}
        err = approvers.approver_ok(rec) if rec["by"] else None
        if err:
            print(f"hive-mind: {err}", file=sys.stderr)
            return 2
        return _finish(lib, a, "approver-add", rec)
    old = [r for r in _records(lib, APPROVERS_FILE, approvers.approver_ok) if r["id"] == a.credential_id and "revoked" not in r]
    if not old:
        print("hive-mind: no such live approver", file=sys.stderr)
        return 1
    rec = {**{k: v for k, v in old[0].items() if k != "owner_sig"}, "revoked": at}
    return _finish(lib, a, "approver-revoke", {**rec, "by": old[0]["by"]})


def cmd_delegate(lib, argv, out=print):
    import argparse
    ap = argparse.ArgumentParser(prog="hive-mind owner delegate")
    sub = ap.add_subparsers(dest="verb", required=True)
    add = sub.add_parser("add", help="add a delegation: a scoped, capped, expiring right to sign without a ticket")
    add.add_argument("--id", required=True)
    add.add_argument("--act", choices=approvers.DELEGATION_ACTS, required=True)
    add.add_argument("--key", action="append", default=[], help="set-config: x-<module>:* or an exact x-<module>:<key> (repeatable)")
    add.add_argument("--module", action="append", default=[], help="admit: a module whose device may be admitted (repeatable)")
    add.add_argument("--per-day", type=int, required=True)
    add.add_argument("--days", type=float, required=True, help=f"lifetime in days (at most {approvers.MAX_DELEGATION_DAYS})")
    _common_flags(add)
    rev = sub.add_parser("revoke", help="revoke a delegation (one approver's tap, or the owner passphrase)")
    rev.add_argument("--id", required=True)
    _common_flags(rev)
    sub.add_parser("list", help="the delegations")
    a = ap.parse_args(argv)
    now = int(time.time())
    if a.verb == "list":
        for r in _records(lib, DELEGATIONS_FILE, lambda r: approvers.delegation_ok(r, now, new=False)):
            out(f"{r['id']}  {json.dumps(r['scope'], sort_keys=True)}  {r['per_day']}/day  expires "
                f"{time.strftime('%Y-%m-%d', time.localtime(r['expires']))}" + ("  REVOKED" if "revoked" in r else ""))
        return 0
    at = a.at or now
    if a.verb == "add":
        scope = {"acts": [a.act], "keys": a.key} if a.act == "set-config" else {"acts": [a.act], "admit_modules": a.module}
        rec = {"id": a.id, "scope": scope, "per_day": a.per_day, "expires": at + int(a.days * 86400), "created": at,
               "by": _by(lib, a)}
        err = approvers.delegation_ok(rec, at) if rec["by"] else None
        if err:
            print(f"hive-mind: {err}", file=sys.stderr)
            return 2
        return _finish(lib, a, "delegate-add", rec)
    old = [r for r in _records(lib, DELEGATIONS_FILE, lambda r: approvers.delegation_ok(r, now, new=False))
           if r["id"] == a.id and "revoked" not in r]
    if not old:
        print("hive-mind: no such live delegation", file=sys.stderr)
        return 1
    return _finish(lib, a, "delegate-revoke", {**{k: v for k, v in old[0].items() if k != "owner_sig"}, "revoked": at})


def _stdin_request(keys):
    """The one JSON object a JSON-on-stdin verb takes, or None after saying why. Never reads a terminal and never
    prompts: a terminal on stdin is refused. The size is capped before parsing."""
    try:
        if os.isatty(0):
            print("hive-mind: this verb reads one JSON object on stdin, not a terminal", file=sys.stderr)
            return None
        raw = sys.stdin.buffer.read(STDIN_MAX + 1)
        if len(raw) > STDIN_MAX:
            print("hive-mind: the request on stdin is too large", file=sys.stderr)
            return None
        req = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        print("hive-mind: stdin is not one JSON object", file=sys.stderr)
        return None
    if not isinstance(req, dict) or set(req) != set(keys):
        print("hive-mind: the request must be an object with exactly: " + ", ".join(sorted(keys)), file=sys.stderr)
        return None
    return req


def _forward(lib, op, req, out):
    """Send one request to the gated agent and print its reply as JSON. 0 if the agent said ok, 1 if it refused or is not
    reachable (the reply still goes to stdout, `{"ok": false, "why": ...}`). The agent, not this verb, is the gate."""
    try:
        reply = Client(socket_path(lib.HIVE_HOME)).request({"op": op, **req})
    except AgentError as e:
        reply = {"ok": False, "why": str(e)}
    out(json.dumps(reply))
    return 0 if reply.get("ok") is True else 1


def cmd_present(lib, argv, out=print):
    import argparse
    argparse.ArgumentParser(prog="hive-mind owner present",
                            description="Approve tickets with one passkey assertion: reads {tickets, assertion} as JSON on stdin, "
                                        "forwards it to the gated agent's `present` op and prints the agent's reply as JSON. "
                                        "Exit 0 approved, 1 refused or no agent, 2 bad request.").parse_args(argv)
    req = _stdin_request(("tickets", "assertion"))
    return 2 if req is None else _forward(lib, "present", req, out)


def cmd_member(lib, argv, out=print):
    import argparse
    argparse.ArgumentParser(prog="hive-mind owner member",
                            description="Add or revoke an approver or a delegation: reads {kind, record, assertions} as JSON on "
                                        "stdin, forwards it to the gated agent's `member` op and prints the agent's reply "
                                        "(with the owner-signed record) as JSON. Exit 0 recorded, 1 refused or no agent, 2 bad request."
                            ).parse_args(argv)
    req = _stdin_request(("kind", "record", "assertions"))
    return 2 if req is None else _forward(lib, "member", req, out)


# ---- boot unlock: the keystore and the user unit (owner approval slice A3) -------------------------------------------------

UNIT_NAME = "hive-owner-agent.service"


def unit_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "systemd", "user", UNIT_NAME)


def unit_installed():
    return os.path.isfile(unit_path())


def _sh(argv):
    """Run one service-manager command; (returncode, output). The seam the tests replace."""
    import subprocess
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd="/")
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return r.returncode, (r.stdout + r.stderr).strip()


def _q(arg):
    """One ExecStart word: systemd splits on whitespace and expands `%` and `$`, so quote and double them."""
    esc = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{esc}"'


def render_unit(ctl_path, source, rp_id, port, approvers_min, allow_synced, hive_home):
    """The user unit. A oneshot that stays active: `owner unlock --gated` starts the agent and exits; the agent stays in
    the unit's cgroup and ends at `ExecStop` (`owner lock`) or at logout/shutdown. Nothing secret is in it."""
    start = [sys.executable, "-I", ctl_path, "owner", "unlock", "--gated", "--rp-id", rp_id, "--port", str(port),
             "--approvers-min", str(approvers_min), "--passphrase-source", source] + (["--allow-synced"] if allow_synced else [])
    stop = [sys.executable, "-I", ctl_path, "owner", "lock"]
    esc = f"HIVE_HOME={hive_home}".replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    env = f'Environment="{esc}"\n'
    return ("[Unit]\nDescription=HiveMind owner agent, gated, unlocked at boot from the OS keystore\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\n" + env +
            "ExecStart=" + " ".join(_q(w) for w in start) + "\nExecStop=" + " ".join(_q(w) for w in stop) + "\n"
            "[Install]\nWantedBy=default.target\n")


def cmd_keystore(lib, argv, out=print):
    import argparse
    ap = argparse.ArgumentParser(prog="hive-mind owner keystore")
    ap.add_argument("action", choices=("store", "clear", "status"))
    ap.add_argument("--source", choices=keystore.SOURCES)
    ap.add_argument("--passphrase-fd", type=int, default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    key_dir = lib._ensure_key_dir()
    if a.action == "status":
        line = keystore.describe(key_dir)
        print(f"keystore: {line}" if line else "keystore: none (the passphrase is typed at `owner unlock`)")
        print(f"boot unit: {'installed (' + unit_path() + ')' if unit_installed() else 'not installed'}")
        return 0
    if a.action == "clear":
        src = keystore.clear(key_dir, lib.HIVE_HOME)
        print(f"cleared the stored passphrase ({src})" if src else "nothing stored")
        if unit_installed():
            print("hive-mind: the boot unit is still installed and will fail to unlock; "
                  "`hive-mind owner boot-unit remove` removes it", file=sys.stderr)
        return 0
    if not a.source:
        print("hive-mind: owner keystore store needs --source dpapi|libsecret|keychain", file=sys.stderr)
        return 2
    ok, why = keystore.available(a.source)
    if not ok:
        print(f"hive-mind: {a.source} is not available here: {why}", file=sys.stderr)
        return 1
    if not lib._is_file(lib.OWNER_SEALED_PATH):
        print("hive-mind: no sealed owner key on this device; nothing to store a passphrase for", file=sys.stderr)
        return 1
    print(f"This stores the owner passphrase in {a.source}. Any process of your user on this node that can ask {a.source} for\n"
          "it can read it, so use it only for `owner unlock --gated` (a gated agent signs nothing without an approver's ticket).\n"
          "Do not run seats or `trust: full` modules on this node.\n")
    pw = _read_unlock_passphrase(a.passphrase_fd)
    if pw is None:
        print("Cancelled: nothing stored.", file=sys.stderr)
        return 1
    try:
        import ownerkey
        ownerkey.load_sealed(lib.OWNER_SEALED_PATH, pw, lib._owner_unseal)       # never store a passphrase that does not open it
    except (ValueError, KeyError) as e:
        print(f"hive-mind: that passphrase does not open the owner key ({e}); nothing stored", file=sys.stderr)
        return 1
    try:
        keystore.store(a.source, pw, key_dir, lib.HIVE_HOME, now=time.time())
    except keystore.KeystoreError as e:
        print(f"hive-mind: {e}", file=sys.stderr)
        return 1
    print(f"stored in {a.source}. Next: `hive-mind owner boot-unit install --rp-id HOST --passphrase-source {a.source}`")
    return 0


def cmd_boot_unit(lib, argv, out=print):
    import argparse
    ap = argparse.ArgumentParser(prog="hive-mind owner boot-unit")
    ap.add_argument("action", choices=("install", "remove", "status"))
    ap.add_argument("--rp-id")
    ap.add_argument("--port", type=int, default=8444)
    ap.add_argument("--approvers-min", type=int, default=1)
    ap.add_argument("--allow-synced", action="store_true")
    a = ap.parse_args(argv)
    path = unit_path()
    if a.action == "status":
        print(f"boot unit: {'installed at ' + path if unit_installed() else 'not installed'}")
        return 0
    if a.action == "remove":
        _sh(["systemctl", "--user", "disable", "--now", UNIT_NAME])
        try:
            os.unlink(path)
        except FileNotFoundError:
            print("boot unit: not installed")
            return 0
        _sh(["systemctl", "--user", "daemon-reload"])
        print("boot unit removed (lingering is left as it was: `loginctl disable-linger` ends it)")
        return 0
    rec = keystore.read_record(lib._ensure_key_dir())
    if rec is None:
        print("hive-mind: no passphrase stored: run `hive-mind owner keystore store --source ...` first", file=sys.stderr)
        return 1
    if not a.rp_id or not 0 < a.port < 65536 or a.approvers_min < 1 or not all(c.isalnum() or c in ".-" for c in a.rp_id):
        print("hive-mind: boot-unit install needs --rp-id HOST (the owner node's MagicDNS name), a valid --port and "
              "--approvers-min >= 1", file=sys.stderr)
        return 2
    rc, _ = _sh(["systemctl", "--user", "is-system-running"])
    if rc == 127:
        print("hive-mind: no user systemd manager here (on WSL enable systemd in /etc/wsl.conf)", file=sys.stderr)
        return 1
    ctl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hivemind_ctl.py")
    text = render_unit(ctl, rec["source"], a.rp_id, a.port, a.approvers_min, a.allow_synced, lib.HIVE_HOME)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    steps = [(["systemctl", "--user", "daemon-reload"], "daemon-reload"),
             (["systemctl", "--user", "enable", UNIT_NAME], "enable"),
             (["loginctl", "enable-linger", user], "enable-linger (the agent must start at boot, before a login)")]
    for argv2, what in steps:
        rc, msg = _sh(argv2)
        if rc != 0:
            print(f"hive-mind: boot unit written, but {what} failed: {msg}", file=sys.stderr)
            return 1
    print(f"boot unit installed ({path}), enabled, lingering on: the gated agent unlocks from {rec['source']} at boot.\n"
          "Start it now with `systemctl --user start " + UNIT_NAME + "` (or reboot).")
    return 0


COMMANDS = {"keystore": cmd_keystore, "boot-unit": cmd_boot_unit, "unlock": cmd_unlock, "lock": cmd_lock, "status": cmd_status, "approver": cmd_approver, "delegate": cmd_delegate,
            "present": cmd_present, "member": cmd_member}


# ---- the agent process --------------------------------------------------------------------------------------------

def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _harden():
    """Best effort: no core dump, not dumpable (stops same-uid /proc/pid/mem and unprivileged ptrace)."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass
    try:
        if sys.platform.startswith("linux"):
            _libc().prctl(4, 0, 0, 0, 0)                  # PR_SET_DUMPABLE
        elif sys.platform == "darwin":
            _libc().ptrace(31, 0, 0, 0)                   # PT_DENY_ATTACH
    except Exception:
        pass


def _read_seed(fd=0):
    got = bytearray()
    while len(got) < 32:
        chunk = os.read(fd, 32 - len(got))
        if not chunk:
            break
        got += chunk
    return got


def _agent_main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--for", dest="idle", type=float, required=True)
    ap.add_argument("--owner-pub", required=True)
    ap.add_argument("--module", action="append", default=[])
    ap.add_argument("--gated", action="store_true")
    ap.add_argument("--state-dir")
    ap.add_argument("--rp-id")
    ap.add_argument("--origin")
    ap.add_argument("--approvers-min", type=int, default=1)
    ap.add_argument("--allow-synced", action="store_true")
    ap.add_argument("--keystore", choices=keystore.SOURCES)
    a = ap.parse_args(argv)
    if a.keystore and not a.gated:
        return 2                # the agent itself refuses a keystore-sourced seed without the gate
    seed = _read_seed(0)
    os.close(0)
    if len(seed) != 32 or _ed25519 is None:
        return 2
    os.setsid()
    os.chdir("/")
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.closerange(3, 4096)
    _harden()
    buf = mmap.mmap(-1, mmap.PAGESIZE)
    buf[:32] = bytes(seed)
    for i in range(len(seed)):
        seed[i] = 0
    mlocked = False
    try:
        mlocked = _libc().mlock(ctypes.c_void_p(ctypes.addressof(ctypes.c_char.from_buffer(buf))), mmap.PAGESIZE) == 0
    except Exception:
        pass
    lib_holder = []

    def lib():
        if not lib_holder:
            import importlib.machinery
            import importlib.util
            root = os.path.dirname(os.path.abspath(__file__))
            loader = importlib.machinery.SourceFileLoader("hvlib_agent", os.path.join(root, "hv"))
            mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("hvlib_agent", loader))
            loader.exec_module(mod)
            lib_holder.append(mod)
        return lib_holder[0]

    def probe(device_id):
        try:
            h = lib()
            gov = h._governance_state(merkle.read_all_entries(h.JOURNAL_DIR))
        except Exception:
            return None
        if device_id in gov["purged"]:
            return "purged"
        return "admitted" if device_id in gov["admitted"] else "other"

    def installed(module):
        try:
            h = lib()
            data = json.loads((h.HIVE_HOME / ".modules.json").read_text())
            return isinstance(data, dict) and isinstance(data.get(module), dict)
        except Exception:
            return False

    try:
        agent = Agent(bytes(buf[:32]), a.socket, a.idle, Policy(a.owner_pub, a.module, probe, installed), mlocked=mlocked,
                      gated=a.gated, state_dir=a.state_dir, rp_id=a.rp_id, origin=a.origin,
                      approvers_min=a.approvers_min, allow_synced=a.allow_synced, keystore=a.keystore)
    except ValueError:
        return 2
    agent.buf = buf
    agent.bind()
    signal.signal(signal.SIGTERM, lambda *_: agent.do_lock("terminated"))
    agent.run()
    os._exit(0)                                           # exit, not zeroing, is the guarantee for what CPython can't reach


def spawn(seed, sock_path, idle, owner_pub_b64, modules, gated=None, keystore_source=None):
    """Start the agent process with the seed on an anonymous pipe (stdin). argv and env carry nothing secret."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    env = {k: v for k, v in os.environ.items() if k not in ("HIVE_OWNER_KEY_PASSPHRASE", "HIVE_OWNER_PASSPHRASE")}
    argv = [sys.executable, "-I", os.path.join(here, "owner_agent.py"), "--socket", sock_path,
            "--for", str(idle), "--owner-pub", owner_pub_b64]
    for m in modules:
        argv += ["--module", m]
    if gated:
        argv += ["--gated", "--state-dir", gated["state_dir"], "--rp-id", gated["rp_id"], "--origin", gated["origin"],
                 "--approvers-min", str(gated["approvers_min"])] + (["--allow-synced"] if gated["allow_synced"] else [])
    if keystore_source:
        argv += ["--keystore", keystore_source]
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, env=env, cwd="/")
    try:
        p.stdin.write(bytes(seed))
        p.stdin.close()
    except OSError:
        pass
    return p


if __name__ == "__main__":
    sys.exit(_agent_main(sys.argv[1:]) or 0)
