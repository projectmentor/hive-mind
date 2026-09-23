# HiveMind × Alberta Plan — integration design (contract 1.18 → 1.19)

Grounded in `main` @ contract 1.18 (`hv`, 6440 lines). Line numbers are approximate. Code blocks are
**sketches to hand to Claude Code**, not drop-in patches.

The one-sentence thesis (from the ChatGPT synthesis, kept):

> If changing the underlying model causes the Hive to forget what it learned, the learning was stored in the wrong place.

The one-sentence constraint (from HiveMind's own code, which the synthesis missed):

> Every "learned" signal is a deterministic projection over signed journal entries. Nothing is a classifier,
> nothing is node-local. (`_is_salient` comment, ~L1568: a learned classifier "would reintroduce a capturable authority".)

---

## 0. What main already has (so we don't rebuild it)

| ChatGPT concept | Already in `hv` | Gap |
|---|---|---|
| Admission salience | `_is_salient` (~L1578) — structural, content-neutral, opt-in via `remember --gate` | Misnamed only |
| Confidence | `_content_evidence` → `_content_confidence` → `_effective_confidence` (~L1353–1437). Keyed by **content string**; pos = identity-weighted assertions, neg = `retract` entries via `retracts_ref`; same-principal `cap_self`; 180-day half-life at query time via `$HIVE_NOW` | Facts only, correctly — decisions are confidence-free by design (§4) |
| Provenance links | Three bespoke pass-2 resolvers: `resolve_entity_fact`, decision `superseded_by`, `resolve_resolves` (`facts.resolves`, 1.11). All reference journal identity `(node_id, seq)` through `journal_index` | Not generic; each link is its own type + resolver |
| Extensible vocabulary | `announce` act (1.18): fixed envelope `{action, kind}`, kind-specific `data`, **unknown kinds accepted and ignored** | Pattern exists for governance only |
| Salience L3 / utility | `facts.importance` (v1 write-only stub, journaled in payload, never read — **gains learned behavior, §7.1; not renamed**), `access_count`/`last_accessed` (telemetry, explicitly NOT confidence, not journaled) | `importance` never projected; `access_count` must stay out of projections |
| Authorization pattern | `_projection_latest(entries, want_type, authz=)` + `_content_authz(gov, policy_key)`; `capsule_putters` / `cell_writers` with `owner|fertile`; advisory doctor checks `capsule-authz`, `cell-authz` | Reuse as-is |
| Decisions | `hv decide --tags`, `rationale`, `superseded_by`; `hv search` returns a `kind` discriminator (fact or decision) | No `informed_by`, no outcome score. Confidence-free is **by design** (§4), not a gap |
| Event bus | Nothing in repo. De-facto event source is the Claude Code shim `scripts/common/hive_dispatch.sh <event>` (1.12) | Stays ephemeral; out of scope here |

### 0.1 Event bus: one bus, three channels, carried in the payload

Ephemeral; not a journal. Events become entries only after admission on some node. **The bus is
not the sync path** — sync moves entries, never events.

| channel | Alberta Plan signal | examples | may produce |
|---|---|---|---|
| `sense` | observation | tool results, human message, file/CI/webhook | facts, outcomes |
| `act` | action | tool call issued, write performed, message sent | decision→action links |
| `introspect` | internal state | hook lifecycle, plan step, idea, retrieval | ideas, decisions |
| *(reward)* | reward | — | not a channel; it is `outcome-of` polarity |

Channel is an **additive payload field** `"channel"` with values `sense` | `act` | `introspect`,
sitting beside `source` — inside the signed payload, syncs everywhere, no wire change (the tags /
`informed_by` precedent). *Corrected 2026-09-19:* an earlier draft put the channel inside the
`source` string, but the `_parse_source` grammar is `<app>:<context_class>/<instance>/<session8>`
— channel and context class are different axes (a subagent can observe; a primary agent can
introspect), so a channel segment would either collide with the `SRC_CLASS_WEIGHT` slot or be
ignored as legacy. Projections read the field directly; no string surgery.

**Default: absent means `sense`** — a fact is an observation unless the writer says otherwise,
and the doc defines an outcome as an ordinary fact, so hand-recorded outcomes (`source: manual`,
no channel — all pre-1.19 entries, all CLI writes) count. Missing-means-not-sense would kill the
loop on the first dogfooded outcome. Declaring `sense` gains a lazy agent nothing (it is the
default); declaring `introspect` is the only move that changes anything and is against the
writer's own evidential interest — honest opt-in. One guard: an `idea` entry defaults to
`introspect` (a hypothesis is internal state by definition); that guard lives in the idea write
path (PR5), not in any parser.

Projection rules that depend on it:
- `_identity_weight` → 0 for `introspect`-channel positive evidence on a fact (reasoning ≠ observation;
  an idea may be *asserted* from `introspect` but cannot corroborate a fact).
- `_decision_evidence` ignores `outcome-of` links whose channel ≠ `sense` (no self-grading).
- `_is_admissible` may take per-channel thresholds (`sense` is floody, `introspect` is structured).
- Trust velocity (§8) is computed per channel: contradicted `sense` claims and drifting `introspect`
  reasoning point at different compromises.

**Scope:** the bus lives inside one hive (one tailnet). It may reach every node in the hive, but
it never crosses between hives. When hives are federated (future), federation is a journal/sync-
layer concern — entries cross, events do not. Whether the bus reaches all nodes in the hive or
stays per-node (`hive_dispatch.sh` today) is left open; because channel lives in the payload, either answer is a transport change only and entries, projections, and the contract are
unaffected.

---

## 1. Rename: `_is_salient` → `_is_admissible`

**Why.** "Salience" is the name of the whole subsystem (§7.1) — the function's own header says
"Salience Layer 2". A function named `_is_salient` that only means "passed layer 2" reads as the
whole thing. This is the **only rename**; no journal field, column, or CLI flag changes.

**Vocabulary**

| Axis | Question | Type | When computed |
|---|---|---|---|
| **Admission** (salience L1/L2) | Is this a knowledge-shaped write at all? | bool | at write, opt-in gate |
| **Confidence** | How strongly is this supported by independent identities? | float | projection, decayed at query |
| **Importance** (salience L3) | How much does this matter *now* to the hive? | float | projection, decayed at query |
| **Utility** | Has this proven useful in decisions with known outcomes? | float | projection, decayed at query |

**Implementation**

```python
def _is_admissible(content):      # renamed body, unchanged
    ...

_is_salient = _is_admissible      # back-compat shim, same pattern as _wire_claude_hooks (1.13)
```
Also rename the `remember --gate` help text and the comment header. No journal/wire change.

---

## 2. One generic `link` entry type

**Why.** You have three link mechanisms with three resolvers, and the ChatGPT ontology wants ~8
relationship kinds that must be *extensible*. Copy the `announce` design: fixed envelope, open `kind`
vocabulary, unknown kinds land in the journal and project to nothing — so a 1.19 node and a 1.20
node never diverge over a kind one of them doesn't know.

**Entry shape**

```json
{
  "node_id": "…", "seq": 412, "type": "link", "timestamp": "…",
  "payload": {
    "kind": "informed",
    "from_ref": ["nodeA", 401],
    "to_ref":   ["nodeB", 77],
    "data": {},
    "source": "claude-code:primary/DESKTOP-EGMBL5A/abcd1234",
    "channel": "sense"
  },
  "prev_hash": "…", "sig": "…", "pub": "…"
}
```

- `from_ref`/`to_ref` are journal identities, exactly like `retracts_ref` and `resolves_ref` today. Never local `#id`s (rebuild-unstable).
- `kind` is a free string on the wire. The *projection* knows a small set.

**Projected kinds for 1.19**

| kind | from → to | Effect in projection |
|---|---|---|
| `supports` | fact/idea → fact/idea | positive evidence on `to` (identity-weighted, like corroboration) |
| `contradicts` | fact/idea → fact/idea | negative evidence on `to` (like `retract`, non-owner weight) |
| `supersedes` | decision → decision | honored on read from 1.19 (write path switches in PR2b) |
| `resolves` | fact → fact | honored on read from 1.19, retract-equivalent (write path switches in PR2b) |
| `entity` | entity → fact | honored on read from 1.19 (write path switches in PR2b) |
| `informed` | decision → fact/idea/decision | utility signal (§3) |
| `outcome-of` | fact → decision | decision outcome_score (§4) |

**Ingest.** NO change. There is no content-type allowlist: Pass 2 of `append_foreign_entries`
gates on signature + admission only (the docstring's type list is descriptive), so `link` from an
admitted device lands today, exactly as cell/comb did in 1.13. The only allowlist is the
governance-ACTION list in Pass 1. `link` is content, not governance — no owner signature required.

**Rebuild.** One pass-2 resolver instead of three:

```python
_LINK_RESOLVERS = {
    "entity":     resolve_entity_fact,     # existing bodies, adapted to read from_ref/to_ref
    "supersedes": resolve_supersedes,
    "resolves":   resolve_resolves,
    "informed":   resolve_informed,        # new: INSERT INTO links(kind, from_id, to_id, …)
    "outcome-of": resolve_informed,
    "supports":   resolve_informed,
    "contradicts":resolve_informed,
}

def resolve_link(conn, entry):
    kind = entry["payload"].get("kind")
    fn = _LINK_RESOLVERS.get(kind)
    if fn is None:
        return                             # unknown kind: lands, projects to nothing (announce rule)
    fn(conn, entry)
```

**store.db** — a generic edge table alongside the existing bespoke columns (keep those; they're the
1.11/1.15 read paths):

```sql
CREATE TABLE IF NOT EXISTS links (
    kind      TEXT NOT NULL,
    from_kind TEXT NOT NULL, from_id INTEGER NOT NULL,
    to_kind   TEXT NOT NULL, to_id   INTEGER NOT NULL,
    signer    TEXT NOT NULL,             -- node_id post-_verify_entry
    created_at TIMESTAMP,
    PRIMARY KEY (kind, from_kind, from_id, to_kind, to_id, signer)
);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_kind, to_id, kind);
```

**Back-compat.** Old `entity_fact` / `retract` / decision-supersede entries keep their existing
resolvers forever (journal is append-only; nothing is migrated). PR2 reads all seven kinds, but
the three existing verbs keep emitting their legacy fields; the write-path switch is a separate
PR (2b) after the fleet is on 1.19, gated by a doctor advisory listing pre-1.19 peers. **Never
dual-emit** — legacy `--resolves` already soft-retracts, and a hard `resolves` link is
retract-equivalent, so dual-emit double-counts negative evidence from one act. The verbs don't
change on the surface either way.

---

## 3. `informed_by` on decisions (the utility signal)

**Why.** Utility = "was this used in a decision, and did the decision work out?" That needs the
decision to name what it used. Today `access_count` counts *reads*, is node-local, and the code
says it must not feed confidence. Reads are cheap and floody; **uses are rare and signed**. Journal
uses, not reads.

**Write path.** Additive payload field (identical precedent: `tags` in 1.15).

```
hv decide "Ship PR2 before fertile-mode timeline work" \
   --rationale "…" --tags hivemind,security \
   --informed 118 402 d17          # local ids; resolved to journal refs at write time
```

Inside `cmd_decide`, each `--informed` id is resolved via `_index_lookup` (as `--resolves` does
today) and the decision payload gets:

```json
"informed_by": [["nodeA",401], ["nodeA",409], ["nodeB",12]]
```

then one `link` entry per ref with `kind: "informed"`, `from_ref: <this decision>`. (Storing it
both in the payload and as links is deliberate: the payload is the human-legible record; the links
are what the projection reads.)

**MCP parity.** `hive_decide` gains an `informed_by: [ids]` arg. Claude Code skill/rubric: "when you
call `hive_decide`, pass the ids of the facts you retrieved and relied on."

**Projection.**

```python
def _utility_evidence(entries, gov):
    """target (node_id,seq) -> {'uses': {decision_ref: weight}, 'last_ts': str}
    weight = identity weight of the decision's signer × outcome_score(decision)   (§4; vindication, not confidence)
    A decision with no outcome yet contributes a small prior (e.g. 0.25), so 'was used' counts
    for something even before 'worked out' is known."""
```

Projected to a new column `facts.utility` (and `ideas.utility`) by the same ONLY-writer discipline
as `_recompute_confidence`. Query-time decay reuses `_effective_confidence` with the same half-life.

---

## 4. Outcomes → decisions get an outcome score (NOT confidence)

*Decided 2026-08-30.* Decisions stay confidence-free, on purpose. "Is this the decision?" is
certain — signed, journaled, current until superseded; corroboration does not apply and only a
supersede by author or owner changes what is decided (human on the loop). "Did it work out?" is
uncertain and learned from outcomes: that is `outcome_score`, a *vindication* signal, a separate
axis. Outcomes inform the human who might supersede; they never demote a decision on their own.
`hv search --min-confidence` keeps dropping decisions (1.15); a bad-outcome decision still
surfaces — it is still the decision.

**Why.** Without an outcome signal there is no learning loop — only a log.

**Shape.** An outcome is *not* a new type. It is an ordinary fact plus an `outcome-of` link with a
signed polarity:

```
hv remember "CI green on PR2; cell-authz doctor check surfaced 0 unhonored cells" \
   --outcome-of d17 --polarity +1
```
emits: the `fact` entry, then a `link` `{kind:"outcome-of", from_ref:<fact>, to_ref:<d17>, data:{"polarity": 1}}`.
The `--outcome-of` write stamps the **same channel on both** the fact and the link; the projection
reads the **link's** (it is the entry asserting "this is an outcome of that decision"), so an agent
cannot pair an `introspect` fact with a `sense` link or the reverse to smuggle a self-graded
outcome past the rule.

Polarity is in `[-1, +1]`; default `+1`. Keep it coarse — three values (−1, 0, +1) is enough and
prevents agents inventing precision.

**Projection** — a deliberate clone of `_content_evidence`, keyed by decision ref instead of content
string:

```python
def _decision_evidence(entries, gov):
    """decision (node_id,seq) -> {'pos': {id: w}, 'neg': {id: w}, 'last_ts': str}"""
    out = {}
    for e in entries:
        if e.get("type") != "link" or e["payload"].get("kind") != "outcome-of":
            continue
        p = e["payload"]
        if p.get("channel", "sense") != "sense":
            continue                       # no self-grading: only observed outcomes count
        ev = out.setdefault(tuple(p["to_ref"]), {"pos": {}, "neg": {}, "last_ts": ""})
        key, w = _identity_weight(e["node_id"], p.get("source", "manual"))
        pol = p.get("data", {}).get("polarity", 1)
        side = "pos" if pol > 0 else "neg"
        ev[side][key] = max(ev[side].get(key, 0.0), w * abs(pol))
        ev["last_ts"] = max(ev["last_ts"], e.get("timestamp", ""))
    return out

def _recompute_decision_outcome(conn, entries):
    gov = _governance_state(entries)
    for ref, ev in _decision_evidence(entries, gov).items():
        did = _index_lookup_by_ref(conn, "decision", ref)
        conn.execute("UPDATE decisions SET outcome_score=?, last_outcome_at=? WHERE id=?",
                     (_content_confidence(ev, gov), ev["last_ts"], did))
```

`_content_confidence` is reused unchanged as the scoring function: governed pos − neg, `cap_self`
when every outcome reporter shares one principal (a device can't vindicate its own decisions).
Decay applies to the outcome *evidence* (fact half-life), never to the decision's standing.
`decisions` gains `outcome_score REAL DEFAULT 0.0, last_outcome_at TIMESTAMP` via the same
ALTER-if-missing idiom used for `facts.resolves`. No `confidence` column on decisions, ever.

### 4.1 Reward: type, interfaces, and where it is applied

Not binary — **ternary at the human/LLM interface, scalar at the schema.** `0` means "an outcome
was observed and it was neutral"; absence of any `outcome-of` link means "no outcome yet." These
are different states and both are honest.

| | Alberta Plan | HiveMind |
|---|---|---|
| Type | real scalar | numeric in [−1, +1] |
| When | every time step | per decision, when an outcome is recorded |
| Aggregation | summed over time (the return) | all outcomes for a decision, identity-weighted, decayed |
| Who emits it | the environment | a `sense`-channel observer, signed |

Sparse and per-decision rather than dense and per-step: the decision, not the time step, is the
unit of action here.

**Data type.** `data.polarity` is numeric; the projection computes `w × |polarity|`, so any float
in [−1, +1] works today. The ternary restriction {−1, 0, +1} is a **CLI/MCP convention**, not a
schema constraint. Two interfaces onto one field, no contract change to add the second:
- humans and LLMs: {−1, 0, +1} via `--polarity`
- RL / Oak-type agents (future): float in [−1, +1] on the `act` → outcome path

**Why the human/LLM side stays coarse.** Magnitude comes from *how many independent identities*
reported outcomes, not from one agent's claimed intensity — the `cap_self` reasoning. Three values
plus identity weighting yield a richer post-projection signal than one agent's "0.83", and one
nobody can inflate alone.

**Applied in 1.19**
1. `decisions.outcome_score` — pos − neg through `_content_confidence` (vindication, §4)
2. utility of every fact/idea in the decision's `informed_by` — credit flows back along links (§3)
3. trust velocity — per-signer mean of decision outcomes, short vs. long window (§8)

**Cheap follow-ons (not 1.19)**
4. `doctor` advisory: strongly negative outcome + no supersede → "revisit" (outcomes nudge the
   human on the loop; never demote)
5. boot summary surfaces recent negative outcomes so the agent sees what went wrong last session
6. cell/comb utility — which tools and configs sit upstream of good outcomes

**Retroactive.** Old decisions have no outcomes → outcome_score 0, which is honest. THREAT_MODEL
sign-offs can be re-recorded as decisions with `--informed` pointing at the limitation facts; that
is the first real "we decided to accept this" vs "how it works" split you asked for.

---

## 5. Links are evidence, not commands (the authorization rule)

**Why.** `_projection_latest` taught the lesson: signature ≠ authorization. A `contradicts` or
`supersedes` link from a compromised admitted device is the new denial-of-knowledge surface. But
adding a `link_writers=owner|fertile` knob would make fertile hives useless (every agent needs to
link). Instead: in fertile hives a link never *hides* anything, it *weighs*.

**Rule**

| kind | payload owner-signed by owner-at-T (`owner_sig`, via `owner_timeline`) | signer is target's author | any other admitted device |
|---|---|---|---|
| `supersedes` | hard: `superseded_by` set | hard: `superseded_by` set | **downgraded to `contradicts`** (evidence only) |
| `resolves` | hard soft-retract | hard soft-retract | downgraded to `contradicts` |
| `contradicts` / `supports` | evidence, owner weight | evidence | evidence, `cap_self` applies |
| `informed` / `outcome-of` / `entity` | evidence | evidence | evidence |

```python
def _link_authority(entry, target_entry, gov):
    signer = entry["node_id"]                       # post-_verify_entry
    pos = (entry["timestamp"], entry["node_id"], entry["seq"])
    if _is_authorized_writer(signer, pos, entry["payload"], gov, "owner"):
        return "hard"        # owner-key signature ON THE PAYLOAD, owner-at-T via owner_timeline
    if signer == target_entry["node_id"]:
        return "hard"        # self-correction is always allowed (device-level, like retract)
    return "evidence"
```

Corrected 2026-09-19: an earlier draft compared the signer's *principal* to the owner id — the
exact device-key-as-owner-key shortcut 1.16 closed. A device key is never the owner key: if any
device in the owner's principal were compromised, every `supersedes` it emitted would become hard.
Hard authority requires **owner-key possession proven by `owner_sig` on the payload**, checked by
the existing `_is_authorized_writer` under the `owner` policy — no new authority code. On the
owner's machine, `hv decide --supersedes` and `hv remember --resolves` co-sign with the owner key
when present, exactly as `hv capsule put` and `hv wire --add` do today.

**Awkward case, by design:** the owner writing a `supersedes` from a device without the owner key
loaded produces a device-signed link → not the target's author → downgrades to evidence. Nothing
is lost: the `link-authz` doctor check lists it and the owner re-issues it owner-signed — the same
ratification flow as the other downgrade cases.

Convergence: the link lands in every journal; every node applies the same downgrade; a downgraded
link still moves confidence, so a real correction from a peer is not silenced — it just can't
unilaterally erase.

**Doctor.** Advisory `link-authz` check listing downgraded `supersedes`/`resolves` links, beside
`capsule-authz` and `cell-authz`, so the owner can ratify by re-issuing them owner-signed.

---

## 6. `idea` — fourth `_PASS1` type

**Why.** An idea is a hypothesis whose confidence is *earned* through `supports`/`contradicts`/
`outcome-of` links, not asserted. Modelling it as a fact would let the corroboration projection treat a
restated hypothesis as evidence (two agents repeating the same guess ≠ two observations).

**Differences from `fact`**

- Corroboration-by-identical-content is **off** for ideas. Confidence comes only from links.
- Initial confidence: 0.0 (not the fact default). `hv search --min-confidence` therefore hides raw
  ideas unless asked for `--kind idea`.
- `hv search --format json` `kind` discriminator gains `idea`.

```python
_PASS1 = {"fact": persist_fact, "decision": persist_decision,
          "entity": persist_entity, "idea": persist_idea}
```
`ideas` table = `facts` minus `importance`/`access_count`, plus `utility`. CLI: `hv propose "…"
--tags …`; MCP: `hive_propose`.

**Ideas: propagation, attention, and grounding**

An idea nobody sees is a hypothesis nobody can support or contradict — it dies at 0.0. It needs
*attention*, not announcement. The `announce` act (1.18) is the wrong vehicle: governance-typed,
projection-invisible, exists only to carry a device key.

1. **Propagation is free.** `idea` is content; sync carries it to every node like a fact.
2. **Notification on arrival.** When ingest lands a peer's `idea`, emit a local bus event on the
   `introspect` channel (internal state arriving, not an observation of the world). Agents on that
   node see "new open hypothesis from nodeB" in the same stream as everything else. This is also
   why the bus need not cross nodes (§0.1 deferral): sync + ingest-emit gives the same effect.
3. **Surfacing.** Boot summary gains an "open ideas" section — not superseded, confidence below
   threshold, capped at a few, newest or most-linked first. The standing invitation to weigh in.

**Grounding rule (required).** Ideas earn confidence from `sense`-grounded evidence, not
`introspect` chatter:
- `supports` / `contradicts` from an entry whose `channel` is `sense` (or absent — the default) → full identity weight
- from an entry whose `channel` is `introspect` — declared, or an `idea`'s write-path default → weight 0
  (`cfg["introspect_support_weight"]`, default 0; raise deliberately if ever)
Otherwise two LLM sessions can talk an idea into high confidence with no observation ever touching
it — the "recursive self-reflection manufactures knowledge" failure the thesis names. Same rule
already applies to facts (§0.1); this extends it to ideas explicitly.

Alberta-Plan mapping: an idea is a candidate *feature*; §7's utility projection is Oak's "continually
assess and replace" applied to ideas instead of neural features.

---

## 7. Salience subsystem (three layers) and utility

### 7.1 Salience is a layered subsystem, not a field

*Decided 2026-08-30.* "Salience" names the whole "does this matter?" pipeline. The code already
says so — the header above `_is_salient` reads "Salience Layer 2 (structural gate)". Made explicit:

```
L1  agent rubric (Hermes hook / Claude Code skill)      ─┐  ADMISSION: does this enter?
L2  _is_admissible in hv core, before append_journal    ─┘  (binary, at write time)
        ↓
    JOURNAL  (append-only, synced; not a layer, not a judge)
        ↓
L3  importance — how much it matters, learned over time     (continuous, projection over the journal)
```

Layers are **sequential, not summed**. L1/L2 decide what enters; L3 only exists for what got in.
Admission is a precondition of importance, never a term in it.

**Layer definitions**

| | L1 — agent rubric | L2 — structural gate | L3 — importance |
|---|---|---|---|
| **What it is** | The agent's own judgment of whether something is worth writing at all | A content-neutral check that a write is knowledge-shaped | A learned, continuous measure of how much an admitted entry matters now |
| **Where it runs** | Inside the agent / adapter, before any `hv` call. Code anchors: Hermes `_salience_gate` (`return True  # MVP`), the Claude Code skill rubric | In `hv` core: `_is_admissible`, called from `cmd_remember` when `--gate` is set, before `append_journal` | In the rebuild projection, after the journal; writes `facts.importance` / `ideas.importance` |
| **Who owns it** | The agent author. Each agent may differ. | The hive. Shared core, so every agent gates identically. | The hive. Deterministic over the journal. |
| **Input** | Whatever the agent has: conversation, tool results, its own reasoning | The content string only | Journal entries: the asserted value, `link` in-degree by other identities, tags, timestamps |
| **Output** | write / don't write | admit / reject (bool) | float in [0, 1], decayed at query |
| **Decides by** | structure of the *situation*: decision + rationale, correction, outcome, constraint, new entity, first-hand tool result → write; intermediate reasoning, restatement, pleasantry → don't | structure of the *text*: length, bare question, greeting | evidence from experience |
| **May never** | rate importance or topic (it is a form filter) | look at topic, meaning, or importance; be learned; hold state | be a classifier; read node-local state (`access_count`); exceed `importance_self_cap` on self-assertion alone |
| **Failure it prevents** | the agent journaling its own chatter | an agent's L1 being missing or lax (Hermes L1 is a pass-through today) | importance becoming a capturable authority |
| **Journal effect** | nothing written | nothing written | nothing written — projection only |

Reading the table top to bottom: L1 is *should I say this*, L2 is *is this even a statement*, L3
is *did it turn out to matter*. L1 and L2 are both admission and both structural; they differ in
who owns them and what they can see. A write that passes L1 but not L2 is rejected; a write that
skips `--gate` bypasses L2 entirely (explicit writes are intentional — the 1.x design).

**Closed 2026-09-19 — `async-inventing-shore.md` reconciled from the docstring.** The file (a
transient Claude Code plan on DESKTOP-EGMBL5A) was gone by 2026-09-19, but the Hermes
`_salience_gate` docstring preserved its substance: the pass list (decisions+rationale,
corrections, outcomes, constraints, new entities, first-hand tool results) and fail list
(intermediate reasoning, restatements, speculation, pleasantries) are the **L1 rubric** — they
judge the structure of the situation, not the text — and the deferred "surprise/consequence
weighting" and "earn-your-keep promotion" are **L3** importance and utility learned from links
and outcomes. The old five layers numbered by pipeline position; this table numbers by who owns
the judgment. Nothing was a fourth kind of thing; it nests without renumbering. The docstring is
rewritten in PR1 to say "Salience L1 — agent rubric (§7.1)", keeping the pass/fail lists as the
rubric text and dropping the dead file reference.

**Renames: one function, zero journal structure.**
- `_is_salient` → `_is_admissible`, old name kept as a shim (the `_wire_claude_hooks` pattern).
  Renamed only because a function named "is salient" that means "passed L2" reads as the whole
  subsystem.
- `importance` keeps its name everywhere: payload field, `facts.importance` column,
  `hv remember --importance`, MCP arg. It has been journaled since v1 and has always synced.
- Hermes `return True  # MVP` hook is L1 and unchanged.

**`importance` gains learned behavior.** It is a v1 write-only stub (journaled, persisted, never
read — `CLI_REFERENCE.md` says so). Now, exactly like confidence: the *journal* holds what the
agent asserted at write time; the *column* is written only by the rebuild projection, starting from
that asserted value and moved by experience.

**Self-assertion cap** (`cap_self` applied to L3): the journaled value alone can never hold the
column above `importance_self_cap` (default 0.3). An agent may flag "this seems big"; only links
from *other* identities can make it stay big. This is the Bitter-Lesson hedge — domain hints are
admitted but cannot beat evidence.

### 7.2 Projections

Pure functions of journal + governance + `$HIVE_NOW`, like confidence. Never read `access_count`.

```python
def _importance(entry_ref, asserted, links_in, tags, cfg, now):
    prior = min(asserted, cfg["importance_self_cap"])                  # 7.1 cap
    recent = sum(_identity_weight_of(l) * (0.5 ** (_age_days(l.ts, now) / HALFLIFE_DAYS))
                 for l in links_in if l.signer_principal != entry_ref.principal)   # other identities only
    volatile = 1.0 if "volatile" in tags else 0.0                      # 1.2 auto-tag ⇒ re-check
    return min(1.0, prior + cfg["w_links"] * _squash(recent) + cfg["w_volatile"] * volatile)

def _utility(entry_ref, informed_links, decision_outcome, now):
    return _squash(sum(_identity_weight_of(l) * (0.25 + 0.75 * max(0.0, decision_outcome[l.from_ref]))
                       * (0.5 ** (_age_days(l.ts, now) / HALFLIFE_DAYS))
                       for l in informed_links))
```

Knobs (`hv config confidence set …`, namespace exists since 1.5; journaled as governance so every
node projects identically): `importance_self_cap` 0.3, `w_links` 0.6, `w_volatile` 0.1.

`facts.importance` / `ideas.importance` / `facts.utility` are written **only** by the rebuild
projection — same single-writer discipline as `facts.confidence`. Decay at query time,
`_effective_confidence`-style.

**What "ceasing to matter" looks like.** No deletion, no tombstone: an entry nobody links to for
two half-lives decays to its capped prior at most and drops below the boot-summary threshold. Still
in the journal, still searchable, resurrectable by one new link.

### 7.3 How decay maps to the Alberta Plan / Oak — and where we diverge on purpose

Decay in HiveMind is **projection-only**; the journal never loses anything.

| HiveMind | Alberta Plan / Oak | Fit |
|---|---|---|
| Confidence half-life (180d) at query time via `$HIVE_NOW` | Step 1 tracking estimates µ_t, σ_t are exponentially weighted — a half-life. Temporal uniformity: same rule every rebuild, no consolidation phase | Direct analogue |
| `volatile` auto-tag → re-check, don't trust | "Increase speed of learning when things start to change" | Partial: ours is structural (claim form), theirs is learned |
| L3 importance decays toward its capped prior; utility decays with age of `informed` links | Oak: elements not useful in planning are reranked, removed, replaced | Analogue, with the substitution below |
| Owner-forget (`FORGET_FLOOR`, never decays) | None — a governance act, outside the learning loop | Deliberate |
| Credit flows back along explicit `informed_by` links | Eligibility traces (fading credit-assignment memory) | **Deliberate divergence** — see below |
| One global `HALFLIFE_DAYS` | Step 1 per-feature step-size meta-learning (IDBD/Autostep) | **Deliberate divergence** — see below |

**The substitution.** Oak deletes to free a finite resource (the feature budget). HiveMind's finite
resource is not storage — append is cheap — but the agent's *attention*: boot summary, retrieval
results, context window. "Removed and replaced" ⇒ "drops below the retrieval threshold and
something with fresher evidence takes the slot." Same pressure, reversible.

**Two divergences, decided 2026-08-30, not gaps:**
1. **No per-fact learned half-life in 1.19.** The IDBD analogue (below, D-2) is not expensive to
   build — it is a projection — but it would be adapting on nothing: a typical fact today has one
   assertion and at most one retraction, and IDBD-style adaptation needs evidence density.
   Instead: **half-life per class, set by config, not learned** — `volatile` 14d, fact 180d,
   idea 90d. Decisions have no half-life: their standing is a supersede chain, not evidence;
   their outcome evidence decays under the fact half-life (knobs under `hv config confidence`, journaled as governance).
   Deterministic, no per-fact state, gives "volatile decays faster" immediately.
2. **No approximate credit assignment.** Eligibility traces exist because an RL agent lacks
   provenance. We have `informed_by` links — outcomes credit exactly the entries that informed the
   decision, no temporal smearing. Keep it explicit.

**Prerequisite done now, so D-2 stays a pure projection addition later:** `_content_evidence` keeps only
max-weight-per-identity and `last_ts`; it discards evidence *order*. When it is cloned for
decisions (§4), both projections retain the ordered evidence sequence `[(ts, sign, identity), …]` per
target. Zero cost now; it is the only prerequisite the IDBD analogue has, and it is built once
instead of retrofitted into two projections.

Mapping of the layers themselves: L1/L2 admission is a *justified* divergence from Oak's
admit-then-discard (a G-Set cannot discard, so the gate must sit before persistence); L3 importance
is Step 1's "learn which of the fixed features are relevant" — the same fixed-representation,
learned-relevance shape; utility is Oak's "never useful in planning ⇒ replaced", with planning =
decisions that had outcomes.

---

## 8. Trust velocity (last; advisory only)

**Why.** Per-signer reliability is a *derived* view of the same evidence: how often a device's facts
get `retract`ed/`contradicts`, how its decisions' outcomes score. Change-over-time is the security
signal, not the level.

```python
def _signer_reliability(entries, gov, window_days, now):
    # per node_id: (asserted, retracted-or-contradicted, decision_outcome_mean) within window
def _trust_velocity(entries, gov, now):
    long = _signer_reliability(entries, gov, 180, now)
    short = _signer_reliability(entries, gov, 14, now)
    return {n: short[n] - long[n] for n in long}
```

Ship as `hv doctor` advisory `trust-drift` (thresholds configurable) and a `hv peers` column. Do
**not** wire it into admission, purge, or link authority in 1.19 — collect the signal for a release
first; the ChatGPT NORMAL→CAUTION→RESTRICTED states are a 1.2x decision once you've seen real drift.

---

## 9. Contract, migration, tests

**Contract 1.19** (additive, wire-compatible):
- New content type `link` (no ingest change — content is gated by admission, not type; one pass-2 resolver, unknown kinds ignored)
- New content type `idea`
- Additive decision payload `informed_by`; additive columns `decisions.outcome_score`,
  `decisions.last_outcome_at`, `facts.utility`; `facts.importance` becomes projection-written. Decisions
  remain confidence-free
- Per-class half-life knobs (`volatile` 14d, fact 180d, idea 90d); `HALFLIFE_DAYS` becomes the `fact` default
- New doctor checks: `link-authz`, `trust-drift`
- Version skew: NONE for PR1–7. A 1.18 node accepts and persists `link`/`idea` (valid signature,
  admitted device), its projections ignore the unknown types (`_PASS1.get`), and Merkle stays
  converged — no `diverged`, no upgrade ordering. (Better than `announce`, which needed its
  governance-action allowlist entry.) The only skew window in this feature set is the write-path
  switch (PR2b) — a 1.18 node would not honor a supersede/resolve emitted as a `link` (converged
  journals, divergent views) — which is why it is sequenced separately and gated on the fleet.

**Tests** (mirroring `tests/test_governance.py`, `test_succession.py` style):
- `test_links.py`: unknown kind lands + projects to nothing; `supersedes` from non-author non-owner
  downgrades; owner-at-T authority survives a transfer (reuse `owner_timeline` fixtures)
- `test_outcomes.py`: decision outcome_score = clone of the fact-confidence cases (single principal
  capped by `cap_self`; owner-forget floor not applicable); a negative outcome never changes
  `superseded_by`; `--min-confidence` still excludes decisions
- `test_utility.py`: `informed` weight rises with decision outcome_score; determinism under `$HIVE_NOW`
  across two rebuilt nodes (byte-identical `store.db` columns)
- Differential: rebuild the same journal on two nodes, diff `links`, `facts.utility`,
  `decisions.outcome_score` — must be identical (this is the convergence guarantee)

**Sequence** (each its own PR, each independently shippable):
1. rename `_is_admissible` + shim
2. `link` type + generic resolver + `links` table + `supports`/`contradicts` wired into
   `_content_evidence`, hard `resolves` retract-equivalent (old resolvers untouched; legacy
   verbs keep their write paths)
2b. write-path switch for `supersedes`/`resolves`/`entity` → `link`, gated on a fleet-on-1.19
   doctor advisory
3. `hv decide --informed` / `hive_decide(informed_by)` → `informed` links
4. `--outcome-of` + `_decision_evidence` → `decisions.outcome_score` (both evidence projections retain ordered `(ts, sign, identity)`)
5. `idea` type
6. importance (salience L3) + utility projections + per-class half-life knobs (§7)
7. `link-authz` and `trust-drift` doctor checks

Steps 1–4 already give you the full loop (fact → decision → outcome → outcome_score → utility) with nothing
learned stored in any model.

---

## 10. Deferred initiatives (decisions, not mechanisms)

**D-1 — Gateway-mediated secrets via Tailscale Aperture / PAM.** *Decided 2026-08-30: separate
initiative, sequenced after the event bus (§0.1) is running. Not part of contract 1.19.*

- **What.** Optional cell `kind: gateway` (config, not capsule) pointing a tool at an Aperture
  (LLM/MCP/API gateway) or PAM (SSH/DB/K8s/S3) service so the agent never holds the secret.
  Aperture request/response hooks become a `sense`-channel event source once the bus exists.
- **Why worth it.** For any secret that can be gateway-mediated, THREAT_MODEL Limitations #1
  (forward-only revocation) and #2 (version currency under partition) disappear: revocation is a
  gateway policy edit, immediate, no upstream rotation, no stale copy anywhere.
- **Why not a replacement.** Capsules distribute secrets to sovereign devices — journal-resident,
  encrypted to device keys, governed by `owner_timeline`, offline and off-tailnet. Gateways
  eliminate distribution but require the Tailscale control plane and IdP as trust root. HiveMind's
  trust root stays the owner key; Tailscale stays transport + optional gateway. Dropping capsules
  would not shed crypto maintenance (ed25519 signs every entry; ChaCha20-Poly1305 seals owner
  escrow).
- **Why deferred.** No 1.19 work depends on it; the hook-as-sense-source piece depends on the bus;
  PAM is beta/waitlist; and it is a THREAT_MODEL-level acceptance (external control-plane
  dependency, LLM traffic transiting the proxy) that deserves its own sign-off.
- **Open before starting.** Confirm whether Aperture can be self-hosted. Do not map tailnet
  identity onto HiveMind device identity — fingerprint binding remains the source of truth.
- **Action now.** One paragraph in THREAT_MODEL under Limitations #1/#2 recording this as a
  deferred optional mitigation, tagged as a *decision*.

**D-2 — Per-fact adaptive half-life (the IDBD analogue).** *Decided 2026-08-30: deferred.
Trigger: evidence density — enable when `link` and outcomes have run long enough that a typical
fact carries several evidence events.*

- **What IDBD is.** Incremental Delta-Bar-Delta (Sutton 1992; Autostep is the normalized form):
  one step size per weight, adjusted from that weight's own correction history — consecutive
  corrections in the same direction ⇒ increase (learning too slowly); alternating ⇒ decrease
  (overshooting or noise); no movement ⇒ near zero.
- **The analogue.** weight → fact; step size → half-life; correction → evidence sign
  (corroborate/support = +, contradict/retract = −). Consistently agreeing evidence ⇒ stable ⇒
  longer half-life. Alternating evidence ⇒ contested/volatile ⇒ shorter. Consistently negative is
  not a decay question — confidence already handles it.
- **Sketch.** `halflife_f = clamp(base_for_class × g(agreement_f), lo, hi)` where `agreement_f`
  is the decayed running agreement of successive evidence signs for that fact. Pure projection,
  deterministic under `$HIVE_NOW`, one added column, no journal change. Does not apply to decisions (no
  confidence; standing is by supersede).
- **Why deferred.** Not cost — data. Adapting on one or two evidence events per fact is a rule
  fitted to noise. Per-class half-lives (§7.3) deliver the visible behavior now.
- **Prerequisite (done in 1.19).** Ordered evidence sequence retained in `_content_evidence` and
  its decision clone.
- **Not in scope, ever.** Learned per-fact adaptation is the ceiling; nothing beyond this
  (no gradient, no meta-step tuning of a reasoning engine).

---

## 11–12. Omitted from the public copy

Sections 11 (positioning) and 12 (site copy) are maintained privately. Section numbers are kept
so that references to §13 and earlier stay valid.

---

## 13. Handoff to Claude Code

**Scope.** Implement §1–9 (the eight PRs in §9). §10 is context,
not tasks. Base branch: `main` @ contract 1.18 (`f2f9e96`).

**Repos.**
- Code: `github.com/projectmentor/hive-mind` (public, AGPL)

**Process: plans before code, except PR1.** CC opens one issue per PR below as a written plan
— scope, files touched, the exact names it chooses for anything left to its judgment, tests it
will write, contract impact, and what it will *not* do. David reviews the plan; then CC codes.
Each approved plan is recorded with `hv decide --tags hivemind,design,1.19`. PR1 is exempt:
it is small and serves to validate that CC has the house conventions right (issue refs, test
style, and observing the bot's re-sign on merge rather than committing one) before anything
with a design surface lands.

**The PRs**

| PR | Title | Spec | Adds | Touches | Contract | Tests | Doctor | Blocked by |
|---|---|---|---|---|---|---|---|---|
| 1 | Rename `_is_salient` → `_is_admissible` | §1, §7.1 | shim, help text, comment header; Hermes `_salience_gate` docstring → "Salience L1 — agent rubric (§7.1)" | `hv`, `integrations/hermes/__init__.py` | none | existing suite | — | — |
| 2 | Generic `link` entry type (read side complete) | §2, §5 | `link` type; `resolve_link` + `_LINK_RESOLVERS`; `links` table; `_link_authority` downgrade rule; `supports`/`contradicts` wired into `_content_evidence` (identity-weighted ±); hard `resolves` link retract-equivalent; resolver honours `supersedes`/`resolves`/`entity` links on read; legacy verbs unchanged | `hv` (pass-2, schema, `_content_evidence`; NO ingest change, NO write-path change), `merkle.py` untouched | **1.19** | `test_links.py`: unknown kind lands/projects nothing; non-author non-owner `supersedes` downgrades AND lowers target confidence as `contradicts`; owner-at-T survives transfer; two-node differential | `link-authz` (advisory) | 1 |
| 2b | Write-path switch: `--supersedes`/`--resolves`/entity emit `link` | §2 | verbs emit `link` instead of legacy fields (never dual-emit); fleet-contract doctor advisory | `hv` (write paths) | none | legacy verb output byte-compat until switch; post-switch no dual-emit; downgrade flow end-to-end | fleet-on-1.19 advisory | 2 + fleet on 1.19 |
| 3 | `informed_by` on decisions | §3 | `--informed` on `hv decide`; `informed_by` payload field; `informed` links; `hive_decide(informed_by)` | `hv`, `integrations/mcp/hive_mcp.py`, Claude Code skill rubric | none (additive payload) | payload round-trip; links emitted per ref; pre-1.19 decisions read back empty | — | 2 |
| 4 | Outcomes → `outcome_score` | §4, §4.1 | `--outcome-of`/`--polarity` on `hv remember`; `_decision_evidence`; `_recompute_decision_outcome`; `decisions.outcome_score`, `last_outcome_at`; `_content_evidence` and clone retain ordered `(ts, sign, identity)`; `sense`-only rule for `outcome-of` | `hv` (projection, schema, CLI), MCP parity | none | `test_outcomes.py`: `cap_self` on single principal; negative outcome never touches `superseded_by`; `--min-confidence` still excludes decisions; `channel: introspect` outcome ignored; no-channel outcome counts; differential | — | 2 |
| 5 | `idea` entry type | §6 | `idea` in `_PASS1`; `ideas` table; `hv propose` / `hive_propose`; search `kind: idea`; corroboration-by-content off; grounding rule (`introspect` support weight 0); ingest-emit bus event; boot-summary "open ideas" | `hv`, MCP, boot summary, `hive_dispatch.sh` | **1.20** | idea starts at 0.0; restated idea does not corroborate; `channel: sense` support raises, `channel: introspect` does not; hidden from default search | — | 2 |
| 6 | Importance (L3) + utility projections | §7 | `_importance`, `_utility`; `facts.importance` projection-written; `facts.utility`, `ideas.importance`, `ideas.utility`; `importance_self_cap`, `w_links`, `w_volatile`, per-class half-life knobs under `hv config confidence`; `HALFLIFE_DAYS` becomes `fact` default; query-time decay | `hv` (projection, config, search ranking) | none | `test_utility.py`: self-assertion capped; other-identity links raise; `informed` weight rises with `outcome_score`; per-class half-life; determinism under `$HIVE_NOW`; differential | — | 3, 4, 5 |
| 7 | Trust velocity | §8 | `_signer_reliability`, `_trust_velocity`; `hv peers` column | `hv` (doctor, peers) | none | window math; no effect on admission/purge/link authority | `trust-drift` (advisory) | 4 |

Sequence note: PR2 is the keystone — 3, 4, 5 branch from it and can proceed in parallel once it
merges. PR6 waits for all three. PR7 only needs 4. PR2b is independent of 3–7 and waits on the
fleet, not the code.

**Verify before writing.** Line refs in this doc are approximate — re-grep. Confirm PR1/PR2 are the
merged 1.16/1.17 (they are). Confirm nothing landed on `main` after `f2f9e96`.

**House conventions.** The `CONTRACT_VERSION` changelog-comment style (one dense line per version,
newest first); the advisory `doctor` check pattern (`capsule-authz`, `cell-authz`); test style from
`tests/test_governance.py` and `tests/test_succession.py`; `hive #NN` issue refs in commit
messages — open an issue per PR first. **`hive #NN` means a GitHub issue in the public repo,
never a hive fact/decision id** — those are node-local row numbers, reassigned on every rebuild
(the 1.16/1.17 commit bodies citing `hive #58/#60/#61` were decision ids on the author's machine
at the time and are unresolvable now; that precedent is the bug, not the convention). Cite a hive
decision in a commit by tag + a few words of content (`hv search` finds both since 1.15); once
PR3 lands, `informed_by` makes the stable journal identity `(node_id, seq)` the first-class
reference.

**Manifest re-sign is the bot's job, never yours.** `chore: re-sign source manifest [skip ci]` is
committed by `.github/workflows/sign.yml` on every push to `main` that touches code, after the
full suite passes, using the release key in repo secrets (`HIVE_SIGNING_KEY`). PRs never include a
re-sign commit — you don't have the key, and the bot would overwrite it. Practical consequences:
(a) after each merge, ~12 min until the bot's commit lands; `hv verify` on a freshly pulled node
reports unverified in that window; branch each new PR from the post-re-sign commit so the base
carries the fresh manifest. (b) "main @ contract 1.19" will mean the bot's re-sign commit after
PR2 merges, the same way `f2f9e96` is the re-sign after the 1.18 merge. (This is also why branch
protection on `main` has no required status check — the bot's push would be blocked.)

**Contract bumps.** Nothing in this feature set changes the wire (no ingest change — content is
gated by admission, not type). House convention bumps `CONTRACT_VERSION` for new journal types and
projection-semantics changes anyway (1.13, 1.15–1.17 all bumped with "no wire change"): PR2
(`link`) → 1.19; PR5 (`idea`) → 1.20, each with a changelog comment stating "no wire change, no
version skew." Rename, `informed_by`, outcomes, projections, doctor checks ride under whatever
contract is current.

**Left to CC's judgment, within this doc's constraints.** `_squash` shape; boot-summary "open
ideas" cap; exact CLI flag and MCP arg names (`--informed`, `--outcome-of`, `--polarity`,
`hv propose`, `hive_propose` are suggestions, not mandates); doctor check names; `links` table
index choices.

**Hard constraints — never.**
- No learned classifier anywhere (L2 stays content-neutral; L3 is a projection).
- No node-local state in any projection (`access_count` stays out).
- No journal field renames (`importance` keeps its name; only `_is_salient` → `_is_admissible`).
- No `confidence` column on decisions (§4).
- No migration or rewrite of existing journal entries; old resolvers stay.
- No `link` hides a target unless its payload carries a valid owner-at-T `owner_sig` or its
  signer is the target's author (§5). A device key is never the owner key.
- Never dual-emit: one act writes its legacy field OR a `link`, never both (§2) — dual-emit
  double-counts evidence.
- No `supports`/`contradicts` weight from entries whose `channel` is `introspect` (§6), default 0.

**Definition of done, per PR.**
- Tests pass, including a differential rebuild on two nodes with byte-identical projected columns.
- New advisory `doctor` check where §5/§8 call for one.
- `docs/CLI_REFERENCE.md`, `docs/AGENT_INTEGRATION.md`, `docs/INTERNALS.md` updated.
- `CONTRACT_VERSION` changelog comment added when the contract bumps (PR2 → 1.19, PR5 → 1.20;
  each notes "no wire change, no version skew").
- `THREAT_MODEL.md` updated where §5 touches authorization.
- MCP parity (`integrations/mcp/hive_mcp.py`) for any new CLI verb or arg.

**Side tasks.**
- D-1 paragraph into `THREAT_MODEL.md` under Limitations #1/#2 (§10), tagged as a decision.
- In PR1 (it establishes the L1/L2/L3 vocabulary in code): rewrite the Hermes `_salience_gate`
  docstring — "Salience L1 — agent rubric (§7.1)", pass/fail lists kept as the rubric text, dead
  `async-inventing-shore.md` reference dropped. Reconciliation closed 2026-09-19 (§7.1).

**Dogfood (starts now).** Record the 2026-08-30 decisions in the hive with
`hv decide --tags hivemind,design,1.19` before writing code. Once PR3 lands, re-record the key ones
with `--informed` pointing at the facts that motivated them.
