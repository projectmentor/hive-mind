#!/usr/bin/env bash
# hive-mind update  —  scripts/installer/_update.sh
set -euo pipefail

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

# The signed-manifest verdict as one word (official/offline_ok/signed/modified/…), offline and fast:
# `hv verify` only prints (and always exits 0), and its key-anchor layer goes to the network.
_verify_level() {
  python3 - "$HIVE_DIR" 2>/dev/null <<'PY' || echo unknown
import importlib.util, sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
root = Path(sys.argv[1])
loader = SourceFileLoader("hv_update_check", str(root / "hv"))
spec = importlib.util.spec_from_loader("hv_update_check", loader)
hv = importlib.util.module_from_spec(spec)
loader.exec_module(hv)
print(hv._verify_status(root, check_anchor=False)["level"])
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

info "Pulling latest from GitHub..."
# Normally a fast-forward. But if upstream history was REWRITTEN (e.g. a force-push to scrub a
# leaked secret from history), the local branch can no longer fast-forward and a plain pull aborts,
# stranding the node on the old history. Detect that case and hard-reset to the upstream — but ONLY
# when the working tree is clean, so a node with genuine local edits is never silently clobbered.
git -C "$HIVE_DIR" fetch --tags origin
_BR="$(git -C "$HIVE_DIR" rev-parse --abbrev-ref HEAD)"
if git -C "$HIVE_DIR" merge-base --is-ancestor HEAD "@{u}" 2>/dev/null; then
  git -C "$HIVE_DIR" pull --ff-only
  ok "Repo updated"
elif [ -z "$(git -C "$HIVE_DIR" status --porcelain)" ]; then
  info "Upstream history was rewritten — hard-resetting clean tree to origin/$_BR"
  git -C "$HIVE_DIR" reset --hard "@{u}"
  ok "Repo re-synced to rewritten upstream"
else
  echo "  Upstream diverged AND you have local changes — refusing to reset." >&2
  echo "  Commit/stash your changes, then: git -C \"$HIVE_DIR\" reset --hard @{u}" >&2
  exit 1
fi

# The pull above may have replaced THIS script on disk, but bash is still running
# the old copy from memory — so any update logic added in the new version (e.g.
# rewriting systemd units) would silently not run until a SECOND update. Re-exec
# the freshly-pulled copy once so the new logic applies on the FIRST update. The
# env-var guard prevents an infinite re-exec loop.
if [ -z "${HIVE_UPDATE_REEXEC:-}" ]; then
  export HIVE_UPDATE_REEXEC=1
  exec bash "$HIVE_DIR/scripts/installer/_update.sh" "$@"
fi

# sign.yml re-signs the manifest a few minutes AFTER each merge to main. A pull inside that window
# leaves a clean tree that differs from the signed manifest, and `hv doctor` reports it MODIFIED.
# Wait for the re-sign commit and pull it; if it hasn't landed, say so and carry on (#93).
RESIGN_WAIT_S="${HIVE_RESIGN_WAIT_S:-180}"
RESIGN_POLL_S="${HIVE_RESIGN_POLL_S:-15}"
if [ -z "$(git -C "$HIVE_DIR" status --porcelain)" ] && [ "$(_verify_level)" = modified ]; then
  info "Signed manifest is behind the code; waiting up to ${RESIGN_WAIT_S}s for the release re-sign..."
  _waited=0; _landed=0
  while [ "$_waited" -lt "$RESIGN_WAIT_S" ]; do
    sleep "$RESIGN_POLL_S"; _waited=$((_waited + RESIGN_POLL_S))
    git -C "$HIVE_DIR" fetch -q origin 2>/dev/null || continue
    if [ "$(git -C "$HIVE_DIR" rev-parse HEAD)" != "$(git -C "$HIVE_DIR" rev-parse "@{u}")" ] \
       && git -C "$HIVE_DIR" merge-base --is-ancestor HEAD "@{u}"; then
      git -C "$HIVE_DIR" pull -q --ff-only && _landed=1
      break
    fi
  done
  if [ "$_landed" = 1 ] && [ "$(_verify_level)" != modified ]; then
    ok "Release re-sign landed; pulled it"
  else
    warn "Manifest re-sign pending on GitHub; run 'hive-mind update' again in a few minutes"
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

echo ""
ok "Update complete"
echo ""
