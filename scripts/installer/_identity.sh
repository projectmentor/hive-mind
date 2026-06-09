#!/usr/bin/env bash
# =============================================================================
# _identity.sh — reusable device-identity preservation primitive.
#
# Identity = this device's Ed25519 device key (.device-key → device_id) and, if this device is
# the owner, its owner key (.owner-key). This primitive stashes ONLY those keys to a STABLE
# per-user location that survives `hive-mind uninstall` and a repo wipe — so a reinstall can
# resume the SAME device_id. That matters because admission is an owner-signed `admit <device_id>`
# entry in the synced journal: restore the same device_id and the prior admission applies again
# with no re-admit (closes the reinstall-orphans-identity churn — facts #203/#205).
#
# It deliberately holds NO journal/data — that's `--keep-hive`'s full timestamped backup. Sourced
# by both _uninstall.sh (save) and _install_node.sh (restore); composed by --keep-hive.
# =============================================================================

# Stable, survives uninstall; overridable for tests / non-standard installs.
HIVE_IDENTITY_STASH="${HIVE_IDENTITY_STASH:-$HOME/.config/hive-mind/identity}"
_HIVE_IDENTITY_FILES=(.device-key .device-id .owner-key)

# keep_identity_save <HIVE_DIR> — copy this device's identity keys into the stash (0600).
# Returns 0 if a device key was saved, 1 if there was nothing to save.
keep_identity_save() {
  local src="$1" f saved=""
  [ -n "$src" ] && [ -e "$src/.device-key" ] || return 1
  mkdir -p "$HIVE_IDENTITY_STASH" || return 1
  chmod 700 "$HIVE_IDENTITY_STASH" 2>/dev/null || true
  for f in "${_HIVE_IDENTITY_FILES[@]}"; do
    if [ -e "$src/$f" ]; then
      cp -a "$src/$f" "$HIVE_IDENTITY_STASH/" 2>/dev/null && saved=1
      chmod 600 "$HIVE_IDENTITY_STASH/$f" 2>/dev/null || true
    fi
  done
  [ -n "$saved" ]
}

# keep_identity_can_restore <HIVE_DIR> — true iff a stashed identity exists AND the install has
# no current device key (so restoring is meaningful, not an overwrite).
keep_identity_can_restore() {
  local dst="$1"
  [ -e "$HIVE_IDENTITY_STASH/.device-key" ] && [ -n "$dst" ] && [ ! -e "$dst/.device-key" ]
}

# keep_identity_stashed_id — print the stashed device_id (for prompts), or nothing.
keep_identity_stashed_id() {
  [ -e "$HIVE_IDENTITY_STASH/.device-id" ] && tr -d '[:space:]' < "$HIVE_IDENTITY_STASH/.device-id" 2>/dev/null
}

# keep_identity_restore <HIVE_DIR> — copy the stashed identity keys into HIVE_DIR. Returns 0 if a
# device key is in place afterwards.
keep_identity_restore() {
  local dst="$1" f
  [ -n "$dst" ] || return 1
  mkdir -p "$dst"
  for f in "${_HIVE_IDENTITY_FILES[@]}"; do
    [ -e "$HIVE_IDENTITY_STASH/$f" ] && cp -a "$HIVE_IDENTITY_STASH/$f" "$dst/" 2>/dev/null || true
    [ -e "$dst/$f" ] && chmod 600 "$dst/$f" 2>/dev/null || true
  done
  [ -e "$dst/.device-key" ]
}
