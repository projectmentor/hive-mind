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

import os
import socket

import requests
from requests.adapters import HTTPAdapter

import merkle
import sync_common

hv = sync_common.load_hv()
SIZE = merkle.CHUNK_SIZE

# ── path-MTU resilience (see sync_daemon.SYNC_MAX_SEG) ───────────────────────────────────────────
# Many tailnets ride an underlay whose path MTU is below Tailscale's default 1280, silently
# blackholing full-size packets — so a multi-KB /sync/chunk stalls until timeout while small
# responses pass. Two defenses, both keeping Tailscale at its DEFAULTS (no `ip link` tweaks):
#   1. Clamp the outgoing TCP segment size on our connections (covers the PUSH/POST body).
#   2. Paginate PULL and PUSH into small batches so each transfer "catches its breath".
SYNC_MAX_SEG = int(os.environ.get("HIVE_SYNC_MAXSEG", "1000"))
PULL_PAGE = max(1, int(os.environ.get("HIVE_SYNC_PULL_PAGE", "25")))   # entries per /sync/chunk GET
PUSH_PAGE = max(1, int(os.environ.get("HIVE_SYNC_PUSH_PAGE", "25")))   # entries per /sync/ingest POST


class _ClampMSSAdapter(HTTPAdapter):
    """Cap the outgoing TCP segment size on sync connections so multi-KB bodies survive a
    sub-1280-MTU tailnet path. No-op where TCP_MAXSEG is unsupported or disabled (SYNC_MAX_SEG<=0)."""
    def init_poolmanager(self, *args, **kwargs):
        try:
            from urllib3.connection import HTTPConnection
            base = list(HTTPConnection.default_socket_options or [])
        except Exception:
            base = []
        if SYNC_MAX_SEG > 0 and hasattr(socket, "TCP_MAXSEG"):
            base.append((socket.IPPROTO_TCP, socket.TCP_MAXSEG, SYNC_MAX_SEG))
        kwargs["socket_options"] = base
        return super().init_poolmanager(*args, **kwargs)


_session = None


def _sess():
    """A shared requests.Session whose connections clamp the TCP segment size."""
    global _session
    if _session is None:
        s = requests.Session()
        adapter = _ClampMSSAdapter()
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _session = s
    return _session


def _local_entries():
    return merkle.read_all_entries(hv.JOURNAL_DIR)


def _get(base, path, **params):
    r = _sess().get(f"{base}{path}", params=params or None, timeout=15)
    r.raise_for_status()
    return r.json()


def _post(base, path, payload):
    r = _sess().post(f"{base}{path}", json=payload, timeout=60)
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

    # PULL differing/missing windows from the peer, paginated into PULL_PAGE-seq sub-windows so each
    # /sync/chunk response stays small (the peer chooses nothing — the window is ours to size). A new
    # entry written during the round is simply caught this round if ahead of our cursor, or on the
    # next round if behind: append is a G-Set union keyed by (node_id, seq), so re-pulls are no-ops
    # and the Merkle re-check keeps running until both sides match. Safe under concurrent writes.
    pulled = []
    for node, start, end in _differing_windows(merkle.node_chunk_hashes(local), remote_chunks):
        s = start
        while s <= end:
            e = min(s + PULL_PAGE - 1, end)
            data = _get(base, "/sync/chunk", node=node, start=s, end=e)
            pulled.extend(data.get("entries", []))
            s = e + 1

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
    # Paginate the push so each /sync/ingest body stays small; the daemon de-dups by (node_id, seq),
    # so a batch that partially overlaps prior state is idempotent.
    for i in range(0, len(push), PUSH_PAGE):
        pushed += _post(base, "/sync/ingest",
                        {"entries": push[i:i + PUSH_PAGE], "hive_id": local_hive}).get("accepted", 0)

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
