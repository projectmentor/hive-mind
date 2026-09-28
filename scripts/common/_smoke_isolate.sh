# _smoke_isolate.sh — sourced by smoke.sh, sync_smoke.sh and sync_auth_smoke.sh (#170).
#
# A smoke run by hand used to write into the caller's account. `owner init` stashes the owner key through
# $HIVE_IDENTITY_STASH, whose default is the real ~/.config/hive-mind/identity: the one place a reinstall
# restores an identity from. And since 2.0 PR 3a every hive's key directory defaults to ~/.hive/keys/<id>,
# so each run left key directories in the real home. Under pytest the `isolation` fixture hid both; by hand
# nothing did. The script now owns its isolation, as it owns its ports (#161), instead of trusting the
# caller's environment.

# smoke_isolate <dir> — point HOME, the identity stash and the Claude config dir under <dir>, and clear
# HIVE_KEY_DIR, which would otherwise put every hive this run makes into one real key directory. Set
# unconditionally: a caller's exported value is exactly what this must not write to. The user's
# site-packages stay visible to python3, as the test suite keeps them (tests/conftest.py).
smoke_isolate() {
  PYTHONUSERBASE="${PYTHONUSERBASE:-$(python3 -m site --user-base)}"
  export PYTHONUSERBASE
  export HOME="$1/home"
  export HIVE_IDENTITY_STASH="$1/home/.config/hive-mind/identity"
  export CLAUDE_CONFIG_DIR="$1/home/.claude"
  unset HIVE_KEY_DIR
  mkdir -p "$HOME"
}
