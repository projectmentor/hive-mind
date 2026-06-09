#!/usr/bin/env bash
# =============================================================================
# hive-mind cleanup  —  scripts/installer/cleanup.sh
#
# Removes the Win11 OpenSSH bridge setup from a device.
# Run from WSL on each node (has access to /mnt/c and powershell.exe).
#
# What it removes:
#   Windows side (via powershell.exe / cmd.exe):
#     - Stops and disables sshd service
#     - Removes OpenSSH Server Windows capability
#     - Removes C:\ssh-wsl.bat
#     - Removes C:\ProgramData\ssh\administrators_authorized_keys (if empty/ours)
#     - Removes netsh portproxy rule for port 9876
#     - Removes firewall rule "HiveMind Sync 9876"
#     - Removes scheduled task "HiveMind WSL Autostart"
#     - Cleans C:\Users\Public\  (hivemind-*.html, *.pub, my-wsl-key.pub)
#     - Removes DefaultShell registry key for OpenSSH
#
#   WSL side:
#     - Removes ~/projects/hermes/.ssh/  (stale authorized_keys nobody reads)
#     - Removes ~/projects/hive-mind/scripts/install/  (old two-file setup; replaced)
#
# Safe to run multiple times — every step is idempotent.
# =============================================================================

set -uo pipefail   # no -e so we continue past individual failures

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}[ok]${RST}  $*"; }
info() { echo -e "${BLD}[..]${RST}  $*"; }
warn() { echo -e "${YLW}[!!]${RST}  $*"; }
skip() { echo -e "      ${RST}(skip) $*"; }

echo ""
echo -e "${BLD}hive-mind cleanup — Win11 OpenSSH bridge teardown${RST}"
echo "────────────────────────────────────────────────────"
echo ""

# ── guard ─────────────────────────────────────────────────────────────────
grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null \
  || { echo "Must run inside WSL."; exit 1; }

# ════════════════════════════════════════════════════════════════════════════
# WINDOWS SIDE
# ════════════════════════════════════════════════════════════════════════════
echo -e "${BLD}Windows cleanup${RST}"

# ── stop + disable sshd ───────────────────────────────────────────────────
info "Stopping sshd service..."
powershell.exe -NoProfile -Command "
  \$svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
  if (\$svc) {
    Stop-Service sshd -Force -ErrorAction SilentlyContinue
    Set-Service  sshd -StartupType Disabled -ErrorAction SilentlyContinue
    Write-Host 'stopped and disabled'
  } else {
    Write-Host 'not installed'
  }
" 2>/dev/null | tr -d '\r' | while read -r l; do
  [[ "$l" =~ stopped ]] && ok "sshd stopped and disabled" || skip "sshd: $l"
done

# ── remove OpenSSH Server capability ─────────────────────────────────────
info "Removing OpenSSH.Server capability..."
RESULT=$(powershell.exe -NoProfile -Command "
  \$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction SilentlyContinue
  if (\$cap -and \$cap.State -eq 'Installed') {
    Remove-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction SilentlyContinue | Out-Null
    Write-Host 'removed'
  } else {
    Write-Host 'not_installed'
  }
" 2>/dev/null | tr -d '\r')
[[ "$RESULT" == "removed" ]] && ok "OpenSSH Server capability removed" || skip "OpenSSH Server: $RESULT"

# ── remove DefaultShell registry key ─────────────────────────────────────
info "Removing DefaultShell registry key..."
powershell.exe -NoProfile -Command "
  Remove-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name 'DefaultShell' -ErrorAction SilentlyContinue
" 2>/dev/null
ok "DefaultShell registry key removed (or was absent)"

# ── remove C:\ssh-wsl.bat ────────────────────────────────────────────────
info "Removing C:\\ssh-wsl.bat..."
if [ -f /mnt/c/ssh-wsl.bat ]; then
  rm -f /mnt/c/ssh-wsl.bat && ok "C:\\ssh-wsl.bat removed" || warn "Could not remove C:\\ssh-wsl.bat"
else
  skip "C:\\ssh-wsl.bat not present"
fi

# ── remove administrators_authorized_keys ────────────────────────────────
info "Removing C:\\ProgramData\\ssh\\administrators_authorized_keys..."
ADMIN_KEYS="/mnt/c/ProgramData/ssh/administrators_authorized_keys"
if [ -f "$ADMIN_KEYS" ]; then
  # Only remove if it only contains our key (or is empty)
  LINES=$(wc -l < "$ADMIN_KEYS" 2>/dev/null || echo 0)
  if [ "$LINES" -le 1 ]; then
    powershell.exe -NoProfile -Command "
      Remove-Item 'C:\ProgramData\ssh\administrators_authorized_keys' -Force -ErrorAction SilentlyContinue
    " 2>/dev/null
    ok "administrators_authorized_keys removed"
  else
    warn "administrators_authorized_keys has $LINES lines — not removing automatically"
    warn "  Review: /mnt/c/ProgramData/ssh/administrators_authorized_keys"
  fi
else
  skip "administrators_authorized_keys not present"
fi

# ── remove portproxy rule for 9876 ────────────────────────────────────────
info "Removing portproxy rule for port 9876..."
powershell.exe -NoProfile -Command "
  netsh interface portproxy delete v4tov4 listenport=9876 listenaddress=0.0.0.0 2>&1 | Out-Null
  Write-Host 'done'
" 2>/dev/null | tr -d '\r' | grep -q done && ok "portproxy rule removed" || skip "portproxy: already absent"

# ── remove firewall rule ──────────────────────────────────────────────────
info "Removing firewall rule 'HiveMind Sync 9876'..."
powershell.exe -NoProfile -Command "
  netsh advfirewall firewall delete rule name='HiveMind Sync 9876' 2>&1 | Out-Null
  Write-Host 'done'
" 2>/dev/null | tr -d '\r' | grep -q done && ok "Firewall rule removed" || skip "Firewall rule: already absent"

# ── remove scheduled task ─────────────────────────────────────────────────
info "Removing 'HiveMind WSL Autostart' scheduled task..."
powershell.exe -NoProfile -Command "
  \$t = Get-ScheduledTask -TaskName 'HiveMind WSL Autostart' -ErrorAction SilentlyContinue
  if (\$t) {
    Unregister-ScheduledTask -TaskName 'HiveMind WSL Autostart' -Confirm:\$false
    Write-Host 'removed'
  } else {
    Write-Host 'not_found'
  }
" 2>/dev/null | tr -d '\r' | while read -r l; do
  [[ "$l" == "removed" ]] && ok "Scheduled task removed" || skip "Scheduled task: $l"
done

# ── clean C:\Users\Public\ junk ───────────────────────────────────────────
info "Cleaning C:\\Users\\Public junk files..."
WIN_USER=$(powershell.exe -NoProfile -Command "[System.Environment]::UserName" 2>/dev/null | tr -d '\r')
JUNK_FILES=(
  "/mnt/c/Users/Public/hivemind-commercial.html"
  "/mnt/c/Users/Public/hivemind-dev.html"
  "/mnt/c/Users/Public/my-wsl-key.pub"
)
REMOVED=0
for f in "${JUNK_FILES[@]}"; do
  if [ -f "$f" ]; then
    rm -f "$f" && { ok "Removed $(basename "$f")"; ((REMOVED++)); } || warn "Could not remove $f"
  fi
done
[ "$REMOVED" -eq 0 ] && skip "No junk files in C:\\Users\\Public\\"

# ════════════════════════════════════════════════════════════════════════════
# WSL SIDE
# ════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BLD}WSL cleanup${RST}"

# ── remove ~/projects/hermes/.ssh/ ───────────────────────────────────────
info "Removing ~/projects/hermes/.ssh/ ..."
if [ -d "$HOME/projects/hermes/.ssh" ]; then
  rm -rf "$HOME/projects/hermes/.ssh" && ok "~/projects/hermes/.ssh/ removed" \
    || warn "Could not remove ~/projects/hermes/.ssh/"
else
  skip "~/projects/hermes/.ssh/ not present"
fi

# ── remove old two-file install scripts ──────────────────────────────────
info "Removing old scripts/install/ (replaced by scripts/installer/)..."
OLD_DIR="$HOME/projects/hive-mind/scripts/install"
if [ -d "$OLD_DIR" ]; then
  rm -rf "$OLD_DIR" && ok "Old scripts/install/ removed" \
    || warn "Could not remove $OLD_DIR"
else
  skip "Old scripts/install/ not present"
fi

# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${GRN}${BLD}Cleanup complete.${RST}"
echo ""
echo "  Verify portproxy is clear:"
echo "    powershell.exe -NoProfile -Command \"netsh interface portproxy show all\""
echo ""
echo "  Verify sshd is gone:"
echo "    powershell.exe -NoProfile -Command \"Get-Service sshd -ErrorAction SilentlyContinue\""
echo ""
