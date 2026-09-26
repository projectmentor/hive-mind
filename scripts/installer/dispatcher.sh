#!/usr/bin/env bash
# =============================================================================
# hive-mind command dispatcher  —  scripts/installer/dispatcher.sh
#
# This IS the `hive-mind` command. The installer symlinks ~/.local/bin/hive-mind
# to this file (like it does for `hv`), so a plain `git pull` keeps the command
# current — new subcommands show up with no reinstall. It resolves its own repo
# location, so it works regardless of where the repo lives.
# =============================================================================
# Resolve this script's real path, following symlinks (the installer symlinks
# ~/.local/bin/hive-mind here). BSD `readlink` on macOS lacks `-f`, so resolve
# the symlink chain portably instead of relying on the GNU extension.
_resolve_self() {
  local p="${BASH_SOURCE[0]}" t
  while [ -L "$p" ]; do
    t="$(readlink "$p")"
    case "$t" in
      /*) p="$t" ;;
      *)  p="$(cd "$(dirname "$p")" && pwd)/$t" ;;
    esac
  done
  printf '%s\n' "$(cd "$(dirname "$p")" && pwd -P)/$(basename "$p")"
}
SELF="$(_resolve_self)"
HIVE_DIR="${HIVE_DIR:-$(cd "$(dirname "$SELF")/../.." && pwd)}"
INSTALLER="$HIVE_DIR/scripts/installer"
CMD="${1:-help}"
shift 2>/dev/null || true

case "$CMD" in
  install)   exec bash "$INSTALLER/_install_node.sh" "$@" ;;
  update)    exec bash "$INSTALLER/_update.sh"       "$@" ;;
  reset)     exec bash "$INSTALLER/_reset.sh"        "$@" ;;
  status)    exec bash "$INSTALLER/_status.sh"       "$@" ;;
  invite)    exec bash "$INSTALLER/_invite.sh"       "$@" ;;
  uninstall) exec bash "$INSTALLER/_uninstall.sh"    "$@" ;;
  # 2.0 (#136): the owner/operator control plane. `hv` is the agent data plane and cannot owner-sign;
  # these verbs are where the owner key is loaded and governance is signed. This dispatcher stays the
  # `hive-mind` command — it is already in the signed manifest by its .sh suffix and already symlinked
  # into PATH — and simply execs the module. It never reads the owner key, never passes a seed on a
  # command line, and never puts one in the environment.
  owner|group|admit|config|unforget|retract)
    exec python3 "$HIVE_DIR/hivemind_ctl.py" "$CMD" "$@" ;;
  *)
    echo "Usage: hive-mind <subcommand>"
    echo ""
    echo "Subcommands:"
    echo "  install      Set up this device from scratch"
    echo "  update       Pull latest + restart daemon (auto-heals after a force-push/rewrite)"
    echo "  reset        Recover a wedged install: force-align code + rebuild + restart + verify (keeps your Hive data)"
    echo "  status       Show device health and peer sync state"
    echo "  invite       Print the address to paste on a new device to join this hive"
    echo "  uninstall    Remove HiveMind from this device (--keep-hive to keep your data)"
    echo ""
    echo "Owner / operator commands (2.0 — these moved off \`hv\`, which cannot owner-sign):"
    echo "  owner …      Owner identity and governance (init, mint, export, pin, transfer, …)"
    echo "  group …      Membership (admit, revoke, deny, change, purge)"
    echo "  config set   Governed parameters"
    echo "  unforget     Reverse an owner forget"
    echo "  retract --owner   An owner forget (plain \`hv retract\` is peer evidence)"
    echo ""
    ;;
esac
