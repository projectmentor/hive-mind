"""
Hive Mind sync client (Phase 2, P2P_DESIGN.md §3 sync flow).

Runs a bidirectional round with each configured peer:
  1. Compare global Merkle roots — if equal, done (0 bytes).
  2. Otherwise GET /sync/hello for the peer's per-node chunk-hash vectors and
     seq maxima, and diff them against ours to localize the differing 100-seq
     windows (a flat one-level compare — a full recursive tree walk is needless
     at this scale; the root is the shortcut, the chunk vector the localizer).
  3. PULL the windows we lack/differ on, append (G-Set dedup) + rebuild.
  4. PUSH the windows the peer lacks/differs on to its /sync/ingest.
"""

import requests

import merkle
import sync_common

hv = sync_common.load_hv()
SIZE = merkle.CHUNK_SIZE


def _local_entries():
    return merkle.read_all_entries(hv.JOURNAL_DIR)


def _get(base, path, **params):
    r = requests.get(f"{base}{path}", params=params or None, timeout=15)
    r.raise_for_status()
    return r.json()


def _post(base, path, payload):
    r = requests.post(f"{base}{path}", json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


def _differing_windows(local_chunks, remote_chunks):
    """Yield (node, start, end) seq windows present-on-remote-but-differing
    (or absent) locally — i.e. windows to PULL. Swap args to get PUSH windows."""
    for node, rhashes in remote_chunks.items():
        lhashes = local_chunks.get(node, [])
        for ci, rh in enumerate(rhashes):
            if ci >= len(lhashes) or lhashes[ci] != rh:
                yield node, ci * SIZE + 1, (ci + 1) * SIZE


def _sync_with_peer(peer):
    base = peer["url"].rstrip("/")
    pid = peer.get("id", base)

    local = _local_entries()
    local_root = merkle.merkle_root(merkle.chunk_hashes(local))
    if _get(base, "/sync/merkle-root")["root_hash"] == local_root:
        print(f"  {pid}: in sync")
        return

    hello = _get(base, "/sync/hello")
    # Hive scoping: if both sides have a hive_id and they differ, this is a different hive —
    # never merge its journal into ours. (Empty on either side = pre-owner, allowed so the genesis
    # owner declaration can propagate during bootstrap.)
    local_hive, peer_hive = hv._local_hive_id(), hello.get("hive_id", "")
    if local_hive and peer_hive and local_hive != peer_hive:
        print(f"  {pid}: different hive ({peer_hive} vs {local_hive}) — not syncing")
        return
    remote_chunks = hello.get("chunks", {})

    # PULL differing/missing windows from the peer.
    pulled = []
    for node, start, end in _differing_windows(merkle.node_chunk_hashes(local), remote_chunks):
        data = _get(base, "/sync/chunk", node=node, start=start, end=end)
        pulled.extend(data.get("entries", []))

    accepted = duplicates = 0
    if pulled:
        accepted, duplicates = hv.append_foreign_entries(pulled)
        if accepted:
            hv.rebuild_db()

    # PUSH windows the peer lacks/differs on (recompute local after the pull).
    local = _local_entries()
    push = []
    for node, start, end in _differing_windows(remote_chunks, merkle.node_chunk_hashes(local)):
        push.extend(merkle.entries_in_range(local, node, start, end))

    pushed = 0
    if push:
        pushed = _post(base, "/sync/ingest", {"entries": push, "hive_id": local_hive}).get("accepted", 0)

    print(f"  {pid}: pulled {accepted} (dup {duplicates}), pushed {pushed}")


def sync_now():
    hv.init_db()
    cfg = sync_common.load_peers()
    peers = cfg.get("peers", [])
    if not peers:
        print("No peers configured (.peers.json). Nothing to sync.")
        return
    print(f"sync now: {hv.NODE_ID} -> {len(peers)} peer(s)")
    for peer in peers:
        try:
            _sync_with_peer(peer)
        except requests.RequestException as e:
            print(f"  {peer.get('id', peer.get('url'))}: offline/unreachable ({e})")
        except Exception as e:
            print(f"  {peer.get('id', peer.get('url'))}: error {e}")


if __name__ == "__main__":
    sync_now()
