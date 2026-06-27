# `scripts/` layout

Organized by **where the code actually diverges**, not aspirationally.

```
scripts/
  common/      cross-platform: POSIX-sh hooks + all Python helpers
  installer/   shared install/update/uninstall flow + the _service.sh supervisor router
  platform/
    linux/     _units.sh      systemd --user units (also used on WSL2)
    macos/     _launchd.sh    launchd LaunchAgents
    termux/    _runit.sh      termux-services / runit (Android via Termux)
    windows/   remove-portproxy.bat
```

## Platform support

| Script | Lang | Linux | WSL2 | macOS | Termux (Android) | Windows | iOS |
|---|---|:--:|:--:|:--:|:--:|:--:|:--:|
| `common/*.py` (gen_verify, sign_release, gen_keypair, infer-phrases) | Python | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| `common/hive_dispatch.sh`, `*_hook.sh`, `smoke.sh`, `sync_smoke.sh`, `deploy_node.sh` | bash | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| `installer/` (install/update/uninstall, `_service.sh` router, `dispatcher.sh`) | bash | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| `platform/linux/_units.sh` | bash | ✅ | ✅ | — | — | — | — |
| `platform/macos/_launchd.sh` | bash | — | — | ✅ | — | — | — |
| `platform/termux/_runit.sh` | bash | — | — | — | ✅ | — | — |
| `platform/windows/remove-portproxy.bat` | batch | — | (host) | — | — | ✅ | — |

The `installer/_service.sh` seam picks the right `platform/<os>/` backend at runtime
(`_service_kind` → systemd / launchd / runit).

## Why no `ios/` (or native `android/`) directory

iOS cannot run shell scripts at all, and "Android" support today is **Termux** (POSIX bash, the
`termux/` backend) — not a native app. Native iOS / Android execution is a separate concern tracked
by the mobile task-runner feature; the matching `platform/ios/` (and a native-Android) directory will
land with that work rather than sit here empty.
