"""`hive-mind` — the owner/operator control plane (2.0, public #136).

The split gives the fleet two commands over one library: `hv` is the agent **data plane**, and this is
the **control plane**, where the owner key is loaded and governance is signed. Requirement S2 is that
`hv` cannot owner-sign at all, and the static tests assert it.

**The split (PR 2b).** `hv` no longer imports `ownerkey` and produces no owner signature. Every owner step
lives in `hivemind_owner.py`, which `install` runs into the library's namespace in THIS process only: its
definitions replace `hv`'s data-plane placeholders (which refuse with the `hive-mind` form to run), so a
command's structure is written once, in `hv`, and the flags once, in `hv`'s parser (S1). The old `hv` names
point here and exit 2 (S6).

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

# NO sys.path insert, and none is needed. Run as a script, Python already puts this file's directory
# first on sys.path; imported by a test, the caller has already put the project there. An insert at
# import time would be the same fault #152 fixed in `ownerkey.py`: this file is BOTH an entry point and
# an imported module, so an import-time insert would prepend the real source directory and hide a staged
# copy for everything loaded afterwards. Entry-point-only path setup would still be legitimate — but it
# would have to happen inside the function the dispatcher runs, not here.
import commandmap

_hv = None
OWNER_SRC = ROOT / "hivemind_owner.py"


def hv():
    """The library, loaded once, with its owner steps installed. `hv` has no `.py` extension, so it is
    loaded by path — the same way `sync_common.load_hv` does it for the sync daemon, which has consumed
    `hv` as a library since #7."""
    global _hv
    if _hv is None:
        loader = importlib.machinery.SourceFileLoader("hvlib", str(ROOT / "hv"))
        spec = importlib.util.spec_from_loader("hvlib", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _hv = install(mod)
    return _hv


def install(lib):
    """Give a loaded library its owner steps, in this process only (2.0 S2). Runs `hivemind_owner.py` in
    the library's own namespace, so each definition there replaces the data-plane placeholder of the same
    name and the moved bodies keep their names, globals and bytes. Idempotent. Tests that call an owner
    step in-process install it the same way."""
    if getattr(lib, "_CONTROL_PLANE", False):
        return lib
    ns = lib.__dict__
    ns["_data_plane_owner_cmd"] = ns["owner_cmd"]           # wrapped: `hv` keeps show/elections/propose/vote
    ns["_data_plane_link_payload"] = ns["_link_payload"]    # wrapped: `hv` builds, this plane owner-signs
    exec(compile(OWNER_SRC.read_text(), str(OWNER_SRC), "exec"), ns)
    lib._CONTROL_PLANE = True
    return lib


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
    head = argv[0]
    if tuple(argv[:1]) in {("unforget",), ("admit",)}:
        return True
    # The owner-signed form of the link verbs (decision h:34cc1dbcd3): through `hv` their links are
    # device-signed; here, with source `manual`, they are owner-signed.
    if head in commandmap.OWNER_LINK_VERBS:
        return True
    # Flag-conditional: the flag, not the verb, decides. `retract --owner` is governance; plain `hv retract`
    # is peer negative evidence. `doctor --fix` here makes the operator-state repairs. A capsule write or
    # `wire --add` here can owner-sign, which an owner policy requires.
    if head == "retract":
        return "--owner" in argv
    if head == "doctor":
        return "--fix" in argv
    if head == "capsule":
        return argv[1:2] in (["put"], ["rotate"], ["rm"])
    if head == "wire":
        return "--add" in argv
    lib = _to_library_argv(argv)
    return commandmap.lookup(lib) is not None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__.strip().splitlines()[0])
        return 2
    # A 1.x `hv` alias removed in 2.0 is suggested by its new name: suggesting `hv rebuild` would send the
    # operator to a second pointer rather than to something that runs. Checked first, because
    # `doctor --fix wire-agent` would otherwise route here and die in argparse without naming anything.
    old = commandmap.renamed(argv)
    if old is not None or not _is_control_plane(argv):
        suggest = old[1] if old is not None else argv
        print(f"hive-mind: `{' '.join(argv)}` is not a control-plane command.\n"
              f"  The agent data plane is `hv`; try `hv {' '.join(suggest)}`.", file=sys.stderr)
        return 2
    m = hv()
    parser = m.build_parser()                     # the library owns every flag definition
    args = parser.parse_args(_to_library_argv(argv))
    try:
        m._unlock_before_writing(args)            # a link verb: a locked key aborts before any write (#167)
        return m.dispatch(args)
    except m.OwnerKeyLocked as e:                 # sealed and not unlocked: nothing signed or written
        print(f"hive-mind: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
