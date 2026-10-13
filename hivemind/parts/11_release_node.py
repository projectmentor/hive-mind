def version_cmd(args):
    """Print the agent-facing contract version (SemVer). Adapters/installers can
    check this to confirm compatibility or pin to a major."""
    print(f"hv contract-version {CONTRACT_VERSION}")


_VERIFY_EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".nudge_state", "journal"}
# The trust artifacts are NOT part of the manifest they vouch for: verify.json holds the digest,
# verify.json.sig signs it, and hivemind.pub is the anchor (its integrity comes from the published
# copy at the canonical site, not from being self-listed). Listing any of them would be circular.
_VERIFY_EXCLUDE_NAMES = {"verify.json", "verify.json.sig", "hivemind.pub", "nudge.env",
                         ".peers.json", ".peer_candidates.json", ".device-key", ".device-id", ".owner-key", ".owner-pub", ".key-dir",
                         ".genesis-pin"}
# The ONE extensionless file the manifest covers (5848726950). Any other tracked file with no suffix is
# skipped, and a test fails when one appears, so adding a second entry point is a deliberate decision
# rather than a file that ships unsigned.
_VERIFY_EXTENSIONLESS = {"hv"}
_VERIFY_EXTS = {".py", ".sh", ".md", ".txt", ".json", ".mjs", ".yml", ".yaml", ".example", ".toml", ".cfg",
                ".html", ".css", ".js", ".svg"}   # .html/.css/.js/.svg: the dashboard SPA + assets the
                                                  # daemon serves are signed too (tamper-evident over HTTP)
# Tracked files the manifest does not cover and a pinned check tolerates. Nothing else outside the manifest
# may ride in under a good signature: a tracked __pycache__/*.pyc is loaded by CPython without reading its
# source. Each is inert on a node (license, ignore list, a Windows-only helper, a test fixture).
_VERIFY_PINNED_UNSIGNED_OK = {".gitignore", "LICENSE", "scripts/platform/windows/remove-portproxy.bat",
                              "tests/security/Dockerfile"}
_VERIFY_TRUST_ARTIFACTS = {"verify.json", "verify.json.sig", "hivemind.pub"}
OFFICIAL_VERIFY_URL = "https://raw.githubusercontent.com/projectmentor/hive-mind/main/verify.json"
# The trust anchor: the public key, served from our own TLS domain (a DIFFERENT origin than the
# code host). A fork can ship its own hivemind.pub, but it cannot change what this URL serves, so a
# bundled key that does not match this one is the tell. Keep this in sync with hivemind.pub on main.
OFFICIAL_PUBKEY_URL = os.environ.get(
    "HIVE_PUBKEY_URL", "https://hivemind.projectmentor.org/.well-known/hivemind.pub")
OFFICIAL_SOURCE = "github.com/projectmentor/hive-mind"


def _git_tracked(root):
    """The set of git-tracked files (relative paths), or None if not a git repo / git unavailable.
    Using the tracked set makes the manifest REPRODUCIBLE: it covers exactly the distributed source,
    so a local untracked file (e.g. .claude/settings.local.json) can never shift the digest."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, timeout=10)
        if out.returncode != 0:
            return None
        return [n.decode() for n in out.stdout.split(b"\x00") if n]
    except Exception:
        return None


def _source_manifest(root):
    """Deterministic fingerprint of the source code (the files that define behavior). Covers the
    git-tracked source (reproducible from any clean clone); excludes the trust artifacts themselves,
    runtime data, caches, and non-code files. Returns {version, files, digest}."""
    root = Path(root)
    tracked = _git_tracked(root)
    candidates = [root / t for t in tracked] if tracked is not None else list(root.rglob("*"))
    files = {}
    for p in sorted(candidates):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if set(rel.parts) & _VERIFY_EXCLUDE_DIRS:
            continue
        if p.name in _VERIFY_EXCLUDE_NAMES or p.name.startswith("store.db") or ".bak" in p.name:
            continue
        if p.name not in _VERIFY_EXTENSIONLESS and p.suffix.lower() not in _VERIFY_EXTS:
            continue
        try:
            files[str(rel)] = hashlib.sha256(p.read_bytes()).hexdigest()
        except Exception:
            continue
    digest = hashlib.sha256("\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode()).hexdigest()
    return {"version": CONTRACT_VERSION, "files": files, "digest": digest}


def _manifest_sequence(manifest_bytes):
    """The signing sequence in a verify.json's bytes: the integer `sequence` sign.yml signs in (the number of
    commits behind the commit being signed). A manifest without it, or with anything but a non-negative
    integer there, counts as 0, so a node that predates the field starts enforcing after one update."""
    try:
        s = json.loads(manifest_bytes).get("sequence")
    except Exception:
        return 0
    return s if isinstance(s, int) and not isinstance(s, bool) and s >= 0 else 0


def _b64bytes(s):
    try:
        return base64.b64decode("".join(str(s).split()), validate=False)
    except Exception:
        return None


def _fetch_official_pubkey():
    """Fetch the published public key from our own TLS domain (the trust anchor). Returns 32 raw
    bytes, or None if unreachable / not yet published. Best-effort; never raises.
    Sends a normal User-Agent: the default 'Python-urllib' UA is blocked (403) by the CDN."""
    try:
        req = urllib.request.Request(OFFICIAL_PUBKEY_URL, headers={
            "User-Agent": "Mozilla/5.0 (compatible; hivemind-verify/1.0; +https://hivemind.projectmentor.org)"})
        with urllib.request.urlopen(req, timeout=6) as r:
            return _b64bytes((r.read() or b"").decode("ascii", "ignore"))
    except Exception:
        return None


def _verify_status(root, check_anchor=True, pinned_pub=None):
    """The integrity + authenticity verdict, as data (shared by `verify` and `doctor`).
    Three independent layers, strongest last:
      1. INTEGRITY  — local source fingerprint matches the bundled manifest (verify.json). Catches
         local edits and accidental drift.
      2. SIGNATURE  — the manifest carries a valid Ed25519 signature (verify.json.sig) under the
         bundled public key (hivemind.pub). Catches a fork that changed code: it cannot forge a
         signature without the private key.
      3. KEY ANCHOR — the bundled public key matches the key published at our own TLS domain
         (a different origin than the code host). Catches a fork that ships its OWN key + self-signed
         manifest: the bundled key will not match the one served from hivemind.projectmentor.org.
    Returns {level, version, ok, lines}. `ok` is True once integrity holds and the bundled signature
    (if any) is valid; the key-anchor layer only changes the headline, never `ok`. check_anchor=False
    skips the network layer (fast, offline-safe). Never raises and never blocks.

    pinned_pub (32 raw bytes) is for judging a tree that is NOT installed yet (`hive-mind update` checks the
    fetched tree before it switches to it). The tree's own hivemind.pub is then no evidence at all: the manifest
    must verify under the pinned key, taken from the installed tree, and the tree must ship that same key
    (hivemind.pub is outside the manifest, so a swapped one would otherwise ride in under a good signature).
    Anything short of that is not ok: no signature, a different key, a bad signature."""
    root = Path(root)
    local = _source_manifest(root)
    try:
        manifest_bytes = (root / "verify.json").read_bytes()
        bundled = json.loads(manifest_bytes)
    except Exception:
        manifest_bytes, bundled = None, None
    sig = _b64bytes((root / "verify.json.sig").read_text()) if (root / "verify.json.sig").exists() else None
    bundled_pub = _b64bytes((root / "hivemind.pub").read_text()) if (root / "hivemind.pub").exists() else None
    ver = (bundled or {}).get("version", "?")

    # Layer 1: integrity.
    if not bundled:
        return {"level": "no_manifest", "version": ver, "ok": False, "lines": [
            f"⚠ No manifest to verify against. Reinstall from the official source: {OFFICIAL_SOURCE}"]}
    if local["digest"] != bundled.get("digest"):
        return {"level": "modified", "version": ver, "ok": False, "lines": [
            "⚠ This install has been MODIFIED locally — your files differ from the signed manifest.",
            f"  If you did not edit it yourself, reinstall from the official source: {OFFICIAL_SOURCE}"]}

    if pinned_pub is not None:
        if _ed25519 is None or not pinned_pub:
            return {"level": "unverifiable", "version": ver, "ok": False, "lines": [
                "⚠ Cannot check the signature here (no Ed25519 implementation or no pinned key)."]}
        if sig is None:
            return {"level": "unsigned", "version": ver, "ok": False, "lines": [
                f"⚠ v{ver} carries no release signature."]}
        if bundled_pub != pinned_pub:
            return {"level": "key_changed", "version": ver, "ok": False, "lines": [
                f"⚠ v{ver} ships a different signing key than the one this install pins."]}
        if not _ed25519.verify(manifest_bytes, sig, pinned_pub):
            return {"level": "sig_invalid", "version": ver, "ok": False, "lines": [
                f"⚠ v{ver}: the manifest is not signed by the key this install pins."]}
        tracked = _git_tracked(root)
        stray = None if tracked is None else sorted(
            t for t in tracked if t not in local["files"] and t not in _VERIFY_TRUST_ARTIFACTS
            and t not in _VERIFY_PINNED_UNSIGNED_OK)
        if stray is None or stray:
            return {"level": "unsigned_files", "version": ver, "ok": False, "lines": [
                f"⚠ v{ver} tracks files the signature does not cover"
                + (f": {', '.join(stray[:5])}" if stray else " (cannot list the tree).")]}
        return {"level": "signed", "version": ver, "ok": True, "lines": [
            f"✓ v{ver}: integrity OK and signed by the pinned key."]}

    # Layer 2: signature.
    if _ed25519 is None or sig is None or bundled_pub is None:
        return {"level": "integrity_only", "version": ver, "ok": True, "lines": [
            f"✓ Integrity OK: matches the bundled manifest for v{ver}. (No signature bundled yet — "
            "this build predates signed releases.)",
            f"  Confirm against {OFFICIAL_SOURCE} when online."]}
    if not _ed25519.verify(manifest_bytes, sig, bundled_pub):
        return {"level": "sig_invalid", "version": ver, "ok": False, "lines": [
            "⚠ SIGNATURE INVALID — the manifest is not signed by the HiveMind release key. This is "
            "a tampered or forged copy.",
            f"  Reinstall from the official source: {OFFICIAL_SOURCE}"]}
    if not check_anchor:
        return {"level": "signed", "version": ver, "ok": True, "lines": [
            f"✓ HiveMind v{ver}: integrity OK and the release signature is valid."]}

    # Layer 3: key anchor (does the bundled key match the one published on our domain?).
    official_pub = _fetch_official_pubkey()
    if official_pub is None:
        return {"level": "offline_ok", "version": ver, "ok": True, "lines": [
            f"✓ Official HiveMind v{ver}: integrity OK and the release signature is valid.",
            f"  Could not reach {OFFICIAL_PUBKEY_URL} to confirm the key is the published one (offline?)."]}
    if official_pub == bundled_pub:
        return {"level": "official", "version": ver, "ok": True, "lines": [
            f"✓ Official HiveMind v{ver} from ProjectMentor — verified.",
            "  Source integrity OK, release signature valid, and the signing key matches the one "
            "published at hivemind.projectmentor.org."]}
    return {"level": "fork", "version": ver, "ok": False, "lines": [
        "⚠ Signed, but with a key that does NOT match the official key published at "
        "hivemind.projectmentor.org. This is a fork or an impostor.",
        f"  Reinstall from the official source: {OFFICIAL_SOURCE}"]}


RESIGN_WINDOW_S = 1800   # #93: how long after a commit lands on main its sign.yml re-sign may still be pending


def _resign_window_verdict(clean, at_upstream, age_s, window_s=RESIGN_WINDOW_S):
    """#93, pure: may a `modified` verdict be a pending sign.yml re-sign rather than tampering?
    Only when the tree is clean, HEAD IS the fetched upstream commit, and that commit is younger than
    the window. A dirty tree is a local edit; HEAD off upstream is a local commit; an old commit that
    still fails is an unsigned push to main, which is exactly what `authenticity` exists to catch, so
    none of those may hide behind the hint."""
    return bool(clean and at_upstream and age_s is not None and 0 <= age_s < window_s)


def _resign_pending_hint(root):
    """The operator hint when `_resign_window_verdict` holds for this checkout, else None. Reads git
    facts only (no fetch, no network); any git error means no hint, so the check stays `fail`."""
    def git(*a):
        r = subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            raise RuntimeError(r.stderr)
        return r.stdout.strip()
    try:
        clean = git("status", "--porcelain") == ""
        at_upstream = git("rev-parse", "HEAD") == git("rev-parse", "@{u}")
        age_s = time.time() - int(git("log", "-1", "--format=%ct"))
    except Exception:
        return None
    if not _resign_window_verdict(clean, at_upstream, age_s):
        return None
    return (f"(clean checkout at its upstream commit, {int(age_s // 60)} min old: the release re-sign "
            "is probably still pending on GitHub; run `hive-mind update` again in a few minutes)")


# `hv verify` exit codes (#190). Not 2: argparse usage errors.
VERIFY_EXIT_VERIFIED = 0     # official: integrity, signature and key anchor all confirmed
VERIFY_EXIT_FAILED = 1       # modified, signature invalid, fork key, or no manifest: do not trust this install
VERIFY_EXIT_UNCHECKED = 3    # nothing wrong found, but a layer could not be checked (anchor unreachable, unsigned build)
_VERIFY_EXIT = {"official": VERIFY_EXIT_VERIFIED, "signed": VERIFY_EXIT_UNCHECKED,
                "offline_ok": VERIFY_EXIT_UNCHECKED, "integrity_only": VERIFY_EXIT_UNCHECKED}


def _verify_exit_code(level):
    """The `hv verify` exit code for a `_verify_status` level. Unknown levels fail closed."""
    return _VERIFY_EXIT.get(level, VERIFY_EXIT_FAILED)


def verify_cmd(args):
    """Integrity + authenticity check (prints the layered verdict; see _verify_status). Exits 0 only
    when verified; 1 when the install fails a check; 3 when it could not be fully checked. `hv doctor`
    stays advisory: it calls `_verify_status` itself and never this."""
    v = _verify_status(Path(__file__).resolve().parent)
    for line in v["lines"]:
        print(line)
    return _verify_exit_code(v["level"])


# `hv doctor migrate-identity`, the one-time 1.3 re-keying of a hostname journal (#130), exists only on 1.x.
_MIGRATION_GONE = ("A node whose journal is still under its hostname re-stamps it with `hv doctor migrate-identity` "
                   "on a 1.x release (1.28 is the last), then upgrades to 2.0.")


def _identity_show():
    """Show this device's device_id, pubkey + label (or its legacy hostname identity)."""
    seed = _device_seed()
    if seed is None:
        print("No device key — this device uses its legacy hostname identity.")
        print(f"  node_id: {NODE_ID}")
        print("  `hv config identity init` mints one on a fresh install. A device with history under")
        print(f"  its hostname re-stamps it first, on a 1.x release: {_MIGRATION_GONE}")
        return
    pub = _ed25519.pub_from_seed(seed)
    print(f"device_id: {_device_id_for_pub(pub)}")
    print(f"pubkey:    {base64.b64encode(pub).decode()}")
    print(f"label:     {NODE_LABEL}")


def _identity_init(force):
    """Mint this device's Ed25519 device key (fresh install). Guards a populated journal unless forced."""
    if _ed25519 is None:
        print("ed25519 module unavailable; cannot create a device key.")
        return
    if DEVICE_KEY_PATH.exists() and not force:
        print(f"Device key already exists at {DEVICE_KEY_PATH}. Use --force to replace it.")
        return
    # Minting a key on a node that already authored entries under its hostname would split
    # its identity (old entries under the hostname, new ones under the device_id). On a
    # populated journal the migration is the correct path; only let init run on an empty one.
    if not force:
        mine = [e for e in merkle.read_all_entries(JOURNAL_DIR)
                if e.get("node_id") == socket.gethostname()]
        if mine:
            print(f"This device already has {len(mine)} entries under its hostname.")
            print(f"Re-stamp them first, on a 1.x release (not `hv config identity init`): {_MIGRATION_GONE}")
            print("Pass --force only if the journal is disposable.")
            return
    seed = os.urandom(32)
    HIVE_HOME.mkdir(parents=True, exist_ok=True)
    _write_device_key(seed)
    pub = _ed25519.pub_from_seed(seed)
    did = _device_id_for_pub(pub)
    DEVICE_ID_PATH.write_text(did + "\n")
    print(f"Device key created. device_id: {did}")
    print(f"  pubkey: {base64.b64encode(pub).decode()}")
    print(f"  seed:   {DEVICE_KEY_PATH} (private — never commit or sync it)")
    print("  Share the device_id + pubkey with peers (add them to .peers.json).")


def _key_announce():
    """Publish this device's signing key as a signed journal entry (`announce` kind:key), so peers
    can harvest an identity-bound capsule key for it. Needed only by a device that never authored
    anything (e.g. admitted directly by the owner without a join-request); a device that ever wrote
    a signed entry is already harvestable, and the guard makes this a no-op then. Works
    pre-admission and pre-owner by design (manual intent; ingest accepts it like a join-request)."""
    if _ensure_device_key():
        print(f"Device key created. device_id: {NODE_ID}")
    if _ed25519 is None or _device_seed() is None:
        print("No device key — this device uses its legacy hostname identity, so there is no key")
        print("to announce. Mint one with `hv config identity init` (a device with hostname history: see `hv doctor`).")
        return
    if not _announce_needed(merkle.read_all_entries(JOURNAL_DIR)):
        print(f"This device's key is already harvestable (a signed entry by {NODE_ID} exists) — "
              "nothing to announce.")
        return
    _emit_key_announce()
    print(f"Announced this device's signing key ({NODE_ID}) — propagates on the next sync.")
    print("  Owner: run `hv capsule rotate` afterwards to re-seal existing capsules to it.")


def _write_device_key(seed):
    """Write this device's seed into the key directory (0600), outside the working tree (2.0 PR 3a), and
    drop a legacy copy at the root so there is never a second one. Returns the path."""
    global DEVICE_KEY_PATH
    path = KEY_DIR / "device-key"
    _write_private(path, base64.b64encode(seed).decode() + "\n")
    try:
        if LEGACY_DEVICE_KEY_PATH.exists():
            LEGACY_DEVICE_KEY_PATH.unlink()
    except OSError:
        pass
    DEVICE_KEY_PATH = path
    return path


def _ensure_device_key():
    """Auto-mint a device key on first use if this is a FRESH node, so joining/capsules work without a
    manual `hv config identity init`. Returns True iff it just minted one (caller can notify). Safe by construction:
    it NEVER splits identity — it refuses when a key already exists, when HIVE_NODE_ID overrides the
    identity (tests / two instances on one box), or when the journal already has entries under this
    hostname (that case is the coordinated migration path, not a silent re-mint)."""
    global NODE_ID
    if _ed25519 is None or DEVICE_KEY_PATH.exists() or os.environ.get("HIVE_NODE_ID"):
        return False
    try:
        if any(e.get("node_id") == socket.gethostname()
               for e in merkle.read_all_entries(JOURNAL_DIR)):
            return False
    except Exception:
        return False
    seed = os.urandom(32)
    HIVE_HOME.mkdir(parents=True, exist_ok=True)
    _write_device_key(seed)
    pub = _ed25519.pub_from_seed(seed)
    did = _device_id_for_pub(pub)
    DEVICE_ID_PATH.write_text(did + "\n")
    NODE_ID = did                          # adopt the freshly minted identity for the rest of this run
    return True


def _is_daemon_cmdline(toks):
    """True iff an argv (token list) is a real `<...>/hv sync daemon` daemon, NOT a shell or
    script that merely MENTIONS the string. The daemon's argv ENDS in `<path>/hv sync daemon`;
    a `bash -c '... hv sync daemon ...'` wrapper does not (its script body sits earlier in argv).
    This guard is load-bearing: `--fix` kills these PIDs, so it must never match an innocent shell."""
    if not (len(toks) >= 3 and toks[-2:] == ["sync", "daemon"]
            and (toks[-3].endswith("/hv") or toks[-3] == "hv")):
        return False
    head = toks[0].lower()
    return "python" in head or head.endswith("/hv") or head == "hv"


def _sync_daemon_pids():
    """PIDs of real hive sync-daemon processes (Linux/Mac via pgrep). [] on any failure; excludes
    our own PID. `pgrep -f` alone would also match this command's parent shell, so every hit is
    re-checked against _is_daemon_cmdline."""
    try:
        out = subprocess.run(["pgrep", "-af", "sync daemon"],
                             capture_output=True, text=True, timeout=5).stdout
    except FileNotFoundError:
        # `pgrep` missing → we CANNOT enumerate orphans. Surface it instead of silently returning
        # [] (which reads as "no orphans" and lets `--fix` skip cleanup without telling anyone).
        print("doctor: `pgrep` not found — cannot detect orphan sync daemons on this host.",
              file=sys.stderr)
        return []
    except Exception as e:
        print(f"doctor: orphan-daemon scan failed ({e}).", file=sys.stderr)
        return []
    me = os.getpid()
    pids = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid != me and _is_daemon_cmdline(parts[1].split()):
            pids.append(pid)
    return pids


def _systemd_hive_sync():
    """(unit_exists, main_pid) for the `--user` hive-sync unit. main_pid is set only when the unit
    is active; None if dead/missing. Returns (False, None) where systemd isn't available."""
    def _show(prop):
        return subprocess.run(["systemctl", "--user", "show", "-p", prop, "--value", "hive-sync"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    try:
        unit_exists = _show("LoadState") == "loaded"
        active = subprocess.run(["systemctl", "--user", "is-active", "hive-sync"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        mp = _show("MainPID")
        main_pid = int(mp) if (active == "active" and mp.isdigit() and mp != "0") else None
        return unit_exists, main_pid
    except Exception:
        return False, None


def _launchd_hive_sync():
    """(loaded, None) for the macOS launchd hive-sync agent in the per-user GUI domain;
    (False, None) off-darwin or when launchctl can't see it. The macOS analogue of
    _systemd_hive_sync — lets `hv doctor --fix` restart a crashed launchd-managed daemon
    (orphan DETECTION already works cross-platform via _classify_orphans; only the restart
    ACTION needs this sibling). No MainPID equivalent is needed at the restart site."""
    if sys.platform != "darwin":
        return False, None
    try:
        r = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/com.projectmentor.hive-sync"],
            capture_output=True, text=True, timeout=5)
        return (r.returncode == 0), None
    except Exception:
        return False, None


def _classify_orphans(pids, main_pid, unit_exists):
    """Which daemon PIDs are orphans, i.e. NOT the one systemd is supposed to manage. Pure (testable).
    - managed daemon present: any OTHER daemon process is an orphan (a second one outside systemd).
    - a systemd unit exists but isn't running these: all of them are unmanaged orphans (the gotcha
      where the unit is dead yet a stale daemon still holds the port).
    - no systemd unit at all (manual management): only flag extras beyond a single survivor."""
    if not pids:
        return []
    if main_pid and main_pid in pids:
        return [p for p in pids if p != main_pid]
    if unit_exists:
        return list(pids)
    return [p for p in pids if p != min(pids)] if len(pids) > 1 else []


def _orphan_daemons():
    """(orphan_pids, unit_exists) for the local sync daemon."""
    pids = _sync_daemon_pids()
    unit_exists, main_pid = _systemd_hive_sync()
    return _classify_orphans(pids, main_pid, unit_exists), unit_exists


def _kill_orphans(orphans):
    """SIGTERM then (after a beat) SIGKILL any survivors. Returns the PIDs we signalled."""
    for p in orphans:
        subprocess.run(["kill", str(p)], capture_output=True)
    if orphans:
        time.sleep(1.5)
        for p in orphans:
            subprocess.run(["kill", "-9", str(p)], capture_output=True)
    return list(orphans)


# ── Claude Code agent integration: a STABLE shim, not inline hooks ─────────────────────────────────
# The reference adapter wires into a FOREIGN config (~/.claude/settings.json) we do not own. Inlining
# one entry per behavior there meant the SET of entries grew with every new behavior, and a node
# updated from before a behavior existed silently lacked it (the daemon self-heal never owned that
# file). So instead we register only a STABLE shim — `hive_dispatch.sh <event>`, one per lifecycle
# event — and decide WHICH behaviors run for each event inside that script, which lives in source
# control (and so under `hv verify`'s signature). New behaviors then ship by `git pull`, never by
# re-wiring ~/.claude; only a genuinely new event TYPE (rare) changes this spec.
#
# This is the SINGLE source of truth, shared by `hv doctor` (the `agent-hooks` check + the `--fix`
# self-heal the 15-min timer runs) and by integrations/claude-code/install.sh + the updater, which
# delegate via `hv wire claude`. Commands use the literal "$HOME/..." form so they dedupe
# byte-for-byte across nodes. (event, command, timeout) — timeouts cover the sum of a shim's
# behaviors (session telemetry ~12s + nudge ~8s on the session events). This is currently the only
# foreign-config integration; plugin agents (Hermes/OpenClaw/MCP) are our signed code and need no shim.
_CLAUDE_HOOKS_BASE = "$HOME/projects/hive-mind/scripts/common"
_CLAUDE_HOOK_SPEC = [
    ("SessionStart",     f"{_CLAUDE_HOOKS_BASE}/hive_dispatch.sh session-start", 20),
    ("SessionEnd",       f"{_CLAUDE_HOOKS_BASE}/hive_dispatch.sh sessionend",    20),
    ("UserPromptSubmit", f"{_CLAUDE_HOOKS_BASE}/hive_dispatch.sh user-prompt",   10),
    ("PreCompact",       f"{_CLAUDE_HOOKS_BASE}/hive_dispatch.sh precompact",     10),
    ("Notification",     f"{_CLAUDE_HOOKS_BASE}/hive_dispatch.sh notification",   10),   # 2.1: modules only
    ("Stop",             f"{_CLAUDE_HOOKS_BASE}/hive_dispatch.sh stop",           10),   # 2.1: modules only
]


def _claude_dir():
    """Where Claude Code keeps its config — honoring CLAUDE_CONFIG_DIR exactly as the CLI does."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")))


def _is_hive_hook_command(cmd):
    """True if a settings.json hook command belongs to Hive Mind: the dispatch shim, or anything under
    `hive-mind/scripts` (an older shim variant). Lets the reconcile prune our OWN stale wiring without ever
    touching a hook the user added. 2.0 (#136) dropped the pre-1.12 direct-hook names (`session_hook.sh`,
    `nudge_hook.sh`) as their own patterns: an install that old upgrades through a 1.x release first."""
    c = cmd or ""
    return "hive_dispatch.sh" in c or "hive-mind/scripts" in c


def _agent_hook_reconcile(cfg, hook_spec):
    """READ-ONLY diff of a parsed settings.json against an agent's canonical shim spec →
    (missing, stale):
      missing — spec shims not yet present (dedupe on (event, command));
      stale   — Hive-owned hooks present that are NOT in the current spec (legacy direct hooks left
                from before the dispatcher shim, to be pruned so behaviors don't double-fire).
    `hook_spec` is the cell's [(event, command, timeout), …] (the built-in Claude cell wraps
    `_CLAUDE_HOOK_SPEC`, so behavior is identical to the pre-refactor `_claude_hook_reconcile`)."""
    hooks = (cfg or {}).get("hooks", {}) or {}
    spec_cmds = {(ev, cmd) for ev, cmd, _ in hook_spec}
    def present(event, command):
        for grp in hooks.get(event, []) or []:
            for h in (grp.get("hooks", []) or []):
                if h.get("type") == "command" and h.get("command") == command:
                    return True
        return False
    missing = [(ev, cmd, to) for (ev, cmd, to) in hook_spec if not present(ev, cmd)]
    stale = []
    for ev, grps in hooks.items():
        for grp in (grps or []):
            for h in (grp.get("hooks", []) or []):
                cmd = h.get("command", "")
                if (h.get("type") == "command" and _is_hive_hook_command(cmd)
                        and (ev, cmd) not in spec_cmds):
                    stale.append((ev, cmd))
    return missing, stale


def _claude_hook_reconcile(cfg):
    """Back-compat alias: reconcile against the built-in Claude hook spec."""
    return _agent_hook_reconcile(cfg, _CLAUDE_HOOK_SPEC)


def _builtin_claude_cell():
    """The built-in `kind:agent` cell for Claude Code: wraps the canonical hook spec + the
    hive-memory skill link. Defined in signed source (not journaled), so it's covered by `hv verify`
    and resolves before any journaled cell of the same name. `hv wire claude`, the `agent-hooks`
    doctor check, and `--fix` all dispatch through this."""
    return {"kind": "agent", "name": "claude", "builtin": True,
            "spec": {"hooks": _CLAUDE_HOOK_SPEC,
                     "skill": {"name": "hive-memory",
                               "src_subpath": "integrations/claude-code/hive-memory"}}}


def _wire_agent(cell, write=False):
    """Reconcile a `kind:agent` cell's foreign-config wiring (its dispatch-shim hooks + an optional
    skill symlink) against the canonical spec. READ-ONLY unless write=True, in which case missing
    shims are added and legacy direct hooks are PRUNED (so behaviors never double-fire) — both
    surgically, leaving the user's own hooks untouched, after a one-time .bak.doctor backup; an
    absent/stale skill symlink is repaired too. Returns {applicable, missing, stale, skill_ok, wrote,
    skill_fixed}. Silent + no-op where there is no Claude Code on the node (integration not
    applicable). The built-in Claude cell wraps `_CLAUDE_HOOK_SPEC`, so this is byte-for-byte the
    former `_wire_claude_hooks`."""
    spec = (cell or {}).get("spec", {}) or {}
    hook_spec = spec.get("hooks", []) or []
    skill = spec.get("skill")
    claude_dir = _claude_dir()
    result = {"applicable": claude_dir.exists(), "missing": [], "stale": [],
              "skill_ok": True, "wrote": False, "skill_fixed": False}
    if not result["applicable"]:
        return result
    skill_link = skill_src = None
    if skill:
        skill_link = claude_dir / "skills" / skill["name"]
        skill_src = Path(__file__).resolve().parent / skill["src_subpath"]
        try:
            result["skill_ok"] = skill_link.is_symlink() and skill_link.resolve() == skill_src.resolve()
        except Exception:
            result["skill_ok"] = False
    path = claude_dir / "settings.json"
    try:
        cfg = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        cfg = {}
    result["missing"], result["stale"] = _agent_hook_reconcile(cfg, hook_spec)
    if not write or (not result["missing"] and not result["stale"] and result["skill_ok"]):
        return result
    if path.exists():
        try:
            shutil.copy2(str(path), str(path) + ".bak.doctor")
        except Exception:
            pass
    if skill and not result["skill_ok"] and skill_src.exists():
        try:
            (claude_dir / "skills").mkdir(parents=True, exist_ok=True)
            if skill_link.is_symlink() or skill_link.exists():
                skill_link.unlink()
            skill_link.symlink_to(skill_src)
            result["skill_ok"] = True
            result["skill_fixed"] = True
        except Exception:
            pass
    if result["missing"] or result["stale"]:
        hooks = cfg.setdefault("hooks", {})
        stale_set = set(result["stale"])
        for ev in list(hooks):                       # prune legacy Hive-owned hooks, keep the rest
            kept_groups = []
            for grp in (hooks.get(ev) or []):
                kept = [h for h in (grp.get("hooks") or [])
                        if not (h.get("type") == "command"
                                and (ev, h.get("command", "")) in stale_set)]
                if kept:
                    kept_groups.append({**grp, "hooks": kept})
            if kept_groups:
                hooks[ev] = kept_groups
            else:
                hooks.pop(ev, None)
        for (ev, cmd, to) in result["missing"]:      # add the stable shims
            hooks.setdefault(ev, []).append(
                {"hooks": [{"type": "command", "command": cmd, "timeout": to}]})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2) + "\n")
        result["wrote"] = True
    return result


def _builtin_cells():
    """Cells defined in signed source. They resolve BEFORE any journaled cell of the same name, so a
    core integration (the Claude agent) can never be shadowed by a synced journal entry."""
    return [_builtin_claude_cell()]


def _resolve_cell(name, entries=None):
    """Resolve a cell by name: built-ins first, then the journal projection (`_cell_state`)."""
    for c in _builtin_cells():
        if c.get("name") == name:
            return c
    if entries is None:
        entries = merkle.read_all_entries(JOURNAL_DIR)
    return _cell_state(entries).get(name)


def _detect_platform():
    """Coarse platform key for selecting a cell's `spec.platforms` block: wsl|linux|darwin|windows."""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        try:
            if "microsoft" in Path("/proc/version").read_text().lower():
                return "wsl"
        except Exception:
            pass
        return "linux"
    if sys.platform.startswith("win"):
        return "windows"
    return sys.platform


def _load_env_file(path):
    """Parse a dotenv file → {KEY: value}. Tolerant of `export`, quotes, comments; never shell-evals.
    This is the Phase-1 credential source for tool cells; Phase 2 reads from sealed capsules instead."""
    out = {}
    try:
        text = Path(path).expanduser().read_text()
    except Exception:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def _subst(s, mapping, as_env=False):
    """Replace {VAR} placeholders in a verify URL/header/cmd from `mapping` (unknown vars left as-is).
    as_env=True writes `${VAR}` instead of the value, for text a shell will parse: the secret then
    travels in the child's environment, never in the command line or the shell's input."""
    for k, v in mapping.items():
        s = s.replace("{" + k + "}", "${" + k + "}" if as_env else v)
    return s


_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_ENV_NAME_DENY = ("PATH", "HOME", "SHELL", "ENV", "BASH_*", "SHELLOPTS", "BASHOPTS", "IFS", "PS4",
                  "PROMPT_COMMAND", "CDPATH", "GLOBIGNORE", "TMPDIR", "LANG", "LC_*", "LD_*", "DYLD_*",
                  "*PROXY*", "HIVE_*", "PYTHON*", "NODE_OPTIONS", "GIT_*", "SSH_*")


_ENV_ENTRY_MAX = 131072                # Linux MAX_ARG_STRLEN: one environment entry


def _env_name_ok(name):
    """May `name` be a credential variable handed to a cell's shell? Upper-case identifier, and not one
    the shell, loader, proxy stack or hive itself reads."""
    return (isinstance(name, str) and bool(_ENV_NAME_RE.match(name))
            and not any(fnmatch.fnmatchcase(name, pat) for pat in _ENV_NAME_DENY))


def _env_name_problem(names):
    bad = [n for n in (names or []) if not _env_name_ok(n)]
    if not bad:
        return None
    return (f"credential name(s) {bad} are not allowed: use A-Z, 0-9 and _ (start with a letter, at most "
            f"64), and not a name the shell, loader, proxy or hive reads (PATH, HOME, BASH_*, LD_*, "
            f"*PROXY*, HIVE_*, GIT_*, ...)")


# The two variables that carry an owner passphrase (unlock the sealed owner key; encrypt an export or escrow).
# No child process this program starts for anyone else is given them.
OWNER_PASSPHRASE_ENVS = ("HIVE_OWNER_KEY_PASSPHRASE", "HIVE_OWNER_PASSPHRASE")


def _child_env(secrets):
    """Environment for a cell's shell: ours, minus the owner-key passphrase, plus the secrets."""
    env = dict(os.environ)
    for name in OWNER_PASSPHRASE_ENVS:
        env.pop(name, None)
    for k, v in secrets.items():       # reject what exec would refuse, the same way on every platform
        if "\0" in k or "\0" in v or len(k) + len(v) + 1 > _ENV_ENTRY_MAX:
            raise ValueError(f"credential {k!r} contains a NUL byte or is larger than {_ENV_ENTRY_MAX} bytes")
    env.update(secrets)
    return env


def _redact(text, secrets):
    for v in sorted((v for v in secrets.values() if v), key=len, reverse=True):
        text = text.replace(v, "***")
    return text


def _run_verify(cell, secrets):
    """Run a cell's `verify` (http or cmd) → True iff it passes. No `verify` block → trivially True.
    http: GET the url (with optional {VAR}-substituted headers); pass = `expect` substring present, or
    a 2xx when no expect. cmd: run under bash; pass = exit 0 and (expect substring present, if given)."""
    v = cell.get("verify") or {}
    if not v:
        return True
    if v.get("http"):
        try:
            req = urllib.request.Request(_subst(v["http"], secrets),
                                         headers={hk: _subst(hx, secrets)
                                                  for hk, hx in (v.get("headers") or {}).items()})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
                code = getattr(r, "status", 200)
            exp = v.get("expect")
            return (exp in body) if exp else (200 <= code < 300)
        except Exception:
            return False
    if v.get("cmd"):
        try:
            r = subprocess.run(["bash", "-lc", _subst(v["cmd"], secrets, as_env=True)],
                               env=_child_env(secrets), capture_output=True, text=True)
        except (ValueError, OSError):
            return False
        exp = v.get("expect")
        return r.returncode == 0 and (exp in (r.stdout + r.stderr) if exp else True)
    return True


def _wire_tool(cell, env_file=None):
    """Provision a `kind:tool` cell: ensure required credentials (from the per-device `.env` for now),
    run the steps for THIS platform, then verify. Idempotent — if `verify` already passes, do nothing.
    Returns a process-style int (0 ok)."""
    name = cell.get("name", "?")
    ef = env_file or "~/.claude/.env"
    requires = cell.get("requires") or []
    # Prefer sealed CAPSULES (encrypt-to-device); fall back to the per-device .env for un-sealed creds.
    bad = _env_name_problem(requires) if isinstance(requires, list) else "requires must be a list"
    if bad:
        print(f"{name}: {bad}")
        return 1
    secrets = {}
    if requires:
        caps = _capsule_state(merkle.read_all_entries(JOURNAL_DIR))
        sc = _my_curve_scalar()
        for k in requires:
            c = caps.get(k)
            if c and c.get("alg") != "tombstone-v1" and sc is not None:
                try:
                    secrets[k] = _capsule_open(c, sc, NODE_ID).decode("utf-8", "replace")
                except Exception:
                    pass
        env = _load_env_file(ef)
        for k in requires:
            if not secrets.get(k) and env.get(k):
                secrets[k] = env[k]
    missing = [k for k in requires if not secrets.get(k)]
    if missing:
        print(f"{name}: missing credential(s) {missing} — seal them with `hv capsule put <NAME>` "
              f"or add them to {ef}, then re-run.")
        return 1
    if _run_verify(cell, secrets):
        print(f"{name}: already provisioned (verify passed).")
        return 0
    plat = _detect_platform()
    plats = (cell.get("spec", {}) or {}).get("platforms", {}) or {}
    steps = (plats.get(plat) or plats.get("linux") or {}).get("steps", []) or []
    if not steps:
        print(f"{name}: no steps for platform {plat!r} (and verify not yet passing).")
        return 1
    for step in steps:
        try:
            env = _child_env({k: secrets[k] for k in requires})
            r = subprocess.run(["bash", "-lc", step], env=env, capture_output=True, text=True)
        except (ValueError, OSError) as e:
            print(f"{name}: step could not run ({plat}): {_redact(str(e), secrets)[:200]} "
                  f"(a credential may contain a NUL byte or be too large)")
            return 1
        if r.returncode != 0:
            out = _redact((r.stderr or r.stdout).strip(), secrets)[:400]
            print(f"{name}: step failed ({plat}): {_redact(step, secrets)}\n  {out}")
            return 1
    if _run_verify(cell, secrets):
        print(f"{name}: provisioned + verified ✓ ({plat})")
        return 0
    print(f"{name}: steps ran but verify did not pass.")
    return 1


def _wire_one(cell, name, env_file=None):
    """Provision a single resolved cell, dispatching on its `kind`."""
    if not cell:
        print(f"No cell named {name!r}. (`hv wire --list` to see cells, "
              f"`hv wire --add <file.json>` to publish one.)")
        return 1
    kind = cell.get("kind")
    if kind == "agent":
        w = _wire_agent(cell, write=True)
        if not w["applicable"]:
            print(f"{name}: agent config (~/.claude) not present on this node — nothing to wire.")
            return 0
        acts = []
        if w["missing"]:
            acts.append(f"wired {len(w['missing'])} dispatch shim(s)")
        if w["stale"]:
            acts.append(f"migrated {len(w['stale'])} legacy hook(s)")
        if w["skill_fixed"]:
            acts.append("relinked skill")
        print(f"{name}: " + ("; ".join(acts) if acts else "already wired") + ".")
        return 0
    if kind == "tool":
        return _wire_tool(cell, env_file)
    print(f"{name}: unknown cell kind {kind!r}.")
    return 1


def wire_cmd(args):
    """`hv wire <name>` provisions a cell, auto-dispatching by its `kind` (`agent`→reconcile foreign
    config; `tool`→run its recipe). `--comb <name>` runs every member cell; `--list`/`--show`/`--add`
    manage the registry. Absorbs the former `hv doctor wire-agent`, removed in 2.0."""
    entries = merkle.read_all_entries(JOURNAL_DIR)

    if getattr(args, "list", False):
        kind_f = getattr(args, "kind", None)
        cells = {c["name"]: c for c in _builtin_cells()}
        cells.update(_cell_state(entries))                       # journaled overrides built-in
        print("cells:")
        for n, c in sorted(cells.items()):
            if kind_f and c.get("kind") != kind_f:
                continue
            tag = "  (built-in)" if c.get("builtin") else ""
            print(f"  {n:<26} kind={c.get('kind','?')}{tag}")
        combs = _comb_state(entries)
        if combs and not kind_f:
            print("combs:")
            for n, c in sorted(combs.items()):
                print(f"  {n:<26} cells={c.get('cells', [])}")
        return

    if getattr(args, "show", None):
        c = _resolve_cell(args.show, entries) or _comb_state(entries).get(args.show)
        print(json.dumps(c, indent=2) if c else f"No cell or comb named {args.show!r}.")
        return

    if getattr(args, "add", None):
        try:
            payload = json.loads(Path(args.add).read_text())
        except Exception as e:
            print(f"Could not read cell/comb JSON from {args.add}: {e}")
            return
        t = payload.pop("type", None) or ("comb" if "cells" in payload else "cell")
        if t not in ("cell", "comb") or not payload.get("name") or not isinstance(payload["name"], str):
            print("JSON must be a cell/comb with a 'name' (type 'cell' or 'comb').")
            return
        v = payload.get("version", 1)
        if isinstance(v, bool) or not isinstance(v, int) or not -2**63 <= v < 2**63:
            print("'version' must be an integer.")
            return
        problem = merkle.envelope_problem({"node_id": NODE_ID, "seq": 1, "type": t, "payload": payload})
        if problem is not None:
            print(f"Not a valid {t}: {problem}.")
            return
        if t == "cell":
            req = payload.get("requires") or []
            problem = _env_name_problem(req) if isinstance(req, list) else "'requires' must be a list"
            if problem:
                print(f"Not a valid cell: {problem}.")
                return
        gov = _governance_state(entries)
        writers = gov.get("config", {}).get("cell_writers", "owner")
        if gov.get("owner_id"):                 # cells are executable: gate the write (hive #60)
            if writers == "owner" and not _holds_owner_key("wire --add (cell_writers=owner)"):
                print("Publishing cells/combs is owner-only here (config cell_writers=owner) — the "
                      "projection would decline a non-owner definition. Run on the owner device, or "
                      "`hive-mind config set cell_writers fertile`."); return
            if writers == "fertile" and NODE_ID not in gov.get("admitted", set()):
                print("Only admitted devices may publish cells/combs on this hive."); return
        payload.setdefault("version", 1)
        append_journal(t, _authorize_content_payload(payload))
        print(f"Published {t} {payload['name']!r} (version {payload['version']}).")
        return

    comb_name = getattr(args, "comb", None)
    if comb_name:
        comb = _comb_state(entries).get(comb_name)
        if not comb:
            print(f"No comb named {comb_name!r}.")
            return
        names = comb.get("cells", []) or []
        print(f"comb {comb_name!r}: wiring {len(names)} cell(s): {', '.join(names)}")
        for n in names:
            _wire_one(_resolve_cell(n, entries), n, getattr(args, "env_file", None))
        return

    name = getattr(args, "name", None)
    if not name:
        print("usage: hv wire <name> | --comb <name> | --list | --show <name> | --add <file.json>")
        return
    _wire_one(_resolve_cell(name, entries), name, getattr(args, "env_file", None))


