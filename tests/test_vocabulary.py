"""The core vocabulary's tripwires (2.0, public #136 and #150): `vocabulary.py` and the code agree, both ways.

A journal entry whose type, link kind or governance action a node does not recognise lands and projects to
nothing, and the journal is permanent. So a bare name core reuses after a module wrote it would be read with
the new meaning forever. `vocabulary.py` reserves core's names; this file holds it to the code:

* every name WRITTEN at an enumerated writer site is registered, with status `written`;
* every name READ at an enumerated reader site is registered (`written`, `legacy` or `read`);
* every registered name is used: `written` by a writer site, `legacy` and `read` by a reader site, and a
  `reserved` envelope field by no site at all. A registry entry nothing writes or reads fails.

The sites are ENUMERATED, not pattern-matched across whole files, because the same words mean other things
nearby: `action ==` is also a CLI subcommand (`show`, `rotate`, `seal`), `kind ==` also a search kind (`all`)
or a cell kind (`tool`), and `type ==` also a Claude Code hook (`command`). A writer site is a specific call
or literal shape, which is precise wherever it appears; a reader site is a named function. Any function that
compares an entry's `type`, `kind` or `action` to a literal must be classified below, as a reader or as not
journal vocabulary, so a new reader cannot land unexamined.

WRITER SITES (every file in SCANNED)
  entry types     every `append_journal(<type>, …)` call. One passes a variable: `hv:wire_cmd`'s `t`,
                  enumerated from its guard `if t not in ("cell", "comb") …: return`.
  link kinds      every `_link_payload(<kind>, …)` / `_data_plane_link_payload(<kind>, …)` call. One passes a
                  variable: `hv:remember`'s `evidence_kind`, enumerated from `for flag in ("supports",
                  "contradicts")`. `hivemind_owner.py:_link_payload` passes its own `kind` through to `hv`'s
                  builder (the owner-signing wrapper), so it is not a site of its own.
  gov. actions    every dict literal with an `"action"` key (in these files, always a governance payload).
                  One is a variable: `hivemind_owner.py:_group_change`'s `action`, enumerated from its
                  callers: literals, and `hv:group_cmd`'s verb, which is every `group_sub.add_parser(…)`
                  name except `list` and `admit` (GROUP_VERBS_ELSEWHERE).
  announce kinds  the `"kind"` of an `{"action": "announce", …}` literal.
  config keys     `hivemind_owner.py:_config_set`'s `coercers` table (the gate every `set-config` passes,
                  including its `**{k: … for k in _PR6_KNOB_DEFAULTS}` entry, resolved from `hv`), every
                  `_config_set("<key>", …)` call, and a literal `"key"` in a `set-config` payload.
  channels        `hv`'s `--channel` argparse choices, a literal or `… or "<channel>"` value of a `"channel"`
                  key in a dict literal, and a literal `channel=` passed to a link builder.
  behaviour tags  every `tags.append(<tag>)` call.
  source apps     a literal `"source"` in a dict literal (its app, and its class slot as a source context
  and contexts    class), and `hv:_write_source`'s default.
  envelope        `hv:append_journal`'s `entry` dict, `hv:_sign_entry`'s stores, the `"action"` key of a
                  governance literal, the `"kind"` key of an announce literal and of `hv:_link_payload`'s dict.

READER SITES: READERS below, plus these tables and call shapes in `hv`: `_PASS1` and `_LINK_RESOLVERS` (their
keys), `OWNER_APPS` and `SRC_CLASS_WEIGHT`, `_projection_latest(_, "<type>")`,
`_content_unauthorized(_, _, "<type>", "<config key>")`, `_content_authz(_, "<config key>")`, any comparison
with `_channel(…)`, the defaults of `_channel` and `_parse_source`, and any `.get("source", "<app>")`.

Directive `h:157bd5e469`: a guard is not trusted on its docstring. The MUTANT tests at the end feed the
checker a copy of the code with one unregistered name at a site of each category, and a registry with an
entry nothing uses, and each must be reported.
"""

import ast
import importlib.util
import re
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import vocabulary  # noqa: E402

# Every module that writes or projects journal entries, and the sync modules that carry them. The sync
# modules have no sites today; they are scanned so that a first one there is held to the registry too.
SCANNED = ("hv", "hivemind_owner.py", "hivemind_ctl.py", "hive_sync_daemon.py", "sync_client.py",
           "sync_common.py", "merkle.py", "ownerkey.py")

REGISTRY = {key: table for key, _title, _what, table in vocabulary.CATEGORIES}
READABLE = (vocabulary.WRITTEN, vocabulary.LEGACY, vocabulary.READ)

# The enumerated READER functions, (file, function), per accessor. Each compares an entry's field (or a
# name assigned from it) to literals, and each must still do so (test_every_enumerated_reader_still_reads).
READERS = {
    "type": {
        ("hv", "append_foreign_entries"),       # ingest: governance first, then admission-gated content
        ("hv", "_capsule_version_conflicts"),
        ("hv", "_self_signed_owner_acts"),      # genesis candidates
        ("hv", "_governance_state"),
        ("hv", "_governance_state_uncached"),   # the governance walk
        ("hv", "_owner_declaration"),
        ("hv", "_content_evidence"),            # confidence: facts, retracts, evidence links
        ("hv", "_decision_evidence"),           # outcome scores
        ("hv", "_idea_evidence"),
        ("hv", "_signer_reliability"),          # trust velocity
        ("hv", "_link_attention"),              # importance
        ("hv", "_utility_evidence"),            # utility
        ("hv", "_salience_rows"),
        ("hv", "_forgets_grandfathered"),
        ("hv", "_grandfather_facts"),           # doctor forget-authz, `hive-mind doctor --fix` (4c)
        ("hv", "_links_unauthorized"),          # doctor link-authz
        ("hv", "rebuild_db"),                   # pass 2: the legacy resolvers and `link`
        ("hv", "_pending_admissions"),
        ("hv", "_compute_audit"),
        ("hv", "_recipient_pubkeys"),           # capsule recipients
        ("hv", "_doctor_status"),
        ("hv", "_join_request_url"),
        ("hv", "_join_request_label"),
    },
    "kind": {                                   # a `link` payload's kind
        ("hv", "_decision_evidence"),
        ("hv", "_idea_evidence"),               # `{"supports": …, "contradicts": …}.get(kind)`
        ("hv", "_signer_reliability"),
        ("hv", "_utility_evidence"),
        ("hv", "_links_unauthorized"),
    },
    "action": {                                 # a `governance` payload's action
        ("hv", "append_foreign_entries"),       # the authority-less allowlist, the pinned genesis
        ("hv", "_self_signed_owner_acts"),
        ("hv", "_governance_state_uncached"),   # every act the walk honours
        ("hv", "_owner_declaration"),
        ("hv", "_pending_admissions"),
        ("hv", "_recipient_pubkeys"),
        ("hv", "_join_request_url"),
        ("hv", "_join_request_label"),
    },
    "key": {                                    # a `set-config` payload's key
        ("hv", "_governance_state_uncached"),
    },
    "tag": {                                    # a fact's tags
        ("hv", "_fact_class"),                  # the salience class: volatile decays faster
        ("hv", "remember"),                     # the auto-tag: already volatile, or ttl:
        ("hv", "api_search"),                   # `--status volatile`
        ("hv", "_compute_audit"),               # recheck: volatile, ttl:, the durable opt-out
        ("hv", "_volatile_ttl_hours"),          # the ttl: freshness window
    },
    "source": {                                 # a write's source app
        ("hivemind_owner.py", "_link_payload"),           # owner-signs only a `manual` link
        ("hivemind_owner.py", "_will_owner_sign_links"),
    },
    "context": {                                # a source's context class
        ("hv", "_content_evidence"),            # an owner-class retract is a forget
        ("hv", "_forgets_grandfathered"),
    },
}

# Functions that compare a `type`, `kind` or `action` to a literal that is NOT journal vocabulary. Named, with
# the reason, so that a new reader cannot hide among them.
NOT_JOURNAL = {
    "type": {
        ("hv", "_agent_hook_reconcile"): "Claude Code hook specs, `{\"type\": \"command\"}`",
        ("hv", "_wire_agent"): "Claude Code hook specs, `{\"type\": \"command\"}`",
    },
    "kind": {
        ("hv", "_wire_one"): "a cell's kind (`agent` or `tool`), which `hv wire` dispatches on",
    },
    "action": {},
}

# `hv group` verbs that never reach `_group_change`: they write through a site of their own, or not at all.
GROUP_VERBS_ELSEWHERE = {"list": "prints the roster; writes nothing",
                         "admit": "`admit_cmd`, whose `{\"action\": \"admit\"}` literal is its own site"}

# The owner-signing wrapper: builds with `hv`'s own builder, passing its `kind` through, then signs.
LINK_PASS_THROUGH = {("hivemind_owner.py", "_link_payload")}


# ── AST helpers ─────────────────────────────────────────────────────────────────────────────────────

def _sources(overrides=None):
    src = {f: (PROJECT / f).read_text() for f in SCANNED}
    src.update(overrides or {})
    return src


def _units(tree):
    """(name, node): every top-level function, every method (`Class.method`), and the module-level
    statements together as `<module>`. A nested function belongs to the function that holds it."""
    module = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{node.name}.{sub.name}", sub
        else:
            module.append(node)
    yield "<module>", ast.Module(body=module, type_ignores=[])


def _unit(tree, name):
    return [node for n, node in _units(tree) if n == name]


def _is_str(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _strs(node):
    """The string literals `node` is: "x", or ("x", "y") / ["x"] / {"x"}."""
    if _is_str(node):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in node.elts if _is_str(e)]
    return []


def _field(node):
    """The field `node` reads off a dict, `x.get("f", …)` or `x["f"]`, else None."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and _is_str(node.args[0])):
        return node.args[0].value
    if isinstance(node, ast.Subscript) and _is_str(node.slice):
        return node.slice.value
    return None


def _pairs(target, value):
    """(target, value) element-wise for `a, b = x, y`; value None where it cannot be paired."""
    if isinstance(target, (ast.Tuple, ast.List)):
        vals = value.elts if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts) \
            else [None] * len(target.elts)
        for t, v in zip(target.elts, vals):
            yield from _pairs(t, v)
    else:
        yield target, value


def _assignments(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                yield from _pairs(tgt, n.value)


def _dict_items(d):
    return [(k.value if _is_str(k) else k, v) for k, v in zip(d.keys, d.values)]


def _dict_get(d, key):
    return next((v for k, v in _dict_items(d) if k == key), None)


def _module_table(tree, name):
    """The string keys (dict) or elements (set, tuple) of a module-level `name = {…}`."""
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)):
            if isinstance(node.value, ast.Dict):
                return [k.value for k in node.value.keys if _is_str(k)]
            return _strs(node.value)
    return None


def _calls(tree, name):
    """(unit, unit node, call) for every call to `name`, bare or as an attribute (`x.name(…)`)."""
    for unit, node in _units(tree):
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and (
                    (isinstance(n.func, ast.Name) and n.func.id == name)
                    or (isinstance(n.func, ast.Attribute) and n.func.attr == name)):
                yield unit, node, n


def _field_accessor(field):
    return lambda n: _field(n) == field


def _is_call_to(name):
    return lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name


def _compared(unit, is_acc, tables=None):
    """[(line, name)]: every literal `unit` compares an accessor to: `acc == "x"`, `acc in ("x", "y")`,
    `acc in TABLE` (a module table given in `tables`), and the keys of a literal lookup `{"x": …}.get(acc)`.
    A name assigned from the accessor (`action = p.get("action")`) counts as the accessor."""
    aliases = {t.id for t, v in _assignments(unit) if isinstance(t, ast.Name) and v is not None and is_acc(v)}

    def hit(n):
        return is_acc(n) or (isinstance(n, ast.Name) and n.id in aliases)

    out = []
    for n in ast.walk(unit):
        if isinstance(n, ast.Compare):
            sides = [n.left, *n.comparators]
            if any(hit(s) for s in sides):
                for s in sides:
                    out += [(n.lineno, v) for v in _strs(s)]
                    if tables and isinstance(s, ast.Name) and s.id in tables:
                        out += [(n.lineno, v) for v in tables[s.id]]
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
              and isinstance(n.func.value, ast.Dict) and n.args and hit(n.args[0])):
            out += [(n.lineno, k.value) for k in n.func.value.keys if _is_str(k)]
    return out


def _source_parts(s):
    """(app, context class or None) of a literal source, by `hv:_parse_source`'s rule."""
    if ":" not in s:
        return s, None
    app, _, rest = s.partition(":")
    return app, (rest.split("/")[0] or None)


# ── resolving the writer sites that pass a variable ─────────────────────────────────────────────────

def _guard_values(unode, var):
    """The literals of the one `if <var> not in (<literals>) …: return` guard in the function."""
    found = [set(_strs(n.comparators[0])) for n in ast.walk(unode)
             if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == var
             and len(n.ops) == 1 and isinstance(n.ops[0], ast.NotIn) and _strs(n.comparators[0])]
    return found[0] if len(found) == 1 else None


def _loop_values(unode, var):
    """`var`'s values when every assignment to it is None or a loop variable of a `for x in (<literals>)`."""
    loops = {n.target.id: set(_strs(n.iter)) for n in ast.walk(unode)
             if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and _strs(n.iter)}
    vals = set()
    for t, v in _assignments(unode):
        if not (isinstance(t, ast.Name) and t.id == var):
            continue
        if isinstance(v, ast.Constant) and v.value is None:
            continue
        if isinstance(v, ast.Name) and v.id in loops:
            vals |= loops[v.id]
            continue
        return None
    return vals or None


def _subcommands(tree, subparsers):
    """Every `<subparsers>.add_parser("<name>", …)` name."""
    return {c.args[0].value for _u, _n, c in _calls(tree, "add_parser")
            if isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name)
            and c.func.value.id == subparsers and c.args and _is_str(c.args[0])}


def _group_change_actions(trees, _unode):
    """Every action `_group_change` can be called with: its literal arguments, and `hv:group_cmd`'s verb."""
    vals = set()
    for f, tree in trees.items():
        for unit, _n, call in _calls(tree, "_group_change"):
            a = call.args[0] if call.args else None
            if _is_str(a):
                vals.add(a.value)
            elif isinstance(a, ast.Name) and (f, unit) == ("hv", "group_cmd"):
                vals |= _subcommands(trees["hv"], "group_sub") - set(GROUP_VERBS_ELSEWHERE)
            else:
                return None
    return vals or None


# (file, function, variable) -> resolver(trees, function node) -> the values, or None if it no longer resolves
VARIABLE_SITES = {
    "entry_types": {("hv", "wire_cmd", "t"): lambda trees, u: _guard_values(u, "t")},
    "link_kinds": {("hv", "remember", "evidence_kind"): lambda trees, u: _loop_values(u, "evidence_kind")},
    "governance_actions": {("hivemind_owner.py", "_group_change", "action"): _group_change_actions},
}


# ── the scan ────────────────────────────────────────────────────────────────────────────────────────

class Sites:
    def __init__(self):
        self.writes = {k: {} for k in REGISTRY}      # category -> name -> [site]
        self.reads = {k: {} for k in REGISTRY}
        self.unresolved = []                         # writer sites whose value cannot be enumerated

    def w(self, cat, name, site):
        self.writes[cat].setdefault(name, []).append(site)

    def r(self, cat, name, site):
        self.reads[cat].setdefault(name, []).append(site)


def _resolve(S, trees, cat, f, unit, unode, arg, site):
    """Record a writer site's argument: a literal, or an enumerated variable. Anything else is unresolved."""
    if _is_str(arg):
        S.w(cat, arg.value, site)
        return
    key = (f, unit, arg.id) if isinstance(arg, ast.Name) else None
    if key in VARIABLE_SITES.get(cat, {}):
        vals = VARIABLE_SITES[cat][key](trees, unode)
        if vals:
            for v in sorted(vals):
                S.w(cat, v, f"{site} ({arg.id})")
            return
        S.unresolved.append(f"{site}: the enumeration of `{arg.id}` no longer resolves")
        return
    S.unresolved.append(f"{site}: {cat} passed as `{ast.unparse(arg) if arg is not None else '(nothing)'}`, "
                        "which is neither a literal nor an enumerated variable")


def collect(sources):
    trees = {f: ast.parse(src) for f, src in sources.items()}
    S = Sites()
    hv = trees["hv"]
    pr6 = _module_table(hv, "_PR6_KNOB_DEFAULTS") or []

    for f, tree in trees.items():
        # entry types: append_journal(<type>, …)
        for unit, unode, call in _calls(tree, "append_journal"):
            _resolve(S, trees, "entry_types", f, unit, unode, call.args[0] if call.args else None,
                     f"{f}:{unit}:{call.lineno}")

        # link kinds: the link payload builders
        for builder in ("_link_payload", "_data_plane_link_payload"):
            for unit, unode, call in _calls(tree, builder):
                site = f"{f}:{unit}:{call.lineno}"
                arg = call.args[0] if call.args else None
                params = [a.arg for a in unode.args.args] if isinstance(unode, ast.FunctionDef) else []
                if ((f, unit) in LINK_PASS_THROUGH and isinstance(arg, ast.Name) and params
                        and arg.id == params[0]):
                    continue
                _resolve(S, trees, "link_kinds", f, unit, unode, arg, site)
                for kw in call.keywords:
                    if kw.arg == "channel" and _is_str(kw.value):
                        S.w("channels", kw.value.value, site)

        for unit, unode in _units(tree):
            for n in ast.walk(unode):
                if not isinstance(n, ast.Dict):
                    continue
                site = f"{f}:{unit}:{n.lineno}"
                items = dict((k, v) for k, v in _dict_items(n) if isinstance(k, str))
                # governance actions: {"action": …}
                if "action" in items:
                    S.w("envelope_fields", "action", site)
                    act = items["action"]
                    _resolve(S, trees, "governance_actions", f, unit, unode, act, site)
                    if _is_str(act) and act.value == "announce":
                        S.w("envelope_fields", "kind", site)
                        if _is_str(items.get("kind")):
                            S.w("announce_kinds", items["kind"].value, site)
                        else:
                            S.unresolved.append(f"{site}: an announce without a literal `kind`")
                    if _is_str(act) and act.value == "set-config":
                        key = items.get("key")
                        if _is_str(key):
                            S.w("config_keys", key.value, site)
                        elif not (isinstance(key, ast.Name) and (f, unit) == ("hivemind_owner.py", "_config_set")):
                            S.unresolved.append(f"{site}: a set-config key outside the `_config_set` gate")
                # sources: {"source": "<app>:<class>/…"}
                if _is_str(items.get("source")):
                    app, ctx = _source_parts(items["source"].value)
                    S.w("source_apps", app, site)
                    if ctx:
                        S.w("source_contexts", ctx, site)
                # channels: {"channel": "x"} or {"channel": … or "x"}
                ch = items.get("channel")
                if ch is not None:
                    if _is_str(ch):
                        S.w("channels", ch.value, site)
                    elif isinstance(ch, ast.BoolOp) and _is_str(ch.values[-1]):
                        S.w("channels", ch.values[-1].value, site)

        # behaviour tags: tags.append(<tag>)
        for unit, _unode, call in _calls(tree, "append"):
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) \
                    and call.func.value.id == "tags":
                site = f"{f}:{unit}:{call.lineno}"
                if call.args and _is_str(call.args[0]):
                    S.w("behaviour_tags", call.args[0].value, site)
                else:
                    S.unresolved.append(f"{site}: a tag appended that is not a literal")

        # config keys: the `_config_set` gate's table, and literal calls to it
        for unit, _unode, call in _calls(tree, "_config_set"):
            if call.args and _is_str(call.args[0]):
                S.w("config_keys", call.args[0].value, f"{f}:{unit}:{call.lineno}")
    for unode in _unit(trees["hivemind_owner.py"], "_config_set"):
        tables = [v for t, v in _assignments(unode) if isinstance(t, ast.Name) and t.id == "coercers"]
        if len(tables) != 1 or not isinstance(tables[0], ast.Dict):
            S.unresolved.append("hivemind_owner.py:_config_set: no `coercers` table (the set-config gate moved)")
            continue
        for k, v in zip(tables[0].keys, tables[0].values):
            site = f"hivemind_owner.py:_config_set:{v.lineno}"
            if _is_str(k):
                S.w("config_keys", k.value, site)
            elif (k is None and isinstance(v, ast.DictComp) and isinstance(v.generators[0].iter, ast.Name)
                  and _module_table(hv, v.generators[0].iter.id)):
                for key in _module_table(hv, v.generators[0].iter.id):
                    S.w("config_keys", key, f"{site} ({v.generators[0].iter.id})")
            else:
                S.unresolved.append(f"{site}: a coercer key that is neither a literal nor a table in hv")

    # channels: hv's --channel choices
    for unit, _unode, call in _calls(hv, "add_argument"):
        if call.args and _is_str(call.args[0]) and call.args[0].value == "--channel":
            for kw in call.keywords:
                if kw.arg == "choices":
                    for v in _strs(kw.value):
                        S.w("channels", v, f"hv:{unit}:{call.lineno} (--channel)")

    # source apps: the default a write takes
    for unode in _unit(hv, "_write_source"):
        for n in ast.walk(unode):
            if isinstance(n, ast.BoolOp) and _is_str(n.values[-1]):
                S.w("source_apps", n.values[-1].value, f"hv:_write_source:{n.lineno}")

    # envelope: the entry append_journal builds, and what _sign_entry adds
    for unode in _unit(hv, "append_journal"):
        for t, v in _assignments(unode):
            if isinstance(t, ast.Name) and t.id == "entry" and isinstance(v, ast.Dict):
                for k in v.keys:
                    if _is_str(k):
                        S.w("envelope_fields", k.value, f"hv:append_journal:{k.lineno}")
    for unode in _unit(hv, "_sign_entry"):
        for n in ast.walk(unode):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Subscript) and _is_str(t.slice):
                        S.w("envelope_fields", t.slice.value, f"hv:_sign_entry:{n.lineno}")
    for unode in _unit(hv, "_link_payload"):
        for t, v in _assignments(unode):
            if isinstance(v, ast.Dict) and _dict_get(v, "kind") is not None:
                S.w("envelope_fields", "kind", f"hv:_link_payload:{v.lineno}")

    # ── readers ──
    def read_units(accessor, cat, key, tables=None):
        for f, name in sorted(READERS[key]):
            for unode in _unit(trees[f], name):
                for line, v in _compared(unode, accessor, tables):
                    S.r(cat, v, f"{f}:{name}:{line}")

    read_units(_field_accessor("type"), "entry_types", "type")
    read_units(_field_accessor("kind"), "link_kinds", "kind")
    read_units(_field_accessor("action"), "governance_actions", "action")
    read_units(_field_accessor("key"), "config_keys", "key", tables={"_PR6_KNOB_DEFAULTS": pr6})
    for table, cat in (("_PASS1", "entry_types"), ("_LINK_RESOLVERS", "link_kinds"),
                       ("OWNER_APPS", "source_apps"), ("SRC_CLASS_WEIGHT", "source_contexts")):
        for v in _module_table(hv, table) or []:
            S.r(cat, v, f"hv:{table}")
    for fn, pos, cat in (("_projection_latest", 1, "entry_types"), ("_content_unauthorized", 2, "entry_types"),
                         ("_content_unauthorized", 3, "config_keys"), ("_content_authz", 1, "config_keys")):
        for unit, _u, call in _calls(hv, fn):
            if len(call.args) > pos and _is_str(call.args[pos]):
                S.r(cat, call.args[pos].value, f"hv:{unit}:{call.lineno}")

    # channels: every comparison with `_channel(…)`, and `_channel`'s own defaults
    for unit, unode in _units(hv):
        for line, v in _compared(unode, _is_call_to("_channel")):
            S.r("channels", v, f"hv:{unit}:{line}")
    for unode in _unit(hv, "_channel"):
        for n in ast.walk(unode):
            if isinstance(n, ast.BoolOp) and _is_str(n.values[-1]):
                S.r("channels", n.values[-1].value, f"hv:_channel:{n.lineno}")
            if isinstance(n, ast.IfExp) and _is_str(n.orelse):
                S.r("channels", n.orelse.value, f"hv:_channel:{n.lineno}")

    # behaviour tags: `"x" in tl`, `t.startswith("ttl:")`, `re.match(r"ttl:…", t)`
    for f, name in sorted(READERS["tag"]):
        for unode in _unit(trees[f], name):
            for n in ast.walk(unode):
                site = f"{f}:{name}:{getattr(n, 'lineno', 0)}"
                if (isinstance(n, ast.Compare) and _is_str(n.left) and len(n.ops) == 1
                        and isinstance(n.ops[0], (ast.In, ast.NotIn)) and isinstance(n.comparators[0], ast.Name)):
                    S.r("behaviour_tags", n.left.value, site)
                elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.args
                      and _is_str(n.args[0]) and n.func.attr in ("startswith", "match")):
                    prefix = re.match(r"[^\\()\[\]{}.^$*+?|]*", n.args[0].value).group(0)
                    if prefix:
                        S.r("behaviour_tags", prefix + "*", site)

    # sources
    def is_src(n):
        return (isinstance(n, ast.Name) and n.id == "source") or _is_call_to("_write_source")(n)

    for f, name in sorted(READERS["source"]):
        for unode in _unit(trees[f], name):
            for line, v in _compared(unode, is_src):
                S.r("source_apps", v, f"{f}:{name}:{line}")
    for f, name in sorted(READERS["context"]):
        for unode in _unit(trees[f], name):
            for line, v in _compared(unode, lambda n: isinstance(n, ast.Name) and n.id == "ctx"):
                S.r("source_contexts", v, f"{f}:{name}:{line}")
    for unode in _unit(hv, "_parse_source"):
        for n in ast.walk(unode):
            if isinstance(n, ast.BoolOp) and _is_str(n.values[-1]):
                S.r("source_apps", n.values[-1].value, f"hv:_parse_source:{n.lineno}")
        for t, v in _assignments(unode):
            if isinstance(t, ast.Name) and t.id == "ctx" and isinstance(v, ast.IfExp) and _is_str(v.orelse):
                S.r("source_contexts", v.orelse.value, f"hv:_parse_source:{v.lineno}")
    for unit, _u, call in _calls(hv, "get"):
        if len(call.args) == 2 and _is_str(call.args[0]) and call.args[0].value == "source" and _is_str(call.args[1]):
            S.r("source_apps", call.args[1].value, f"hv:{unit}:{call.lineno}")
    return S


def unclassified(sources):
    """[(field, file, function)]: functions that compare an entry's `type`, `kind` or `action` to a literal
    and are neither enumerated readers nor named as not journal vocabulary."""
    out = []
    for f, src in sources.items():
        tree = ast.parse(src)
        for field in ("type", "kind", "action"):
            known = set(READERS[field]) | set(NOT_JOURNAL[field])
            for unit, unode in _units(tree):
                if (f, unit) not in known and _compared(unode, _field_accessor(field)):
                    out.append((field, f, unit))
    return out


# ── the checks, as functions of (sites, registry) so the mutants below can drive them ──────────────

def unregistered_writes(S, registry):
    return [f"{cat}: {name!r} written at {sites[0]} is " +
            ("not registered" if name not in registry[cat] else f"registered `{registry[cat][name]['status']}`")
            for cat in registry for name, sites in sorted(S.writes[cat].items())
            if registry[cat].get(name, {}).get("status") != vocabulary.WRITTEN]


def unregistered_reads(S, registry):
    return [f"{cat}: {name!r} read at {sites[0]} is " +
            ("not registered" if name not in registry[cat] else f"registered `{registry[cat][name]['status']}`")
            for cat in registry for name, sites in sorted(S.reads[cat].items())
            if registry[cat].get(name, {}).get("status") not in READABLE]


def orphans(S, registry):
    out = []
    for cat, table in registry.items():
        for name, rec in sorted(table.items()):
            st = rec["status"]
            if st == vocabulary.WRITTEN and name not in S.writes[cat]:
                out.append(f"{cat}: {name!r} is `written`, but no enumerated site writes it")
            elif st in (vocabulary.LEGACY, vocabulary.READ) and name not in S.reads[cat]:
                out.append(f"{cat}: {name!r} is `{st}`, but no enumerated site reads it")
            elif st == vocabulary.RESERVED and (name in S.writes[cat] or name in S.reads[cat]):
                out.append(f"{cat}: {name!r} is `reserved`, but a site uses it: register it `written`")
    return out


@pytest.fixture(scope="module")
def sites():
    return collect(_sources())


# ── the tripwires ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cat", sorted(REGISTRY))
def test_every_written_name_is_registered_as_written(sites, cat):
    bad = [m for m in unregistered_writes(sites, REGISTRY) if m.startswith(f"{cat}:")]
    assert not bad, "register it in vocabulary.py (then regenerate docs/NAMESPACES.md):\n  " + "\n  ".join(bad)


@pytest.mark.parametrize("cat", sorted(REGISTRY))
def test_every_read_name_is_registered(sites, cat):
    bad = [m for m in unregistered_reads(sites, REGISTRY) if m.startswith(f"{cat}:")]
    assert not bad, "register it in vocabulary.py (then regenerate docs/NAMESPACES.md):\n  " + "\n  ".join(bad)


@pytest.mark.parametrize("cat", sorted(REGISTRY))
def test_every_registry_entry_has_a_site(sites, cat):
    bad = [m for m in orphans(sites, REGISTRY) if m.startswith(f"{cat}:")]
    assert not bad, "the registry lists vocabulary the code does not have:\n  " + "\n  ".join(bad)


def test_every_writer_site_is_enumerated(sites):
    """A writer site whose value is a variable must be one the test enumerates; a new one fails here."""
    assert not sites.unresolved, "\n  ".join(["writer sites the tripwire cannot enumerate:"] + sites.unresolved)


def test_every_journal_comparison_is_classified():
    bad = unclassified(_sources())
    assert not bad, ("functions comparing an entry's field to a literal, not yet classified: add each to READERS "
                     "(journal vocabulary) or NOT_JOURNAL (with the reason):\n  "
                     + "\n  ".join(f"{field}: {f}:{fn}" for field, f, fn in bad))


def test_every_enumerated_reader_still_reads(sites):
    """An enumerated reader that was renamed, or no longer compares anything, shrinks the check silently."""
    trees = {f: ast.parse(src) for f, src in _sources().items()}
    read_at = {site.rsplit(":", 1)[0] for cat in sites.reads.values() for lst in cat.values() for site in lst}
    dead = [f"{key}: {f}:{name} " + ("no longer exists" if not _unit(trees[f], name)
                                    else f"no longer compares an entry's {key} to a literal")
            for key, funcs in READERS.items() for f, name in sorted(funcs) if f"{f}:{name}" not in read_at]
    dead += [f"{f}:{name} (NOT_JOURNAL) no longer exists"
             for d in NOT_JOURNAL.values() for f, name in d if not _unit(trees[f], name)]
    assert not dead, "\n  ".join(["enumerated sites that stopped being sites:"] + dead)


def test_registry_shape():
    seen = set()
    for key, title, what, table in vocabulary.CATEGORIES:
        assert key not in seen and title and what and table, key
        seen.add(key)
        for name, rec in table.items():
            assert set(rec) >= {"status", "since", "meaning"}, (key, name)
            assert rec["status"] in vocabulary.STATUSES, (key, name)
            assert rec["status"] != vocabulary.RESERVED or key == "envelope_fields", \
                f"{key}: {name!r}: only an envelope field may be reserved with no site"
            assert re.fullmatch(r"\d+\.\d+", rec["since"]), (key, name, rec["since"])
            assert rec["meaning"].strip() and "\n" not in rec["meaning"] and "|" not in rec["meaning"], (key, name)
            assert not name.startswith("x-"), f"{key}: {name!r}: `x-` is the module prefix, never a core name"


def test_hv_takes_its_tables_from_the_registry():
    """`_CHANNELS` and `_LINK_EVIDENCE_KINDS` moved into the registry: `hv` derives them, with the same values."""
    tree = ast.parse((PROJECT / "hv").read_text())
    derived = {t.id: ast.unparse(n.value) for n in tree.body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    assert derived["_CHANNELS"] == "vocabulary.CHANNEL_NAMES"
    assert derived["_LINK_EVIDENCE_KINDS"] == "dict(vocabulary.LINK_EVIDENCE_SIDES)"
    assert vocabulary.CHANNEL_NAMES == ("sense", "act", "introspect")
    assert vocabulary.LINK_EVIDENCE_SIDES == {"supports": "pos", "contradicts": "neg", "resolves": "neg"}


def test_the_registry_imports_nothing_and_hv_imports_it():
    """Like `commandmap.py`: a table `hv` can import without its import graph reaching anything new (S2)."""
    tree = ast.parse((PROJECT / "vocabulary.py").read_text())
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    hv = ast.parse((PROJECT / "hv").read_text())
    assert any(isinstance(n, ast.Import) and any(a.name == "vocabulary" for a in n.names) for n in hv.body)


# ── the generated document ──────────────────────────────────────────────────────────────────────────

GENERATOR = PROJECT / "scripts" / "common" / "gen_namespaces.py"
DOC = PROJECT / "docs" / "NAMESPACES.md"


def _generator():
    loader = SourceFileLoader("gen_namespaces", str(GENERATOR))
    spec = importlib.util.spec_from_loader("gen_namespaces", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_the_committed_document_is_the_generators_output():
    assert DOC.read_bytes() == _generator().render().encode(), (
        "docs/NAMESPACES.md differs from vocabulary.py: run `python3 scripts/common/gen_namespaces.py`")


def test_the_generator_is_deterministic(tmp_path):
    outs = []
    for i in range(2):
        out = tmp_path / f"n{i}.md"
        subprocess.run([sys.executable, str(GENERATOR), str(out)], check=True, capture_output=True)
        outs.append(out.read_bytes())
    assert outs[0] == outs[1] == DOC.read_bytes()


def test_the_document_lists_every_registered_name():
    doc = DOC.read_text()
    for _key, title, _what, table in vocabulary.CATEGORIES:
        assert f"## {title}" in doc
        for name in table:
            assert f"| `{name}` |" in doc, name


# ── mutants: the checker must report one unregistered name per category, and an orphaned entry ───────

def _mutated(file, old, new):
    src = _sources()
    assert src[file].count(old) == 1, f"mutant anchor not found once in {file}: {old!r}"
    src[file] = src[file].replace(old, new)
    return collect(src)


def test_mutant_unregistered_entry_type_is_caught():
    S = _mutated("hv", 'entry = append_journal("idea", payload)',
                 'entry = append_journal("idea", payload); append_journal("newtype", {})')
    assert any("'newtype'" in m for m in unregistered_writes(S, REGISTRY))


def test_mutant_unregistered_type_through_the_enumerated_variable_is_caught():
    S = _mutated("hv", 'if t not in ("cell", "comb")', 'if t not in ("cell", "comb", "hook")')
    assert any("'hook'" in m for m in unregistered_writes(S, REGISTRY))


def test_mutant_unregistered_link_kind_is_caught():
    S = _mutated("hv", '_link_payload("extends", [entry["node_id"], entry["seq"]]',
                 '_link_payload("builds-on", [entry["node_id"], entry["seq"]]')
    assert any("'builds-on'" in m for m in unregistered_writes(S, REGISTRY))


def test_mutant_unregistered_link_kind_through_the_evidence_loop_is_caught():
    S = _mutated("hv", 'for flag in ("supports", "contradicts"):',
                 'for flag in ("supports", "contradicts", "refutes"):')
    assert any("'refutes'" in m for m in unregistered_writes(S, REGISTRY))


def test_mutant_unregistered_governance_action_is_caught():
    S = _mutated("hivemind_owner.py", '_append_governance({"action": "heartbeat"})',
                 '_append_governance({"action": "x-new"})')
    assert any("'x-new'" in m for m in unregistered_writes(S, REGISTRY))


def test_mutant_unregistered_group_verb_is_caught():
    S = _mutated("hv", 'group_sub.add_parser("list", help="Show the membership roster")',
                 'group_sub.add_parser("list", help="Show the membership roster")\n'
                 '    group_sub.add_parser("suspend", help="mutant")')
    assert any("'suspend'" in m for m in unregistered_writes(S, REGISTRY))


def test_mutant_unregistered_config_key_is_caught():
    S = _mutated("hivemind_owner.py", '"capsule_putters": str,', '"capsule_putters": str, "x_new_knob": float,')
    assert any("'x_new_knob'" in m for m in unregistered_writes(S, REGISTRY))


def test_mutant_unregistered_read_is_caught():
    S = _mutated("hv", 'elif action == "purge" and p.get("device_id"):',
                 'elif action == "suspend" and p.get("device_id"):\n            pass\n'
                 '        elif action == "purge" and p.get("device_id"):')
    assert any("'suspend'" in m for m in unregistered_reads(S, REGISTRY))


def test_mutant_registry_entry_without_a_site_is_caught(sites):
    registry = {k: dict(v) for k, v in REGISTRY.items()}
    registry["governance_actions"]["x-unused"] = {"status": vocabulary.WRITTEN, "since": "2.0", "meaning": "m"}
    registry["entry_types"]["x-gone"] = {"status": vocabulary.LEGACY, "since": "1.0", "meaning": "m"}
    found = orphans(sites, registry)
    assert any("'x-unused'" in m for m in found) and any("'x-gone'" in m for m in found)


def test_mutant_writer_variable_is_caught():
    S = _mutated("hv", 'entry = append_journal("idea", payload)', 'entry = append_journal(kind_of(payload), payload)')
    assert any("hv:propose" in m for m in S.unresolved)


def test_mutant_new_reader_function_is_caught():
    src = _sources()
    src["hv"] += '\n\ndef _new_projection(entries):\n    return [e for e in entries if e.get("type") == "fact"]\n'
    assert ("type", "hv", "_new_projection") in unclassified(src)


# ── local files and `via` (#150 rule 4 and its local-file list, decision h:a1e3e7cd73) ─────────────────
# Not journal vocabulary, so held to the code by a check of their own: every file literal joined onto a hive
# or key-directory root, or passed to `_effective_key_path`, must be registered in `LOCAL_FILES`, and every
# registered name must have such a site. The same both ways for `via`: the values written into a sighting
# (`_record_peer_candidate(…, via=…)`) or compared when one is read.

_PATH_ROOTS = {"HIVE_HOME", "KEY_DIR", "hv.HIVE_HOME", "hv.KEY_DIR", "hive_home()", "sync_common.hive_home()"}


def _is_via(node):
    """`x["via"]` or `x.get("via")`."""
    if isinstance(node, ast.Subscript) and _is_str(node.slice) and node.slice.value == "via":
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and _is_str(node.args[0]) and node.args[0].value == "via")


def collect_local(sources):
    files, via = {}, {}
    for f, src in sources.items():
        for unit, unode in _units(ast.parse(src)):
            for n in ast.walk(unode):
                site = f"{f}:{unit}:{getattr(n, 'lineno', 0)}"
                if (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) and _is_str(n.right)
                        and ast.unparse(n.left) in _PATH_ROOTS):
                    files.setdefault(n.right.value, []).append(site)
                if isinstance(n, ast.Call) and ast.unparse(n.func).split(".")[-1] == "_effective_key_path" \
                        and n.args and _is_str(n.args[0]):
                    files.setdefault(n.args[0].value, []).append(site)
                if isinstance(n, ast.Call) and ast.unparse(n.func).split(".")[-1] == "_record_peer_candidate":
                    for kw in n.keywords:
                        if kw.arg == "via" and _is_str(kw.value):
                            via.setdefault(kw.value.value, []).append(site)
                if isinstance(n, ast.Compare) and _is_via(n.left):
                    for c in n.comparators:
                        for v in _strs(c):
                            via.setdefault(v, []).append(site)
    return files, via


def local_problems(sources, files_reg=None, via_reg=None):
    files_reg = vocabulary.LOCAL_FILES if files_reg is None else files_reg
    via_reg = vocabulary.VIA_VALUES if via_reg is None else via_reg
    files, via = collect_local(sources)
    out = [f"local file {n!r} at {s[0]} is not registered" for n, s in sorted(files.items()) if n not in files_reg]
    out += [f"local file {n!r} is registered, but no path literal names it" for n in sorted(files_reg) if n not in files]
    out += [f"via {v!r} at {s[0]} is not registered" for v, s in sorted(via.items()) if v not in via_reg]
    out += [f"via {v!r} is registered, but no site writes or compares it" for v in sorted(via_reg) if v not in via]
    return out


def test_every_local_file_and_via_value_matches_the_code():
    bad = local_problems(_sources())
    assert not bad, "register it in vocabulary.py (then regenerate docs/NAMESPACES.md):\n  " + "\n  ".join(bad)


def test_local_registry_shape():
    for table, wheres in ((vocabulary.LOCAL_FILES, {"hive", "keys"}), (vocabulary.VIA_VALUES, {None})):
        for name, rec in table.items():
            assert rec["status"] in (vocabulary.WRITTEN, vocabulary.LEGACY, vocabulary.READ), name
            assert re.fullmatch(r"\d+\.\d+", rec["since"]) and rec["meaning"].strip(), name
            assert "|" not in rec["meaning"] and "\n" not in rec["meaning"], name
            assert rec.get("where") in wheres, name
    assert not set(vocabulary.LOCAL_FILES) & {n for *_, t in vocabulary.CATEGORIES for n in t}, \
        "a local file name must not also be journal vocabulary (#146)"


def test_every_category_says_what_a_module_may_add():
    assert set(vocabulary.MODULE_RULES) == {key for key, *_ in vocabulary.CATEGORIES}
    for key in ("entry_types", "governance_actions", "channels", "envelope_fields"):
        assert vocabulary.MODULE_RULES[key].startswith("No."), key
    for key in ("link_kinds", "config_keys"):
        assert vocabulary.MODULE_RULES[key].startswith("Yes, prefixed."), key
    doc = DOC.read_text()
    assert "**What a module may add**" in doc
    for rule in vocabulary.MODULE_RULES.values():
        assert f"*Modules:* {rule}" in doc


def test_the_document_lists_every_local_file_and_via_value():
    doc = DOC.read_text()
    local = doc[doc.index("## Local files"):]
    for name in list(vocabulary.LOCAL_FILES) + list(vocabulary.VIA_VALUES):
        assert f"| `{name}` |" in local, name


def test_mutant_new_local_file_literal_is_caught():
    src = _sources()
    anchor = 'GENESIS_PIN_PATH = HIVE_HOME / ".genesis-pin"'
    assert src["hv"].count(anchor) == 1
    src["hv"] = src["hv"].replace(anchor, anchor + '\nSCRATCH = HIVE_HOME / ".scratch"')
    assert any("'.scratch'" in m and "not registered" in m for m in local_problems(src))


def test_mutant_local_file_dropped_from_the_registry_is_caught():
    reg = {k: v for k, v in vocabulary.LOCAL_FILES.items() if k != ".genesis-pin"}
    assert any("'.genesis-pin'" in m and "not registered" in m for m in local_problems(_sources(), files_reg=reg))


def test_mutant_new_via_value_is_caught():
    src = _sources()
    anchor = 'hv._record_peer_candidate(device, host, via="outbound")'
    assert src["sync_client.py"].count(anchor) == 1
    src["sync_client.py"] = src["sync_client.py"].replace(
        anchor, anchor + '; hv._record_peer_candidate(device, host, via="inbound")')
    assert any("'inbound'" in m and "not registered" in m for m in local_problems(src))


def test_mutant_registered_via_value_with_no_site_is_caught():
    reg = dict(vocabulary.VIA_VALUES, hint={"status": vocabulary.WRITTEN, "since": "1.26", "meaning": "m"})
    assert any("'hint'" in m and "no site" in m for m in local_problems(_sources(), via_reg=reg))
