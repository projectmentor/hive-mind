"""Which plane runs a command, for tests that drive the CLI (2.0, public #136).

`hv` is the agent data plane and `hive-mind` (`hivemind_ctl.py`) the owner/operator control plane. A test
that drives a command which MOVED off `hv` keeps testing what it tested; it just reaches it where it now
lives. Tests of `hv`'s pointers use `HV` directly, and a test that means the owner-signed form of a link,
or an owner-policy capsule or cell write, names `CTL` itself: those move only conditionally, so they are
not routed here.
"""

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
HV = PROJECT / "hv"
CTL = PROJECT / "hivemind_ctl.py"

if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
import commandmap  # noqa: E402


def entry_for(args):
    """The script that runs `args`: `CTL` for a command that moved (S2/S6), `HV` for everything else."""
    a = [str(x) for x in args]
    # `owner propose-election --mint` is NOT routed: minting is its own command, `hive-mind owner mint`,
    # and proposing the minted key is `hv owner propose-election --pub`. A test that minted inline mints first.
    if commandmap.lookup(a) is not None or (a[:1] == ["retract"] and "--owner" in a):
        return CTL
    return HV


def install_control_plane(mod):
    """Give an in-process `hv` module its owner steps, as `hive-mind` does (2.0 S2)."""
    import hivemind_ctl
    return hivemind_ctl.install(mod)


def load_hv(home, name="hv_planes", control_plane=True):
    """`hv` loaded in-process FOR `home` (#160): `hv` resolves every path (journal, store, genesis pin, keys)
    from HIVE_HOME when it is imported, so a loader must say which hive it is about. HIVE_HOME is set to
    `home` for the import only, then put back. With `control_plane`, the owner steps are installed too."""
    import importlib.machinery
    import importlib.util
    import os
    prev = os.environ.get("HIVE_HOME")
    os.environ["HIVE_HOME"] = str(home)
    try:
        loader = importlib.machinery.SourceFileLoader(name, str(HV))
        m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        loader.exec_module(m)
    finally:
        if prev is None:
            os.environ.pop("HIVE_HOME", None)
        else:
            os.environ["HIVE_HOME"] = prev
    return install_control_plane(m) if control_plane else m
