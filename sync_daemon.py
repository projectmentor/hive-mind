#!/usr/bin/env python3
"""Back-compat shim — `sync_daemon.py` was renamed to `hive_sync_daemon.py` (a descriptive,
hive-namespaced process name that won't collide with an unrelated `sync_daemon`). This shim
re-exports the daemon for ONE release so a service/cron entry still pointing at the old path keeps
working until `hive-mind update` rewrites the unit files. Remove in the next release.

New code should `import hive_sync_daemon` (or launch `hive_sync_daemon.py`) directly.
"""
from hive_sync_daemon import *  # noqa: F401,F403
from hive_sync_daemon import make_server, serve_forever, run_daemon  # explicit entry points

if __name__ == "__main__":
    run_daemon()
