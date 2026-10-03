"""2.1 PR 2: the module prefix, and a module's fleet-wide config key (plan §3 A4 names, M3).

`vocabulary.check_module_name` is the one validator the module API and `hv doctor` will call. `set-config` accepts
`x-<module>:<key>` as a string the core stores under `gov["module_config"]` and never interprets; a bare unknown
key is still refused, and a key from anyone but the owner changes nothing.
"""
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import vocabulary  # noqa: E402
from test_governance import _run, _loadhv as _gov_hv  # noqa: E402
from test_links import _entry, _owned_hive, _loadhv  # noqa: E402

V = vocabulary


@pytest.mark.parametrize("module,ok", [("hwatch", True), ("a", True), ("my-mod2", True), ("", False), ("2fast", False),
                                       ("Hwatch", False), ("a:b", False), ("trail-", False), ("x" * 33, False),
                                       (None, False)])
def test_module_names(module, ok):
    assert V.valid_module_name(module) is ok


def test_prefixed_names_split_and_a_malformed_one_does_not():
    assert V.split_module_name("x-hwatch:heartbeat") == ("hwatch", "heartbeat")
    assert V.split_module_name("x-hwatch:a.b_c-1") == ("hwatch", "a.b_c-1")
    for bad in ("hwatch:beat", "x-hwatch", "x-hwatch:", "x-:beat", "x-Hwatch:beat", "x-hwatch:a:b", "x-hwatch:-a",
                "x-hwatch: a", None, 3):
        assert V.split_module_name(bad) is None, bad


def test_a_module_may_use_a_core_link_kind_tag_and_context_and_define_only_a_prefixed_one():
    ok = V.check_module_name
    assert ok("hw", "link_kinds", "supports") is None and ok("hw", "link_kinds", "x-hw:beat") is None
    assert ok("hw", "behaviour_tags", "volatile") is None and ok("hw", "behaviour_tags", "ttl:2h") is None
    assert ok("hw", "behaviour_tags", "x-hw:seen") is None
    assert ok("hw", "source_contexts", "subagent") is None and ok("hw", "source_contexts", "x-hw:poller") is None
    assert ok("hw", "source_apps", "x-hw") is None
    assert ok("hw", "announce_kinds", "x-hw:hello") is None and ok("hw", "config_keys", "x-hw:poll") is None


@pytest.mark.parametrize("category,name", [
    ("link_kinds", "beat"), ("link_kinds", "x-other:beat"), ("link_kinds", "x-hw"), ("link_kinds", "x-hw:"),
    ("behaviour_tags", "perf"), ("behaviour_tags", "x-other:seen"),
    ("source_contexts", "owner"), ("source_contexts", "poller"),
    ("source_apps", "manual"), ("source_apps", "owner"), ("source_apps", "hw"), ("source_apps", "x-other"),
    ("source_apps", "x-hw:sub"),
    ("announce_kinds", "key"), ("announce_kinds", "hello"), ("config_keys", "cap_self"), ("config_keys", "poll"),
    ("entry_types", "x-hw:thing"), ("entry_types", "fact"), ("governance_actions", "x-hw:act"),
    ("channels", "x-hw:chan"), ("channels", "act"), ("envelope_fields", "x-hw:id"), ("nonsense", "x-hw:a"),
])
def test_everything_else_is_refused_with_a_reason(category, name):
    reason = V.check_module_name("hw", category, name)
    assert reason and "\n" not in reason


def test_an_invalid_module_is_refused():
    assert V.check_module_name("Bad", "link_kinds", "supports")


def test_no_core_name_looks_prefixed_and_the_validator_covers_every_category():
    for key, _title, _what, table in V.CATEGORIES:
        assert key in V.MODULE_RULES
        assert not any(n.startswith(V.MODULE_PREFIX) for n in table)
        if key not in ("entry_types", "governance_actions", "channels", "envelope_fields"):
            assert V.check_module_name("hw", key, "x-hw" if key == "source_apps" else "x-hw:probe") is None, key


# ── the module envelope names ─────────────────────────────────────────────────────────────────────────

def test_the_envelope_names_have_their_meaning_and_stay_reserved():
    for name in ("from", "to", "target", "id"):
        rec = V.ENVELOPE_FIELDS[name]
        assert rec["status"] == V.RESERVED and "module" in rec["meaning"]
    for name in ("from", "to"):
        assert "_resolve_ref" in V.ENVELOPE_FIELDS[name]["meaning"]


# ── M3: fleet-wide module config ──────────────────────────────────────────────────────────────────────

def _config(hv, entries):
    return hv._governance_state(entries)


def _setcfg(hv, dev, key, value, ts, owner=None):
    return _entry(hv, dev, "governance", {"action": "set-config", "key": key, "value": value}, ts, owner=owner)


def test_an_owner_set_prefixed_key_projects_and_a_non_owner_one_does_not(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1, _d2), base = _owned_hive(hv)
    o = (owner[0], owner[1])
    entries = base + [
        _setcfg(hv, d0, "x-hwatch:poll", "30s", "2026-01-02T00:00:01Z", owner=o),
        _setcfg(hv, d0, "x-other:poll", "5m", "2026-01-02T00:00:02Z", owner=o),
        _setcfg(hv, d1, "x-hwatch:evil", "1", "2026-01-02T00:00:03Z"),                 # a device, unsigned by the owner
    ]
    gov = _config(hv, entries)
    assert gov["module_config"] == {"x-hwatch:poll": "30s", "x-other:poll": "5m"}      # visible under each prefix
    assert not any(k.startswith("x-") for k in gov["config"])                         # the numeric table is untouched


def test_the_last_owner_write_wins_and_a_bad_key_or_value_is_ignored(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, _d1, _d2), base = _owned_hive(hv)
    o = (owner[0], owner[1])
    sets = [("x-hw:k", "one"), ("x-hw:k", "two"), ("x-hw:num", 5), ("x-hw:long", "y" * (V.MODULE_VALUE_MAX + 1)),
            ("x-Hw:k", "no"), ("x-hw:a:b", "no"), ("hw:k", "no"), ("poll", "no"), ("x-hw:ok", "y" * V.MODULE_VALUE_MAX)]
    entries = base + [_setcfg(hv, d0, k, v, f"2026-01-02T00:00:{i:02d}Z", owner=o) for i, (k, v) in enumerate(sets)]
    gov = _config(hv, entries)
    assert gov["module_config"] == {"x-hw:k": "two", "x-hw:ok": "y" * V.MODULE_VALUE_MAX}


def test_a_node_with_no_owner_has_an_empty_module_config(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    assert hv._governance_state([])["module_config"] == {} and hv._DEFAULT_GOV["module_config"] == {}


def test_the_cli_accepts_a_prefixed_key_and_still_refuses_a_bare_unknown_one(tmp_path):
    import merkle
    _run(tmp_path, "owner", "init")
    ok = _run(tmp_path, "config", "set", "x-hwatch:poll", "30s")
    assert "set x-hwatch:poll = 30s" in ok.stdout
    bad = _run(tmp_path, "config", "set", "poll", "30s")
    assert "unknown config key 'poll'" in bad.stdout
    _run(tmp_path, "config", "set", "x-Hwatch:poll", "1")                            # malformed: the same refusal
    gov = _gov_hv()._governance_state(merkle.read_all_entries(str(tmp_path / "journal")))
    assert gov["module_config"] == {"x-hwatch:poll": "30s"}
