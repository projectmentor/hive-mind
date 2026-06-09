#!/usr/bin/env bash
# =============================================================================
# hive-mind command dispatcher  —  scripts/installer/dispatcher.sh
#
# This IS the `hive-mind` command. The installer symlinks ~/.local/bin/hive-mind
# to this file (like it does for `hv`), so a plain `git pull` keeps the command
# current — new subcommands show up with no reinstall. It resolves its own repo
# location, so it works regardless of where the repo lives.
# =============================================================================
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
HIVE_DIR="${HIVE_DIR:-$(cd "$(dirname "$SELF")/../.." && pwd)}"
INSTALLER="$HIVE_DIR/scripts/installer"
CMD="${1:-help}"
shift 2>/dev/null || true

case "$CMD" in
  install)   exec bash "$INSTALLER/_install_node.sh" "$@" ;;
  update)    exec bash "$INSTALLER/_update.sh"       "$@" ;;
  status)    exec bash "$INSTALLER/_status.sh"       "$@" ;;
  uninstall) exec bash "$INSTALLER/_uninstall.sh"    "$@" ;;
  *)
    echo "Usage: hive-mind <subcommand>"
    echo ""
    echo "Subcommands:"
    echo "  install      Set up this device from scratch"
    echo "  update       Pull latest + restart daemon"
    echo "  status       Show device health and peer sync state"
    echo "  uninstall    Remove HiveMind from this device (--keep-hive to keep your data)"
    echo ""
    ;;
esac
