#!/usr/bin/env bash
# Bring a Hive Mind node up to date and ready for P2P sync (Phase 2 acceptance).
#
# Safe to run on any node, repeatedly: git pull, run the (idempotent) v2 journal
# migration, rebuild store.db, run the offline test suite, and print this node's
# identity + Merkle root. Does NOT touch .peers.json (node-specific, gitignored)
# or start the daemon — those stay explicit.
#
# Usage:  ./scripts/deploy_node.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== git pull =="
git pull --ff-only

echo "== journal v2 migration (idempotent; backs up journal/ if it changes anything) =="
python3 utilities/migrate_journal.py

echo "== rebuild store.db from the journal =="
./hv rebuild

echo "== offline test suite =="
python3 -m pytest -q

echo "== node identity =="
echo "  node_id : ${HIVE_NODE_ID:-$(python3 -c 'import socket;print(socket.gethostname())')}"
echo -n "  merkle  : "; ./hv doctor merkle | awk '/^Root:/{print $2}'

if [ -f .peers.json ]; then
  echo "== .peers.json present =="
else
  echo "== NEXT: create .peers.json (see config/.peers.json.example) — this node's peer is the OTHER box's Tailscale URL =="
fi
echo "== ready. Start the daemon with: ./hv sync daemon =="
