"""`hive-mind` — the owner/operator control plane (2.0, public #136).

The split gives the fleet two commands over one library: `hv` is the agent **data plane**, and this is
the **control plane**, where the owner key is loaded and governance is signed. Requirement S2 is that
`hv` cannot owner-sign at all, and the static tests assert it.

**In this change nothing has moved yet.** This module is a front end: it parses the control-plane surface
and delegates to the handlers that still live in the library. So `hv` keeps working exactly as it does
today, no logic is duplicated, and the suite stays green. The next change moves the handler bodies here,
drops `hv`'s `import ownerkey`, and turns the old `hv` names into pointers — that is where the guarantee
arrives, and where it should be reviewed on its own.

Why a `.py` module rather than an extensionless `hive-mind` binary: the signed source manifest covers
tracked files by suffix and special-cases only the name `hv`, so an extensionless second entry point
would ship **unsigned** — the one file holding the owner-key path. `scripts/installer/dispatcher.sh`
remains the `hive-mind` command, is already covered by `.sh`, is already symlinked into the operator's
PATH, and simply `exec`s this. It never reads the owner key, never passes a seed on a command line, and
never puts one in the environment.
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))          # an entry point may set sys.path; a library module may not

import commandmap  # noqa: E402

_hv = None


def hv():
    """The library, loaded once. `hv` has no `.py` extension, so it is loaded by path — the same way
    `sync_common.load_hv` does it for the sync daemon, which has consumed `hv` as a library since #7."""
    global _hv
    if _hv is None:
        loader = importlib.machinery.SourceFileLoader("hvlib", str(ROOT / "hv"))
        spec = importlib.util.spec_from_loader("hvlib", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _hv = mod
    return _hv


# Control-plane argv -> the library argv that implements it. The only entries that differ are the ones
# the split renames or collapses, and each is named here rather than inferred, so a reader can see
# exactly what `hive-mind X` runs. Built against commandmap.MOVED, which is the source of truth for what
# belongs on this plane at all.
_RENAMED = {
    ("owner", "revoke"): ("owner", "revoke-escrow"),        # S5: the hyphen goes with the move
    ("config", "set"): ("config", "set"),                   # the three set forms collapse to this one
}


def _to_library_argv(argv):
    """Translate a control-plane invocation into the library's own argv, so the flags are defined once —
    in the library's parser — and this module cannot drift from them."""
    for n in (2, 1):
        key = tuple(argv[:n])
        if key in _RENAMED:
            return list(_RENAMED[key]) + argv[n:]
    return list(argv)


def _is_control_plane(argv):
    """Whether this invocation belongs to the control plane, per the shared table."""
    if not argv:
        return False
    if tuple(argv[:1]) in {("unforget",), ("admit",)}:
        return True
    # Flag-conditional: `retract --owner` is governance and belongs here; plain `hv retract` is peer
    # negative evidence and stays on the data plane. The flag, not the verb, decides.
    if argv[0] == "retract":
        return "--owner" in argv
    lib = _to_library_argv(argv)
    return commandmap.lookup(lib) is not None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__.strip().splitlines()[0])
        return 2
    if not _is_control_plane(argv):
        print(f"hive-mind: `{' '.join(argv)}` is not a control-plane command.\n"
              f"  The agent data plane is `hv`; try `hv {' '.join(argv)}`.", file=sys.stderr)
        return 2
    m = hv()
    parser = m.build_parser()                     # the library owns every flag definition
    args = parser.parse_args(_to_library_argv(argv))
    return m.dispatch(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
