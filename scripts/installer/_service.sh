#!/usr/bin/env bash
# =============================================================================
# _service.sh — cross-platform supervisor seam for the hive-sync daemon.
#
# The supervisor that owns the daemon + the periodic self-heal differs by OS:
#   Linux / WSL : systemd --user units  (see _units.sh — the single source of
#                 truth, sourced and called VERBATIM here, so the Linux path is
#                 byte-identical and carries zero regression risk).
#   macOS       : launchd LaunchAgents  (see _launchd.sh).
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

# launchd: the supervisor key. `systemd` covers native Linux + WSL (where the
# concrete init may be systemctl/systemd/initd/cron — those sub-variants are
# handled by the installer, not here).
_service_kind() {
  case "$(uname -s)" in
    Darwin) echo launchd ;;
    *)      echo systemd ;;
  esac
}

# Pull in the launchd backend only on macOS (defines launchd_* + _resolve_tailscale).
if [ "$(_service_kind)" = launchd ] && [ -f "$_SERVICE_LIB_DIR/_launchd.sh" ]; then
  . "$_SERVICE_LIB_DIR/_launchd.sh"
fi

# service_install <hive_dir> [svc] — install + enable the supervisor units.
service_install() {
  case "$(_service_kind)" in
    launchd) launchd_install "$@" ;;
    *)       write_systemd_units "$@" ;;
  esac
}

# service_restart [svc] — (re)start the sync daemon under the supervisor.
service_restart() {
  local svc="${1:-hive-sync}"
  case "$(_service_kind)" in
    launchd) launchd_restart "$svc" ;;
    *)       systemctl --user restart "$svc" 2>/dev/null || true ;;
  esac
}

# service_is_active [svc] — 0 if the daemon is supervised + running, else 1.
service_is_active() {
  local svc="${1:-hive-sync}"
  case "$(_service_kind)" in
    launchd) launchd_is_active "$svc" ;;
    *)       systemctl --user is-active "$svc" --quiet 2>/dev/null ;;
  esac
}
