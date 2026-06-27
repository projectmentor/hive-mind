#!/usr/bin/env bash
# =============================================================================
# _runit.sh — Android/Termux (termux-services / runit) backend for the supervisor.
#
# The Termux analogue of _units.sh (systemd) and _launchd.sh (launchd). Sourced
# by _service.sh only on Android. Termux has no systemd, launchd, cron, sudo, or
# root — its standard supervisor is termux-services, which runs `runsvdir` over
# $PREFIX/var/service. Each service is a directory with an executable `run`
# script; `sv up|down|restart|status <name>` controls it; `sv-enable`/`sv-disable`
# toggle auto-start. runit has NO timers, so the periodic self-heal is a `run`
# script that loops `hv doctor --fix; sleep 900` (≙ hive-doctor.timer 15-min).
#
#   hive-sync    runsvdir respawn-on-exit  ≙ systemd Restart=always / launchd KeepAlive
#   hive-doctor  while-loop every 900s      ≙ hive-doctor.timer OnUnitActiveSec=15min
#
# A phone behind Tailscale's userspace VPN cannot accept inbound :9876, but it
# never needs to: sync_daemon.py's run_daemon() does an OUTBOUND sync_now() every
# 300s, which both pulls and pushes — so the supervised daemon converges the node
# on its own. A Termux:Boot entrypoint (~/.termux/boot/) restarts it on reboot.
#
# All paths are overridable for tests / CI (see _runit_render + --render-test):
# the render path needs no device, no `sv`, and runs on an ordinary Linux runner.
# =============================================================================

# Termux's $PREFIX is /data/data/com.termux/files/usr; fall back to it for CI
# (where PREFIX is unset) so the rendered paths still resemble a real device.
_RUNIT_PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
_RUNIT_SERVICE_DIR="${_RUNIT_SERVICE_DIR:-$_RUNIT_PREFIX/var/service}"
_RUNIT_LOG_DIR="${_RUNIT_LOG_DIR:-$HOME/.hive-mind/logs}"
_RUNIT_BOOT_DIR="${_RUNIT_BOOT_DIR:-$HOME/.termux/boot}"

# Best interpreter for the run scripts: PATH first, then the Termux prefix.
_runit_python3() {
  if command -v python3 >/dev/null 2>&1; then command -v python3; return; fi
  echo "$_RUNIT_PREFIX/bin/python3"
}

# A run script needs a real shebang. Use the Termux sh if present, else /bin/sh
# (CI / render-test on an ordinary Linux runner has no Termux prefix).
_runit_sh() {
  [ -x "$_RUNIT_PREFIX/bin/sh" ] && { echo "$_RUNIT_PREFIX/bin/sh"; return; }
  echo /bin/sh
}

# _runit_render <hive_dir> — write the two service `run` scripts + the Termux:Boot
# entrypoint, no `sv`. Split out (like _launchd_render) so CI can render + lint
# without termux-services or a phone.
_runit_render() {
  local hive_dir="$1" py sh svc_run doctor_run boot
  if [ -z "$hive_dir" ] || [ ! -f "$hive_dir/sync_daemon.py" ]; then
    echo "_runit_render: invalid hive_dir '$hive_dir' (no sync_daemon.py)" >&2
    return 1
  fi
  py="$(_runit_python3)"; sh="$(_runit_sh)"
  mkdir -p "$_RUNIT_SERVICE_DIR/hive-sync" "$_RUNIT_SERVICE_DIR/hive-doctor" \
           "$_RUNIT_LOG_DIR" "$_RUNIT_BOOT_DIR"

  # ── the sync daemon: runsvdir respawns it on exit (≙ Restart=always) ──
  svc_run="$_RUNIT_SERVICE_DIR/hive-sync/run"
  cat > "$svc_run" << RUN
#!$sh
# hive-mind sync daemon (runit-supervised; runsvdir respawns on exit).
# run_daemon() serves :9876 AND syncs outbound every 300s — converges this node.
export HIVE_HOME="$hive_dir"
cd "$hive_dir" || exit 1
exec "$py" "$hive_dir/sync_daemon.py" >> "$_RUNIT_LOG_DIR/hive-sync.log" 2>&1
RUN

  # ── self-heal: `hv doctor --fix` on a 900s loop (runit has no timers) ──
  doctor_run="$_RUNIT_SERVICE_DIR/hive-doctor/run"
  cat > "$doctor_run" << RUN
#!$sh
# hive-mind self-heal — runit has no timers, so loop hv doctor --fix every 15 min.
export HIVE_HOME="$hive_dir"
cd "$hive_dir" || exit 1
while :; do
  "$py" "$hive_dir/hv" doctor --fix >> "$_RUNIT_LOG_DIR/hive-doctor.log" 2>&1
  sleep 900
done
RUN

  # ── Termux:Boot entrypoint: start on phone reboot (app must be installed) ──
  boot="$_RUNIT_BOOT_DIR/start-hive-mind"
  cat > "$boot" << BOOT
#!$sh
# Termux:Boot entrypoint — start hive-mind on phone reboot. Keep this executable.
# Requires the Termux:Boot app (F-Droid), opened once, with battery optimisation
# disabled for Termux + Termux:Boot. A wakelock keeps Android Doze from freezing
# the daemon.
termux-wake-lock 2>/dev/null || true
# Start the runit supervisor tree if termux-services' login hook hasn't already.
if [ -x "$_RUNIT_PREFIX/etc/profile.d/start-services.sh" ]; then
  . "$_RUNIT_PREFIX/etc/profile.d/start-services.sh"
elif ! pgrep -x runsvdir >/dev/null 2>&1; then
  runsvdir "$_RUNIT_SERVICE_DIR" &
fi
sleep 2
sv up hive-sync   2>/dev/null || true
sv up hive-doctor 2>/dev/null || true
BOOT

  chmod +x "$svc_run" "$doctor_run" "$boot"
}

# runit_install <hive_dir> [svc] — render the scripts then enable + start both
# services. The svc arg is accepted for signature parity with write_systemd_units
# / launchd_install; the service names are fixed (hive-sync, hive-doctor).
runit_install() {
  local hive_dir="$1" label
  _runit_render "$hive_dir" || return 1
  # Lint the generated run scripts before handing them to runsvdir.
  for label in hive-sync hive-doctor; do
    sh -n "$_RUNIT_SERVICE_DIR/$label/run" 2>/dev/null \
      || { echo "runit_install: malformed run script for $label" >&2; return 1; }
  done
  if command -v sv-enable >/dev/null 2>&1; then
    sv-enable hive-sync   2>/dev/null || true
    sv-enable hive-doctor 2>/dev/null || true
  fi
  # `sv up` is idempotent; runsvdir picks up new service dirs within ~5s.
  sv up hive-sync   2>/dev/null || true
  sv up hive-doctor 2>/dev/null || true
}

# runit_restart [svc] — restart the named service (default hive-sync).
runit_restart() {
  sv restart "${1:-hive-sync}" 2>/dev/null || true
}

# runit_is_active [svc] — 0 if runsvdir reports the service running, else 1.
runit_is_active() {
  sv status "${1:-hive-sync}" 2>/dev/null | grep -q '^run:'
}

# runit_uninstall [svc] — stop, disable, and remove both services + the boot
# script (idempotent; svc arg ignored — labels are fixed).
runit_uninstall() {
  local label
  for label in hive-sync hive-doctor; do
    sv down "$label"     2>/dev/null || true
    sv-disable "$label"  2>/dev/null || true
    rm -rf "$_RUNIT_SERVICE_DIR/$label"
  done
  rm -f "$_RUNIT_BOOT_DIR/start-hive-mind"
}

# `bash _runit.sh --render-test` — render the scripts to a temp tree and lint them.
# Used by CI on an ordinary Linux runner (no termux-services, no phone): validates
# the templates without `sv`. Only fires when executed directly, never when sourced.
if [ "${BASH_SOURCE[0]:-x}" = "${0:-y}" ] && [ "${1:-}" = "--render-test" ]; then
  set -e
  _tmp="$(mktemp -d)"
  _RUNIT_SERVICE_DIR="$_tmp/service"
  _RUNIT_LOG_DIR="$_tmp/logs"
  _RUNIT_BOOT_DIR="$_tmp/boot"
  _repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  _runit_render "$_repo"
  for _f in "$_RUNIT_SERVICE_DIR/hive-sync/run" "$_RUNIT_SERVICE_DIR/hive-doctor/run" \
            "$_RUNIT_BOOT_DIR/start-hive-mind"; do
    echo "lint: $_f"
    [ -x "$_f" ] || { echo "render-test FAIL: $_f not executable" >&2; exit 1; }
    sh -n "$_f"  || { echo "render-test FAIL: $_f has a syntax error" >&2; exit 1; }
  done
  grep -q 'exec .*sync_daemon.py'  "$_RUNIT_SERVICE_DIR/hive-sync/run"   || { echo "render-test FAIL: hive-sync missing daemon exec" >&2; exit 1; }
  grep -q 'doctor --fix'           "$_RUNIT_SERVICE_DIR/hive-doctor/run" || { echo "render-test FAIL: hive-doctor missing doctor --fix" >&2; exit 1; }
  grep -q 'sv up hive-sync'        "$_RUNIT_BOOT_DIR/start-hive-mind"    || { echo "render-test FAIL: boot script missing sv up" >&2; exit 1; }
  echo "render-test OK"
fi
