#!/usr/bin/env bash
# Install the Hive Mind sync daemon as a systemd service so it survives WSL
# restarts (TODO from corpus fact #19). Run inside WSL; uses sudo.
#
# Idempotent. Generates /etc/systemd/system/hive-sync.service from the real
# user/home/repo paths, stops any manually-launched daemon, then enables+starts.
#
# NOTE: a systemd service covers "WSL restarted". To also survive a *Windows
# reboot*, WSL itself must auto-start at login — that's handled on the Windows
# host by 01-windows-setup.bat (Task Scheduler).
set -euo pipefail

HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
SVC_USER="$(id -un)"
PY="$(command -v python3)"
UNIT=/etc/systemd/system/hive-sync.service

[ -x "$HIVE_DIR/hv" ] || { echo "ERROR: $HIVE_DIR/hv not found/executable"; exit 1; }

echo "== stopping any manually-launched daemon (frees :9876) =="
pkill -f 'sync_daemon.py' 2>/dev/null || true
pkill -f 'hv sync daemon' 2>/dev/null || true

echo "== writing $UNIT =="
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Hive Mind P2P sync daemon (serve + periodic outbound sync)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$HIVE_DIR
Environment=HIVE_HOME=$HIVE_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY $HIVE_DIR/hv sync daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "== enable + (re)start =="
sudo systemctl daemon-reload
sudo systemctl enable hive-sync.service
sudo systemctl restart hive-sync.service

echo "== verify =="
sleep 2
systemctl is-active hive-sync.service
sudo systemctl --no-pager --lines=5 status hive-sync.service || true
echo -n "/sync/hello -> "; curl -s --max-time 3 http://127.0.0.1:9876/sync/hello || echo "(no response)"
echo
echo "== done. Logs: journalctl -u hive-sync.service -f =="
