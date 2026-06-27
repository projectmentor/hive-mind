#!/usr/bin/env bash
# =============================================================================
# _service.sh — cross-platform supervisor seam for the hive-sync daemon.
#
# The supervisor that owns the daemon + the periodic self-heal differs by OS:
#   Linux / WSL : systemd --user units  (see _units.sh — the single source of
#                 truth, sourced and called VERBATIM here, so the Linux path is
#                 byte-identical and carries zero regression risk).
#   macOS       : launchd LaunchAgents  (see _launchd.sh).
#   Android     : termux-services / runit  (see _runit.sh). Termux has no
#                 systemd/launchd; `uname -s` reports Linux there, so the kind
#                 probe matches Android BEFORE falling through to systemd.
#
# Callers ask `_service_kind` / the `service_*` wrappers instead of hard-coding
# `systemctl`. The systemd branch delegates to the existing functions/commands,
# so nothing that reaches the Linux kernel changes; macOS simply gets a second
# branch. Sourced (not executed) by the installer scripts.
# =============================================================================

# Dir this file lives in — resolve so we can source siblings regardless of CWD.
_SERVICE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# systemd unit writer (Linux) — always available; a no-op when no user systemd bus.
if [ -f "$_SERVICE_LIB_DIR/_units.sh" ]; then . "$_SERVICE_LIB_DIR/_units.sh"; fi

# launchd: the supervisor key on macOS. runit: Android/Termux. `systemd` covers
# native Linux + WSL (where the concrete init may be systemctl/systemd/initd/cron
# — those sub-variants are handled by the installer, not here). Termux reports
# `uname -s`=Linux, so Android is matched first in the non-Darwin arm via
# `uname -o`=Android / $TERMUX_VERSION / the Termux data dir. $TERMUX_VERSION also
# lets CI force the runit branch on an ordinary Linux runner.
_service_kind() {
  case "$(uname -s)" in
    Darwin) echo launchd ;;
    *)
      if [ "$(uname -o 2>/dev/null)" = "Android" ] || [ -n "${TERMUX_VERSION:-}" ] \
         || [ -d /data/data/com.termux ]; then
        echo runit
      else
        echo systemd
      fi ;;
  esac
}

# Pull in the launchd backend only on macOS (defines launchd_* + _resolve_tailscale).
if [ "$(_service_kind)" = launchd ] && [ -f "$_SERVICE_LIB_DIR/_launchd.sh" ]; then
  . "$_SERVICE_LIB_DIR/_launchd.sh"
fi

# Pull in the runit backend only on Android/Termux (defines runit_*).
if [ "$(_service_kind)" = runit ] && [ -f "$_SERVICE_LIB_DIR/_runit.sh" ]; then
  . "$_SERVICE_LIB_DIR/_runit.sh"
fi

# service_install <hive_dir> [svc] — install + enable the supervisor units.
service_install() {
  case "$(_service_kind)" in
    launchd) launchd_install "$@" ;;
    runit)   runit_install "$@" ;;
    *)       write_systemd_units "$@" ;;
  esac
}

# service_restart [svc] — (re)start the sync daemon under the supervisor.
service_restart() {
  local svc="${1:-hive-sync}"
  case "$(_service_kind)" in
    launchd) launchd_restart "$svc" ;;
    runit)   runit_restart "$svc" ;;
    *)       systemctl --user restart "$svc" 2>/dev/null || true ;;
  esac
}

# service_is_active [svc] — 0 if the daemon is supervised + running, else 1.
service_is_active() {
  local svc="${1:-hive-sync}"
  case "$(_service_kind)" in
    launchd) launchd_is_active "$svc" ;;
    runit)   runit_is_active "$svc" ;;
    *)       systemctl --user is-active "$svc" --quiet 2>/dev/null ;;
  esac
}
