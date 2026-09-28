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
_HIVE_IDENTITY_FILES=(.device-key .device-id .owner-key)   # names INSIDE the stash (unchanged)

# _hive_key_path <HIVE_DIR> <device|owner|pub|dir> — where hv keeps that key (2.0 PR 3a, private #27: the
# seeds live in a key directory outside the working tree). The resolution rule lives in ONE place, hv;
# this asks it rather than keeping a second copy that could drift. Prints nothing when hv cannot be
# loaded (no checkout yet), and callers then fall back to the legacy root, which hv still reads and
# `doctor --fix` relocates. Always returns 0: callers assign `x="$(_hive_key_path …)"`, and under the
# installers' `set -e` a failing substitution would end the script before its fallback runs.
_hive_key_path() {
  local home="$1" which="$2"
  [ -n "$home" ] && [ -f "$home/hv" ] || return 0
  HIVE_HOME="$home" python3 - "$home" "$which" 2>/dev/null <<'PY'
import importlib.machinery, importlib.util, sys
home, which = sys.argv[1], sys.argv[2]
loader = importlib.machinery.SourceFileLoader("hv_keypath", home + "/hv")
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("hv_keypath", loader))
loader.exec_module(m)
print({"device": m.DEVICE_KEY_PATH, "owner": m.OWNER_KEY_PATH, "pub": m.OWNER_PUB_PATH, "dir": m.KEY_DIR}[which])
PY
  return 0
}

# keep_identity_save <HIVE_DIR> — copy this device's identity keys into the stash (0600).
# Returns 0 if a device key was saved, 1 if there was nothing to save.
keep_identity_save() {
  local src="$1" dk ok f
  [ -n "$src" ] || return 1
  dk="$(_hive_key_path "$src" device)"; [ -n "$dk" ] || dk="$src/.device-key"
  ok="$(_hive_key_path "$src" owner)"; [ -n "$ok" ] || ok="$src/.owner-key"
  [ -e "$dk" ] || return 1
  mkdir -p "$HIVE_IDENTITY_STASH" || return 1
  chmod 700 "$HIVE_IDENTITY_STASH" 2>/dev/null || true
  cp -a "$dk" "$HIVE_IDENTITY_STASH/.device-key" 2>/dev/null || return 1
  [ -e "$src/.device-id" ] && cp -a "$src/.device-id" "$HIVE_IDENTITY_STASH/.device-id" 2>/dev/null
  [ -e "$ok" ] && cp -a "$ok" "$HIVE_IDENTITY_STASH/.owner-key" 2>/dev/null
  for f in "${_HIVE_IDENTITY_FILES[@]}"; do
    [ -e "$HIVE_IDENTITY_STASH/$f" ] && chmod 600 "$HIVE_IDENTITY_STASH/$f" 2>/dev/null
  done
  return 0
}

# keep_identity_can_restore <HIVE_DIR> — true iff a stashed identity exists AND the install has
# no current device key (so restoring is meaningful, not an overwrite).
keep_identity_can_restore() {
  local dst="$1" dk
  [ -e "$HIVE_IDENTITY_STASH/.device-key" ] && [ -n "$dst" ] || return 1
  dk="$(_hive_key_path "$dst" device)"; [ -n "$dk" ] || dk="$dst/.device-key"
  [ ! -e "$dk" ] && [ ! -e "$dst/.device-key" ]
}

# keep_identity_stashed_id — print the stashed device_id (for prompts), or nothing.
keep_identity_stashed_id() {
  [ -e "$HIVE_IDENTITY_STASH/.device-id" ] && tr -d '[:space:]' < "$HIVE_IDENTITY_STASH/.device-id" 2>/dev/null
}

# keep_identity_restore <HIVE_DIR> — copy the stashed identity keys into HIVE_DIR. Returns 0 if a
# device key is in place afterwards.
keep_identity_restore() {
  local dst="$1" kd f
  [ -n "$dst" ] || return 1
  mkdir -p "$dst"
  kd="$(_hive_key_path "$dst" dir)"
  if [ -n "$kd" ]; then
    # The keys go into the key directory, outside the working tree, and the checkout records where.
    mkdir -p "$kd" && chmod 700 "$kd" 2>/dev/null
    [ -e "$HIVE_IDENTITY_STASH/.device-key" ] && cp -a "$HIVE_IDENTITY_STASH/.device-key" "$kd/device-key" 2>/dev/null
    [ -e "$HIVE_IDENTITY_STASH/.owner-key" ] && cp -a "$HIVE_IDENTITY_STASH/.owner-key" "$kd/owner-key" 2>/dev/null
    for f in device-key owner-key; do [ -e "$kd/$f" ] && chmod 600 "$kd/$f" 2>/dev/null; done
    printf '%s\n' "$kd" > "$dst/.key-dir"
    [ -e "$HIVE_IDENTITY_STASH/.device-id" ] && cp -a "$HIVE_IDENTITY_STASH/.device-id" "$dst/.device-id" 2>/dev/null
    [ -e "$kd/device-key" ]
    return
  fi
  # No hv here to ask: the legacy root, which hv still reads and `hv doctor --fix` relocates.
  for f in "${_HIVE_IDENTITY_FILES[@]}"; do
    [ -e "$HIVE_IDENTITY_STASH/$f" ] && cp -a "$HIVE_IDENTITY_STASH/$f" "$dst/" 2>/dev/null || true
    [ -e "$dst/$f" ] && chmod 600 "$dst/$f" 2>/dev/null || true
  done
  [ -e "$dst/.device-key" ]
}
