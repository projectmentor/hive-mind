# _smoke_daemon.sh — sourced by sync_smoke.sh and sync_auth_smoke.sh (#161).
#
# Each smoke starts two sync daemons. Two things used to go wrong, and both are fixed here:
#
#   * The ports were fixed (19876/19877, 19886/19887). Two smoke runs at once, the two callers in
#     tests/test_sync.py on different xdist workers or a run by hand beside the suite, shared them,
#     and their daemons cross-talked: one run's B pulled the other run's A. Ports are now free ones
#     picked per run, and a HIVE_SMOKE_PORT_* override still pins one for debugging.
#   * Readiness was `curl --retry N --retry-connrefused --retry-delay 0`. A delay of 0 means curl's
#     default backoff, 1s doubling to 10 minutes, so a daemon that never came up (its port taken,
#     or a crash, with its output in /dev/null) left the smoke waiting for hours instead of failing.
#     The wait is now bounded, notices the daemon exiting, and checks that the daemon answering is
#     this run's own: it must serve the Merkle root of the hive the run started it on. Every other
#     curl in the smokes carries --max-time, so a daemon that accepts and never answers fails the
#     run instead of hanging it (the orphaned curl on #159's cancelled macOS job).

# smoke_free_port [host] — print a TCP port on <host> (default 127.0.0.1) that nothing is listening on now.
# Drawn at random from 20000-32767, BELOW the kernel's ephemeral range (Linux from 32768, macOS from 49152).
# Asking the kernel (`bind(0)`, then close) raced: two smoke runs started together could each be handed the
# same just-closed port before either daemon bound it (#173, the residual Fable recorded on #163). Nothing
# else on the machine is handed a port from this range either, and a clash is retried with another draw.
smoke_free_port() {
  python3 - "${1:-127.0.0.1}" <<'PY'
import random, socket, sys
rng = random.SystemRandom()
for _ in range(200):
    port = rng.randint(20000, 32767)
    s = socket.socket()
    try:
        s.bind((sys.argv[1], port))
    except OSError:
        continue
    finally:
        s.close()
    print(port)
    break
else:
    sys.exit("smoke_free_port: no free port in 20000-32767")
PY
}

# smoke_wait_daemon <name> <pid> <host> <port> <hive_home> <log>
# Return 0 once the daemon <pid> serves <hive_home>'s Merkle root on <host>:<port>. Otherwise print why
# (the daemon exited, another process answers on the port, or the deadline passed), with the daemon's
# log, and return 1. Bounded by SMOKE_WAIT_SECONDS (default 45: a cold daemon imports hv and the crypto
# before it binds, which can take over 15s on a loaded macOS runner).
smoke_wait_daemon() {
  local name="$1" pid="$2" host="$3" port="$4" home="$5" log="$6"
  local want got deadline
  want="$(HIVE_HOME="$home" "$HV" doctor merkle | awk '/^Root:/{print $2}')"
  deadline=$(( $(date +%s) + ${SMOKE_WAIT_SECONDS:-45} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "  daemon $name (pid $pid) exited before serving on $host:$port: its port was taken, or it crashed. Its log:"
      sed 's/^/    /' "$log" 2>/dev/null | tail -20
      return 1
    fi
    got="$(curl -s --max-time 2 "http://$host:$port/sync/merkle-root" 2>/dev/null \
           | python3 -c 'import json, sys; print(json.load(sys.stdin).get("root_hash", ""))' 2>/dev/null)"
    if [ -n "$got" ]; then
      [ "$got" = "$want" ] && return 0
      echo "  port $port answers, but not with daemon $name: it serves root $got, and this run's $name is $want."
      echo "  Another process holds the port (a second smoke run?)."
      return 1
    fi
    sleep 0.2
  done
  echo "  daemon $name did not serve on $host:$port within ${SMOKE_WAIT_SECONDS:-45}s. Its log:"
  sed 's/^/    /' "$log" 2>/dev/null | tail -20
  return 1
}
