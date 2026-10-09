#!/usr/bin/env bash
# hive-mind update  —  scripts/installer/_update.sh
set -euo pipefail

# `-h`/`--help` prints usage and changes nothing; any other argument is refused (exit 2) BEFORE the
# command does anything (#224). Keep this ahead of every side effect.
ALLOW_UNSIGNED=0
ALLOW_REWIND=0
for _a in "$@"; do
  case "$_a" in
    -h|--help)
      echo "Usage: hive-mind update [--allow-unsigned] [--allow-rewind]"
      echo "  Pull the latest code, restart the daemon and re-verify (auto-heals after a force-push/rewrite)."
      echo "  The new code is checked against this install's pinned release key BEFORE it replaces anything;"
      echo "  a tree that is unsigned, modified or signed by another key is refused and nothing changes."
      echo "  --allow-unsigned  install it anyway (a fork, or a main whose re-sign has not landed). Your call."
      echo "  --allow-rewind    switch to a commit that is not a descendant of the installed one (an older tree, or a"
      echo "                    deliberately rewritten history). Without it such a tree is refused and nothing changes."
      exit 0 ;;
    --allow-unsigned) ALLOW_UNSIGNED=1 ;;
    --allow-rewind) ALLOW_REWIND=1 ;;
    *) echo "hive-mind update: unknown argument '$_a' (try --help)" >&2; exit 2 ;;
  esac
done

HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
SERVICE="hive-sync"

# Cross-platform supervisor seam — lets `update` re-apply the hardened daemon
# units (systemd on Linux/WSL, launchd on macOS) to already-installed nodes, not
# just fresh installs. Exposes service_install / service_restart.
_SVCLIB="$HIVE_DIR/scripts/installer/_service.sh"
if [ -f "$_SVCLIB" ]; then . "$_SVCLIB"; fi

GRN='\033[0;32m'; YLW='\033[0;33m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}[ok]${RST}  $*"; }
info() { echo -e "${BLD}[..]${RST}  $*"; }
warn() { echo -e "${YLW}[!!]${RST}  $*"; }

# The signed-manifest verdict on a commit that is NOT installed yet, as one word on the first line (signed,
# modified, unsigned, key_changed, sig_invalid, no_pin, unknown...) and the reason after it. The commit is
# checked out detached into a scratch clone, so the installed tree and branch are never touched, and it is
# judged by the INSTALLED hv against the key the installed tree pins: neither the fetched hv nor the fetched
# hivemind.pub is ever run or trusted. Offline: no network layer. Anything that goes wrong is `unknown`.
_verify_commit() {
  local _t _out
  _t="$(mktemp -d)" || { echo unknown; return; }
  if git clone -q --no-checkout "$HIVE_DIR" "$_t/tree" >/dev/null 2>&1 \
     && git -C "$_t/tree" checkout -q --detach "$1" >/dev/null 2>&1; then
    _out="$(PYTHONDONTWRITEBYTECODE=1 python3 - "$HIVE_DIR" "$_t/tree" 2>/dev/null <<'PY'
import importlib.util, sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
root, tree = Path(sys.argv[1]), Path(sys.argv[2])
loader = SourceFileLoader("hv_update_check", str(root / "hv"))
spec = importlib.util.spec_from_loader("hv_update_check", loader)
hv = importlib.util.module_from_spec(spec)
loader.exec_module(hv)
pub = hv._b64bytes((root / "hivemind.pub").read_text()) if (root / "hivemind.pub").is_file() else None
if not pub:
    print("no_pin\nThis install has no hivemind.pub to pin, so a new tree cannot be checked.")
else:
    v = hv._verify_status(tree, check_anchor=False, pinned_pub=pub)
    print(v["level"])
    for line in v["lines"]:
        print(line)
PY
)" || _out=unknown
  else
    _out=unknown
  fi
  rm -rf "$_t"
  echo "${_out:-unknown}"
}

# 2.0 (4c, decision h:696638b9b7): the update's last word on the pre-genesis forget grandfather. An owned hive
# whose `forget_writers` is still open gets an ACTION REQUIRED block: the facts that depend on it and the one
# command that closes it, `hive-mind doctor --fix`. Exit 3 means "print nothing more, fail the update"; 0 means
# closed or no owner. Reads no stdin: the y/N belongs to `doctor --fix`, run by the owner.
_forget_authz_gate() {
  HIVE_HOME="$HIVE_DIR" python3 - "$HIVE_DIR" 2>/dev/null <<'PY'
import importlib.util, sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
root = Path(sys.argv[1])
loader = SourceFileLoader("hv_update_gate", str(root / "hv"))
spec = importlib.util.spec_from_loader("hv_update_gate", loader)
hv = importlib.util.module_from_spec(spec)
loader.exec_module(hv)
entries = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
gov = hv._governance_state(entries)
c = hv._forget_authz_check(entries, gov)
if c is None or not c.get("open"):
    sys.exit(0)
here = hv._owner_key_state(gov) in ("held", "present")
facts = c["facts"]
print("")
print("\033[0;33m[!!]\033[0m  \033[1mACTION REQUIRED: the pre-genesis forget grandfather is open (forget-authz)\033[0m")
print("      2.0 is installed and running, and nothing that was forgotten has come back. But an unsigned")
print("      owner forget dated before genesis is still honoured, and an admitted device can write one (#122).")
if facts:
    print(f"      {len(facts)} fact(s) are kept forgotten only by such a forget:")
    for f in facts[:10]:
        text = " ".join(f["content"].split())
        print(f"        {f['sid']}  {text[:80]}{'…' if len(text) > 80 else ''}")
    if len(facts) > 10:
        print(f"        … and {len(facts) - 10} more (`hv doctor` lists them all)")
print("      Run once, " + ("here (this device keeps an owner key):" if here else "on the owner machine:"))
print("        hive-mind doctor --fix")
if facts:
    print("      It lists these facts and asks y/N. y re-signs those forgets, so the facts stay hidden, and closes")
    print("      the hive. N writes nothing, and names how to let a fact back instead.")
else:
    print("      No fact depends on the grandfather, so it closes without asking.")
print("      Until then `hv doctor` fails forget-authz, and every `hive-mind update` ends here.")
sys.exit(3)
PY
}

# The daemon's configured port (.peers.json `port`, default 9876), read the way the daemon reads it.
_daemon_port() {
  python3 -c "import sys; sys.path.insert(0, sys.argv[1]); import sync_common; \
print(sync_common.load_peers().get('port', 9876))" "$HIVE_DIR" 2>/dev/null || echo 9876
}

echo ""
echo -e "${BLD}hive-mind update${RST}"
echo "────────────────────────────────────"

info "Fetching latest from GitHub..."
# VERIFY, THEN SWITCH. The fetch only fills remote-tracking refs and tags; nothing the node runs changes
# until the commit it would switch to has passed the signed-manifest check against the key THIS install
# pins. A refusal leaves the branch and the working tree exactly as they were.
# Normally the switch is a fast-forward. But if upstream history was REWRITTEN (e.g. a force-push to scrub a
# leaked secret from history), the local branch can no longer fast-forward; the switch is then a hard-reset,
# but ONLY when the working tree is clean, so a node with genuine local edits is never silently clobbered.
git -C "$HIVE_DIR" fetch --tags origin
_BR="$(git -C "$HIVE_DIR" rev-parse --abbrev-ref HEAD)"
_HEAD="$(git -C "$HIVE_DIR" rev-parse HEAD)"
_NEW="$(git -C "$HIVE_DIR" rev-parse "@{u}")"

_switch_mode() {
  if git -C "$HIVE_DIR" merge-base --is-ancestor HEAD "$_NEW" 2>/dev/null; then _MODE=ff
  elif [ -z "$(git -C "$HIVE_DIR" status --porcelain)" ]; then _MODE=reset
  else _MODE=refuse; fi
}

if [ "$_HEAD" = "$_NEW" ]; then
  ok "Already at the latest commit"
else
  _switch_mode
  if [ "$_MODE" = refuse ]; then
    echo "  Upstream diverged AND you have local changes — refusing to reset." >&2
    echo "  Commit/stash your changes, then: git -C \"$HIVE_DIR\" reset --hard @{u}" >&2
    exit 1
  fi

  # A fetched commit that is not a descendant of the installed HEAD is either an OLDER tree (a rollback to an
  # earlier signed commit, whose signature is still valid) or a rewritten history. Neither is installed
  # without the operator saying so. This is not a full rollback check: the manifest signs file contents, not
  # commits, so an old signed tree wrapped in a descendant commit is a fast-forward (THREAT_MODEL). Nothing has changed yet.
  if [ "$_MODE" = reset ] && [ "$ALLOW_REWIND" = 0 ]; then
    if git -C "$HIVE_DIR" merge-base --is-ancestor "$_NEW" HEAD 2>/dev/null; then _WHAT="an OLDER commit than the installed one"
    else _WHAT="a rewritten history (not a descendant of the installed commit)"; fi
    echo "hive-mind update: REFUSED. origin/$_BR is at ${_NEW:0:12}, $_WHAT." >&2
    echo "  Installed: ${_HEAD:0:12}   Fetched: ${_NEW:0:12}" >&2
    echo "  Installing a commit that is not a descendant of the installed one is refused. Nothing was changed: $_BR is still at ${_HEAD:0:12}" >&2
    echo "  and the working tree is untouched." >&2
    echo "  If upstream history was rewritten on purpose and you trust it: hive-mind update --allow-rewind" >&2
    exit 1
  fi

  info "Checking ${_NEW:0:12} against this install's pinned release key..."
  _res="$(_verify_commit "$_NEW")"; _LEVEL="${_res%%$'\n'*}"

  # sign.yml re-signs the manifest a few minutes AFTER each merge to main, so the newest commit is
  # `modified` until that commit lands. Wait for it (#93), then verify the commit that carries it. A
  # commit that is still unsigned after the wait is refused, not installed.
  RESIGN_WAIT_S="${HIVE_RESIGN_WAIT_S:-180}"
  RESIGN_POLL_S="${HIVE_RESIGN_POLL_S:-15}"
  if [ "$_LEVEL" = modified ] && [ "$ALLOW_UNSIGNED" = 0 ]; then
    info "Signed manifest is behind the code; waiting up to ${RESIGN_WAIT_S}s for the release re-sign..."
    _waited=0
    while [ "$_waited" -lt "$RESIGN_WAIT_S" ]; do
      sleep "$RESIGN_POLL_S"; _waited=$((_waited + RESIGN_POLL_S))
      git -C "$HIVE_DIR" fetch -q --tags origin 2>/dev/null || continue
      _N2="$(git -C "$HIVE_DIR" rev-parse "@{u}")"
      if [ "$_N2" != "$_NEW" ] && git -C "$HIVE_DIR" merge-base --is-ancestor "$_NEW" "$_N2"; then
        _NEW="$_N2"; _switch_mode
        _res="$(_verify_commit "$_NEW")"; _LEVEL="${_res%%$'\n'*}"
        [ "$_LEVEL" = modified ] || break
      fi
    done
  fi

  if [ "$_LEVEL" = signed ]; then
    ok "Signature valid under the pinned key"
  elif [ "$ALLOW_UNSIGNED" = 1 ]; then
    warn "Verification failed ($_LEVEL) — installing ${_NEW:0:12} anyway because of --allow-unsigned"
  else
    echo "hive-mind update: REFUSED. Commit ${_NEW:0:12} on origin/$_BR did not verify ($_LEVEL):" >&2
    printf '%s\n' "${_res#*$'\n'}" | sed 's/^/  /' >&2
    echo "  Nothing was changed: $_BR is still at ${_HEAD:0:12} and the working tree is untouched." >&2
    if [ "$_LEVEL" = modified ]; then
      echo "  If the release re-sign has not landed yet, run 'hive-mind update' again in a few minutes." >&2
    fi
    echo "  To install it anyway (a fork you trust): hive-mind update --allow-unsigned" >&2
    exit 1
  fi

  if [ "$_MODE" = ff ]; then
    git -C "$HIVE_DIR" merge --ff-only -q "$_NEW"
    ok "Repo updated"
  else
    info "Upstream history was rewritten — hard-resetting clean tree to origin/$_BR"
    git -C "$HIVE_DIR" reset -q --hard "$_NEW"
    ok "Repo re-synced to rewritten upstream"
  fi
fi

# The switch above may have replaced THIS script on disk, but bash is still running
# the old copy from memory — so any update logic added in the new version (e.g.
# rewriting systemd units) would silently not run until a SECOND update. Re-exec
# the freshly-switched copy once so the new logic applies on the FIRST update. It runs only
# after the verification above has passed (or --allow-unsigned said otherwise). The
# env-var guard prevents an infinite re-exec loop.
if [ -z "${HIVE_UPDATE_REEXEC:-}" ]; then
  export HIVE_UPDATE_REEXEC=1
  exec bash "$HIVE_DIR/scripts/installer/_update.sh" "$@"
fi

# Pin the genesis on an upgrading node (hive-mind-private #14). From this release a node that has not
# pinned refuses inbound sync, so pin here. Pinning is operator state: since 2.0 it runs on the control plane
# (`hive-mind owner pin`), and the periodic `hv doctor --fix` only points at it (public #136).
# `--set` is safe and silent when there is nothing to do: it refuses to guess between two declarations,
# and it leaves an existing pin alone.
if [ -x "$HIVE_DIR/hv" ] && [ -f "$HIVE_DIR/hivemind_ctl.py" ]; then
  if python3 "$HIVE_DIR/hivemind_ctl.py" owner pin 2>/dev/null | grep -q "^genesis pinned:"; then
    :
  elif python3 "$HIVE_DIR/hivemind_ctl.py" owner pin --set >/dev/null 2>&1; then
    ok "Pinned this hive's genesis declaration"
  else
    warn "Genesis not pinned on this node — run 'hv doctor genesis' (it says which case you are in)"
  fi
fi

# Refresh the command symlinks so new subcommands land without a reinstall.
# (Older installs had a static dispatcher copy that never picked up new commands.)
info "Refreshing commands..."
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
ln -sf "$HIVE_DIR/scripts/installer/dispatcher.sh" "$BIN_DIR/hive-mind"
ln -sf "$HIVE_DIR/hv" "$BIN_DIR/hv"
ok "Commands refreshed (hive-mind, hv)"

# Order matters (#93): units, THEN restart, THEN rebuild. The daemon still running the OLD code can
# hold the store's write lock through a minutes-long rebuild, far past the 10 s busy timeout, so a
# rebuild before the restart aborted the whole update. Restarting first also brings the daemon up
# under the NEW unit file.
info "Refreshing supervisor units..."
if command -v service_install >/dev/null 2>&1; then
  service_install "$HIVE_DIR" "$SERVICE"
  ok "Units refreshed (hive-sync + hive-doctor self-heal)"
fi
if command -v cron_repoint_legacy_daemon >/dev/null 2>&1; then
  _repointed="$(cron_repoint_legacy_daemon)"
  [ -n "$_repointed" ] && ok "$_repointed"
fi

info "Restarting sync daemon..."
if command -v service_restart >/dev/null 2>&1; then
  service_restart "$SERVICE"
else
  systemctl --user restart "$SERVICE" 2>/dev/null || true
fi
# Wait for it to answer, so the rebuild below doesn't race the daemon's own first store catch-up.
PORT="$(_daemon_port)"
_up=0
for _ in $(seq 1 "${HIVE_DAEMON_WAIT_S:-15}"); do
  if curl -sf "http://127.0.0.1:${PORT}/sync/hello" >/dev/null 2>&1; then _up=1; break; fi
  sleep 1
done
if [ "$_up" = 1 ]; then ok "Daemon responding"; else echo "  Daemon may still be starting — check the daemon logs."; fi

# 2.1 (M2): the core's unit renderer may have changed, so installed modules' units are rendered again too. Only the
# files that changed are written and only their units restarted. It never fails the update: a module that cannot
# be re-applied is a warning that names it.
if [ -f "$HIVE_DIR/hivemind_ctl.py" ]; then
  _mod_out="$(python3 "$HIVE_DIR/hivemind_ctl.py" module reapply 2>&1)" \
    || _mod_out="${_mod_out:+$_mod_out$'\n'}hive-mind module: warning: module units could not be re-applied"
  while IFS= read -r _l; do [ -n "$_l" ] && warn "${_l#hive-mind module: warning: }"; done <<<"$_mod_out"
fi

info "Rebuilding database..."
if cd "$HIVE_DIR" && ./hv doctor rebuild; then
  ok "DB rebuilt"
else
  warn "Rebuild skipped (store busy). No need to rerun it: the daemon catches the store up on its next cycle, and so does the next hv command."
fi

# Re-assert the Claude Code integration so an update from before a hook existed picks it up here,
# not only on the 15-min self-heal timer. Idempotent; silent + harmless on a node without Claude Code.
if [ -d "$HOME/.claude" ] || command -v claude >/dev/null 2>&1; then
  info "Re-asserting Claude Code hooks..."
  "$HIVE_DIR/hv" wire claude >/dev/null 2>&1 && ok "Claude Code hooks wired" || true
fi

# Last, after the restart, the rebuild and the re-wire: an open forget grandfather fails the update (4c).
_gate=0; _forget_authz_gate || _gate=$?
if [ "$_gate" = 3 ]; then
  echo ""
  exit 1
fi
[ "$_gate" = 0 ] || warn "Could not check the forget grandfather; run 'hv doctor' to see forget-authz."

echo ""
ok "Update complete"
echo ""
