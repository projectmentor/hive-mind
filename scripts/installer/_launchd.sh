#!/usr/bin/env bash
# =============================================================================
# _launchd.sh — macOS launchd backend for the hive-mind supervisor.
#
# The Darwin analogue of _units.sh. Sourced by _service.sh only on macOS. Writes
# two per-user LaunchAgents and loads them into the GUI domain:
#
#   com.projectmentor.hive-sync     KeepAlive=true      ≙ systemd Restart=always
#   com.projectmentor.hive-doctor   StartInterval=900   ≙ hive-doctor.timer (15 min)
#
# Loaded with the modern `launchctl bootstrap gui/$UID` (not the deprecated
# `launchctl load`); restarted with `launchctl kickstart -k`. Logs go to
# ~/Library/Logs/hive-mind/. Every install bootout-then-bootstraps, so it is
# idempotent (bootstrap errors if the label is already loaded).
#
# All overridable for tests / CI (see _launchd_render + --render-test below).
# =============================================================================

_LAUNCHD_PREFIX="com.projectmentor"
_LAUNCHD_AGENTS_DIR="${_LAUNCHD_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
_LAUNCHD_LOG_DIR="${_LAUNCHD_LOG_DIR:-$HOME/Library/Logs/hive-mind}"

_launchd_domain() { echo "gui/$(id -u)"; }
_launchd_plist_path() { echo "$_LAUNCHD_AGENTS_DIR/$_LAUNCHD_PREFIX.$1.plist"; }

# Best interpreter for the plist ExecStart: Homebrew (Apple-Silicon/Intel) → PATH → system.
# A LaunchAgent has no login PATH, so the plist must carry an absolute interpreter path.
_launchd_python3() {
  if command -v python3 >/dev/null 2>&1; then command -v python3; return; fi
  local p
  for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$p" ] && { echo "$p"; return; }
  done
  echo /usr/bin/python3
}

# Resolve the tailscale CLI on macOS: PATH (brew) first, then the GUI app bundle.
# Prints the binary path, or nothing if neither is present.
_resolve_tailscale() {
  if command -v tailscale >/dev/null 2>&1; then command -v tailscale; return; fi
  local app="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
  [ -x "$app" ] && echo "$app"
}

# _launchd_render <hive_dir> — write both plists to $_LAUNCHD_AGENTS_DIR. No launchctl.
# Split out from launchd_install so tests/CI can render + lint without a GUI domain.
_launchd_render() {
  local hive_dir="$1" py plist
  if [ -z "$hive_dir" ] || [ ! -f "$hive_dir/hive_sync_daemon.py" ]; then
    echo "_launchd_render: invalid hive_dir '$hive_dir' (no hive_sync_daemon.py)" >&2
    return 1
  fi
  py="$(_launchd_python3)"
  mkdir -p "$_LAUNCHD_AGENTS_DIR" "$_LAUNCHD_LOG_DIR"

  # ── the sync daemon: always-on (KeepAlive ≙ Restart=always, ThrottleInterval ≙ RestartSec) ──
  plist="$(_launchd_plist_path hive-sync)"
  cat > "$plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$_LAUNCHD_PREFIX.hive-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>$py</string>
    <string>$hive_dir/hive_sync_daemon.py</string>
  </array>
  <key>WorkingDirectory</key><string>$hive_dir</string>
  <key>EnvironmentVariables</key>
  <dict><key>HIVE_HOME</key><string>$hive_dir</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$_LAUNCHD_LOG_DIR/hive-sync.log</string>
  <key>StandardErrorPath</key><string>$_LAUNCHD_LOG_DIR/hive-sync.log</string>
</dict>
</plist>
PLIST

  # ── self-heal: `hv doctor --fix` on load + every 15 min (≙ hive-doctor.timer) ──
  plist="$(_launchd_plist_path hive-doctor)"
  cat > "$plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$_LAUNCHD_PREFIX.hive-doctor</string>
  <key>ProgramArguments</key>
  <array>
    <string>$py</string>
    <string>$hive_dir/hv</string>
    <string>doctor</string>
    <string>--fix</string>
  </array>
  <key>WorkingDirectory</key><string>$hive_dir</string>
  <key>EnvironmentVariables</key>
  <dict><key>HIVE_HOME</key><string>$hive_dir</string></dict>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>$_LAUNCHD_LOG_DIR/hive-doctor.log</string>
  <key>StandardErrorPath</key><string>$_LAUNCHD_LOG_DIR/hive-doctor.log</string>
</dict>
</plist>
PLIST
}

# launchd_install <hive_dir> [svc] — render both plists then (re)load them into gui/$UID.
# The svc arg is accepted for signature parity with write_systemd_units; labels are fixed.
launchd_install() {
  local hive_dir="$1" dom label plist
  _launchd_render "$hive_dir" || return 1
  dom="$(_launchd_domain)"
  for label in hive-sync hive-doctor; do
    plist="$(_launchd_plist_path "$label")"
    if command -v plutil >/dev/null 2>&1; then
      plutil -lint "$plist" >/dev/null 2>&1 || { echo "launchd_install: malformed plist $plist" >&2; return 1; }
    fi
    launchctl bootout   "$dom/$_LAUNCHD_PREFIX.$label" 2>/dev/null || true   # idempotent re-install
    launchctl bootstrap "$dom" "$plist" 2>/dev/null || true
    launchctl enable    "$dom/$_LAUNCHD_PREFIX.$label" 2>/dev/null || true
  done
}

# launchd_restart [svc] — restart the named agent (default hive-sync).
launchd_restart() {
  launchctl kickstart -k "$(_launchd_domain)/$_LAUNCHD_PREFIX.${1:-hive-sync}" 2>/dev/null || true
}

# launchd_is_active [svc] — 0 if the agent is loaded in gui/$UID, else 1.
launchd_is_active() {
  launchctl print "$(_launchd_domain)/$_LAUNCHD_PREFIX.${1:-hive-sync}" >/dev/null 2>&1
}

# launchd_uninstall [svc] — bootout + remove both agents (idempotent; svc arg ignored).
launchd_uninstall() {
  local dom label
  dom="$(_launchd_domain)"
  for label in hive-sync hive-doctor; do
    launchctl bootout "$dom/$_LAUNCHD_PREFIX.$label" 2>/dev/null || true
    rm -f "$(_launchd_plist_path "$label")"
  done
}

# `bash _launchd.sh --render-test` — render both plists to a temp dir and `plutil -lint` them.
# Used by CI on macos-latest to validate the templates without bootstrapping into a GUI domain
# (which a headless runner may lack). Only fires when executed directly, never when sourced.
if [ "${BASH_SOURCE[0]:-x}" = "${0:-y}" ] && [ "${1:-}" = "--render-test" ]; then
  set -e
  _tmp="$(mktemp -d)"
  _LAUNCHD_AGENTS_DIR="$_tmp/agents"
  _LAUNCHD_LOG_DIR="$_tmp/logs"
  _repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  _launchd_render "$_repo"
  for _f in "$_LAUNCHD_AGENTS_DIR"/*.plist; do
    echo "lint: $_f"
    plutil -lint "$_f"
  done
  echo "render-test OK"
fi
