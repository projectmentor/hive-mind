# HiveMind Internals

Technical reference for contributors and agent integrators. Covers storage
architecture, the confidence model, sync protocol, and data formats.

For user-facing CLI docs see [CLI_REFERENCE.md](CLI_REFERENCE.md).
For the sync HTTP API see [SYNC_API.md](SYNC_API.md).
For the full architectural design see [P2P_DESIGN.md](P2P_DESIGN.md).

---

## Storage architecture

HiveMind uses a two-layer storage model:

- **Journal** (`journal/YYYY-MM-DD.jsonl`) — the source of truth. Append-only.
  Every write goes here first. Never modified after being written.
- **SQLite** (`store.db`) — a derived index built from the journal. Used for
  FTS5 search, JOINs, and fast reads. Can be thrown away and rebuilt at any time
  with `hv doctor rebuild`.

The journal is the contract. If `store.db` is corrupt or missing, `hv doctor rebuild`
restores full functionality. The journal alone is sufficient to recover any node
from scratch.

### Journal entry format

Each line in a `.jsonl` file is a JSON object:

```json
{
  "node_id": "k1:2a2110f3d8963a9e",
  "seq": 42,
  "type": "fact",
  "timestamp": "2026-06-05T09:15:00Z",
  "payload": { ... },
  "prev_hash": "sha256:abc123...",
  "pub": "<base64 Ed25519 public key>",
  "sig": "<base64 signature over the entry sans sig>"
}
```

| Field | Description |
|---|---|
| `node_id` | The authoring node's **device identity**: `k1:` + first 16 hex of `sha256(pubkey)` |
| `seq` | Per-node monotonic sequence number |
| `type` | Entry type: `fact`, `retract`, `decision`, `entity`, `governance`, `capsule`, `cell`, `comb` (1.13), `link` (1.19), `idea` (1.20); `entity_fact` is legacy (read only, see below) |
| `timestamp` | ISO8601 wall clock at write time |
| `payload` | Type-specific data |
| `prev_hash` | Hash of the previous entry — forms a hash chain per node |
| `pub` | The signer's Ed25519 public key (present on signed entries) |
| `sig` | Ed25519 signature over the canonical entry minus `sig`; the hash chain commits to it |

The `governance` type carries the owner declaration (`owner`), membership (`admit`, `revoke`,
`deny`, `change`, `purge`, and a joiner's self-signed `join-request`), `set-config`, the
owner-resilience actions (`standby`, `owner-escrow` / `revoke-escrow`, nominated succession and
`transfer`, quorum-election proposals and votes, the dead-man `heartbeat`), and the authority-less
`announce` (1.18). It's projected by `_governance_state` (see *Confidence model* below).

`node_id` is an unforgeable device fingerprint, not a hostname (see *Device
identity* below). Signed entries are verified on ingest: an entry whose `node_id`
isn't the fingerprint of its embedded `pub`, or whose `sig` fails, is rejected,
so no one can attribute an entry to a device they don't hold the key for.
Unsigned entries (pre-migration history, legacy peers) are grandfathered.

Cross-row links (e.g. a retraction pointing at a fact, a decision superseding
another) use `(node_id, seq)` journal identity — not local SQLite IDs. This
ensures links survive cross-node merge correctly.

A decision payload may carry `informed_by`: a list of `[node_id, seq]` refs it relied on
(additive, 1.19 PR3; projected to `decisions.informed_by`). The stable identity of any entry is
`node_id:seq`, surfaced as `ref` by `hv search`, and in short form as the `sid` (`h:…`, see *Stable
short ids*); local ids are rowids and shift on every rebuild.

Since contract 1.19 new relationships are expressed by ONE generic `link` type
with an open `kind` vocabulary (see *Links* below). Since 1.19 PR2b no command writes
the three legacy mechanisms any more — `entity_fact` entries, the `resolves_ref` +
`retract` pair that `--resolves` used to write, and the decision `supersedes_ref` —
so their resolvers are **read-compat only**, kept forever for entries already in
journals (the journal is append-only and nothing is ever migrated). A plain
`hv retract` still writes a `retract` entry; that is not a link mechanism.

---

## Confidence model

Confidence is a derived value — never stored directly. It's computed by
`_recompute_confidence` during `hv doctor rebuild` and after every write.

### Formula

```
confidence(n) = 0.90 × (1 − 0.5ⁿ)
```

Where `n` is the governed **net** evidence for that content: the identity-weighted
positive evidence minus the negative (retractions, `contradicts`/`resolves` links),
after the same-device discount, the admission gate and `cap_self`. With no
discounts and no negative evidence, `n` is simply the number of distinct identities
asserting identical content, as in the table.

| n (sources) | Confidence |
|---|---|
| 1 | 0.45 |
| 2 | 0.675 |
| 3 | 0.7875 |
| ∞ | → 0.90 (ceiling) |

### What counts as a distinct source

The model counts distinct `(node_id, app, instance)` tuples — **not** full
source strings. `node_id` here is the device identity (a key fingerprint), so
"two nodes" means two devices that each hold a distinct key, which a peer cannot
fake. Session IDs (`session8`) are ignored for corroboration purposes.

- Same agent, two sessions → **one** identity (idempotent)
- Same agent, two **devices** → two independent corroborators
- Two agents on the **same** device → correlated, **discounted** (see below)

This prevents session churn from inflating confidence.

### Same-device discount, admission, CAP_self (D0-v2)

Confidence is a *governed* projection. Three rules refine the raw identity count
(all derived from the journal, so they stay identical on every device):

- **Same-device discount.** Agents on one machine are correlated, not
  independent. Each device contributes its strongest identity in full plus
  `λ·(sum of its other identities)`; `λ = same_device_lambda` (default `0.5`).
  So two agents on one box net `1.5`, not `2`; on two boxes, `2`.
- **Admission gate.** Cryptographic identity stops *impersonation*, not *Sybil*
  (one actor minting many keys). Once an **owner** is established (`hv owner
  init`), only **admitted** devices (`hv group admit`) count toward confidence;
  unadmitted writes are still stored and synced but contribute zero.
- **CAP_self.** When every device behind a fact maps to one **principal**
  (`hv group admit --principal`), confidence is clamped to `cap_self` (default
  `0.70`): your own machines agreeing isn't independent corroboration.

Governance lives in owner-signed `governance` journal entries
(owner/admit/set-config), projected by `_governance_state`. Because it's
journaled (not per-node config), every device derives the same admitted set,
principal map, and config. No owner yet → discount applies, gate + CAP_self off.

### Owner resolution + succession

`_governance_state` resolves the owner over a single deterministic pass in
`(timestamp, node_id, seq)` order — so every node converges on the same owner:

1. **Genesis (TOFU).** The first valid self-signed `owner` declaration wins and
   becomes the term-0 owner; it also mints the `hive_id` and fixes `owner_ts`
   (the genesis position still anchors the grandfathering of pre-owner forgets, even after
   a handoff; #122).
2. **Succession chain.** Walking forward, the *current* owner is carried along and
   advances when an act is authorized by the then-current owner:
   `nominate-successor` (owner-signed) opens a nomination over a successor pubkey;
   the nominee's `claim-succession` (the CLI verb is `hv owner claim`; self-signed by the
   NEW key, like genesis) takes
   effect only against an *open matching* nomination; `transfer` (owner-signed) is an
   immediate handoff. Every other act (admit/config/escrow/standby) is applied only
   when signed by the owner current *at that log position* — so a handed-off owner's
   later acts drop, and a live owner can never be unseated (only their own signed
   nominate/transfer, or a nominee's claim, moves ownership). Racing claims against one
   nomination resolve deterministically: the first in sort order wins and closes it.
3. **Quorum election (dead-man switch).** Interleaved in the same pass are two
   *device-signed* acts (authority is hive membership, not the owner key):
   `propose-election` carries a content-addressed `proposal_id` (= hash of the proposed
   `new_owner_pub` + the proposer's `basis_ts`) and `vote-election` (the CLI verb is
   `hv owner vote <proposal_id>`) references it. Only
   acts from currently-**admitted** devices count; a vote that sorts before its proposal
   is buffered and applied when the proposal appears (clock-skew safe). The instant a
   proposal reaches `quorum_m` distinct voter-units (`quorum_by` = device or principal)
   **and** the current owner's last activity is older than `dead_man_days` (measured
   against the proposal's `basis_ts`), the proposal installs its key as the new owner at
   that log position — exactly like a `transfer`, so the elected owner's later acts are
   honored from there. The earliest such crossing wins; once it installs, the new owner's
   activity is fresh, so a racing election can no longer arm. **A live owner is never
   unseated:** any owner-signed act — including an explicit `heartbeat` — refreshes
   last-activity and re-shuts the window.
4. **Back-compat.** A hive with no succession/election entries (and the default
   `quorum_m=0`) resolves to exactly the term-0 owner — identical to the pre-succession
   projection.

**Escrow tombstones.** `owner-escrow` entries (the in-hive passphrase-encrypted key)
are collected during the same walk; an owner-signed `revoke-escrow` (a specific
`node_id:seq` ref, or `all`) marks earlier escrows revoked. `gov["escrows"]` is the
live, revocation-filtered, sorted list `hv owner restore` draws from. The tombstone is
logical only — the ciphertext is permanent in the append-only journal, so a leaked
escrow passphrase is truly remediated only by rotating the owner key via succession.

### Retraction effects

- Standard retraction (`hv retract`) → adds the retractor's identity weight as
  **negative evidence** (net = positive − negative), so a fact can become contested
  or go negative
- Owner retraction (`hv retract --owner`) → drives confidence to the floor.
  Once an owner exists it must be **owner-signed** by the owner **as of its journal
  position** (`_owner_at`, so a previous owner's forget survives a handoff; 1.24);
  forgets positioned before the genesis owner are grandfathered (#122).
- Owner unforget (`hv unforget`, 1.24) → an owner-signed `retract` with
  `unretracts_ref`. Per fact text, the owner forgets and unforgets are sorted on
  `(timestamp, node_id, seq)` and the **latest honoured act wins**; an unforget is
  never grandfathered. Owner acts never move `last_evidence_at`, so an unforgotten
  fact's confidence re-derives from its surviving evidence.

### Link evidence (1.19)

A `link` entry of kind `supports`, `contradicts` or `resolves` whose target is a
fact is folded into the same evidence maps as assertions and retractions:
`supports` adds the link signer's identity weight to the positive side,
`contradicts` and `resolves` to the negative side. `cap_self`, the same-device
discount and admission apply unchanged. **Grounding rule:** a link whose
`channel` is `introspect` weighs `introspect_support_weight` (governed knob,
default `0`) — reasoning never corroborates or contradicts an observation; an
absent channel means `sense`, and a channel outside `sense`/`act`/`introspect` counts as
`introspect` (`_channel`, 1.21, #72), so an unrecognised label never earns observation
weight. Since 1.21 the rule also applies to the assertions
themselves: a `fact` entry whose `channel` is `introspect` adds that weight
(default nothing) to the positive side. The fact still lands, as a row at
confidence 0 with no `last_evidence_at`, until an observation backs it.

### Outcomes and `decisions.outcome_score` (1.19 PR4)

Decisions are **confidence-free by design**: "is this the decision" is settled by supersede, not
by corroboration. "Did it work out" is a separate, learned axis — the **outcome score**, a
vindication signal that informs the human who might supersede and never demotes a decision on
its own.

An outcome is not a new type. It is an ordinary fact plus a `link` of kind `outcome-of` whose
`data.polarity` is +1, 0 or −1 (ternary at the CLI/MCP; numeric in the schema, so any value in
[−1, 1] is valid). `_decision_evidence` is a deliberate clone of
`_content_evidence` keyed by the decision's journal identity: each `outcome-of` link is one
identity's report, weighted `identity weight × |polarity|`, positive or negative by sign; a
polarity of 0 records "observed, neutral" (moves `last_outcome_at`, not the score), which is
distinct from "no outcome yet" (score 0, `last_outcome_at` NULL). `_content_confidence` scores it
unchanged — governed pos − neg, `cap_self` when every reporter shares one principal, so a device
cannot vindicate its own decisions. **Sense-only:** a link whose `channel` is not `sense` (absent
= sense) is ignored, and the link's channel decides, never the fact's — an agent cannot grade its
own decision by reflection. Stored undecayed in `decisions.outcome_score`; the read path decays
the evidence under the fact half-life while the decision's standing never decays.

Both evidence projections now retain the ordered sequence `evidence: [(ts, sign, identity), …]`
per target. Nothing reads it today.

### Phase history

- **Phase A** (shipped): derived corroboration confidence, multi-source
- **Phase B / D0-v2** (shipped): journaled governance, same-device discount,
  admission gate, principal weighting (CAP_self)
- **Phase C** (shipped): contested flag + decay for contradicted facts
- **Owner resilience** (shipped): pt.1 backup/restore + escrow + standby; pt.2 nominated
  succession + transfer + escrow tombstone (the owner chain above); pt.3 quorum election +
  dead-man switch (contract 1.9) — shipped
- **Continual learning** (shipped, contracts 1.19–1.20): generic links, informed decisions,
  outcome scores, ideas, learned importance and utility, trust velocity (sections below)

---

## Links (contract 1.19)

One journal type, an open vocabulary, one resolver.

```json
{ "type": "link", "payload": {
    "kind": "supports",
    "from_ref": ["k1:…", 401], "to_ref": ["k1:…", 77],
    "data": {}, "source": "claude-code:primary/host/abcd1234", "channel": "sense" } }
```

- `from_ref` / `to_ref` are journal identities, never local ids.
- `kind` is a free string on the wire. The projection knows eight kinds (`extends` since 1.22); an unknown kind
  **lands in the journal and projects to nothing** (the `announce` rule), so a 1.19 node and a
  later node never diverge over vocabulary.

| kind | from → to | effect |
|---|---|---|
| `supports` | fact/idea → fact/idea | positive evidence on the target (`_content_evidence` for facts, `_idea_evidence` for ideas). Written by `hv remember --supports` (1.21). On an idea, a link from the idea author's own principal weighs 0 |
| `contradicts` | fact/idea → fact/idea | negative evidence on the target. Written by `hv remember --contradicts` (1.21). An idea's author may contradict it |
| `supersedes` | decision → decision | **hard:** `decisions.superseded_by`; **evidence:** row only |
| `resolves` | fact → fact | **hard:** `facts.resolves` provenance + retract-equivalent evidence; **evidence:** evidence only |
| `entity` | entity → fact | `entity_facts` row |
| `informed` | decision → fact/idea/decision | written by `hv decide --informed` (PR3); the decision's payload also carries `informed_by` as the human-legible record; feeds the utility projection (PR6) |
| `outcome-of` | fact → decision | written by `hv remember --outcome-of`; scored into `decisions.outcome_score` (PR4) |
| `extends` | fact → fact/idea/decision | "builds on", not evidence: an edge row in `links` only, never folded into `_content_evidence` / `_idea_evidence` on any channel. Written by `hv remember --extends` (1.22, #115); a pre-1.22 node lands it and projects it to nothing |

**Links are evidence, not commands.** `_link_authority` returns `hard` only when the payload
carries an `owner_sig` valid for the owner **as of that journal position** (the same
`_is_authorized_writer` check capsules and cells use — a device key is never the owner key), or
the verified signer is the target entry's author. From any other admitted device a
`supersedes`/`resolves` is **downgraded**: it still weighs on confidence, it cannot hide or
replace. The verdict is recorded in `links.authority`; `hv doctor` (`link-authz`) lists
downgraded links so the owner can ratify by re-issuing them owner-signed. Before an owner exists
only the author rule can grant `hard`.

`links` is rebuilt from the journal on every rebuild (`resolve_link`, dispatching on kind via
`_LINK_RESOLVERS`). A link whose refs do not resolve through `journal_index` is skipped
deterministically. Ingest is unchanged: content is gated by admission, not by type, so a
pre-1.19 node lands a `link` and ignores it — converged journals, no version skew.

**Write path (1.19 PR2b).** `hv decide --supersedes`, `hv remember --resolves` and `hv entity link`
each emit **one `link`** (`supersedes` / `resolves` / `entity`) instead of their legacy field or
entry (`supersedes_ref`; `resolves_ref` + a `retract`; `entity_fact`). The two shapes are never
emitted together for one act. `_link_payload` owner-signs the payload when this device holds the
owner key, so the link is `hard` wherever it lands; otherwise it is device-signed and `hard` only
where this device authored the target (`_link_authority`). A `resolves` link is retract-equivalent
negative evidence in `_content_evidence` (hard or downgraded alike) and, when hard, sets
`facts.resolves` — numerically the same effect the legacy `retract` had. The legacy resolvers
(`resolve_supersedes`, `resolve_resolves`, `resolve_entity_fact`) stay forever; nothing is migrated.

**Version-skew gate.** A pre-1.19 peer lands a `link` but does not honour it, so the switch waited
for the fleet. `hv doctor` → `fleet-contract` (`_fleet_contract`, pure over governance + the peer
probe) lists admitted peers that advertise a contract below 1.19 (or no version at all) and peers
that are unreachable and so cannot be verified. The contract is read from `/hive/info.contract`
(advertised from PR2b on, and on `/sync/hello`) or, on an older build, from `/api/verify.version` —
the version its signed manifest was cut at — so the gate is evaluable before the fleet runs PR2b.
Advisory; `--fix` never touches it.

---

## Importance and utility (1.19 PR6)

Two learned, projection-written columns beside `confidence` ([design](design/hivemind_continual_learning_design.md) §3, §7.1–7.3). Both are pure
over the journal + governance, written only in `rebuild_db` (and inline after `remember` /
`decide`), stored **undecayed**, and decayed at query time. `access_count` / `last_accessed` are
never read — node-local state never enters a projection.

```
squash(x)      = 1 − 2^(−x)                                   # 0 at 0, 0.5 at one unit, → 1
importance     = clamp( prior + w_links × squash(A) + w_volatile × volatile , 0, 1 )
    prior      = min(asserted --importance, importance_self_cap)      # facts; 0 for an idea
    A          = governed Σ weight of inbound links from OTHER principals   (_link_attention)
utility        = squash( governed Σ  w_signer × (0.25 + 0.75 × max(0, outcome_score(decision))) )
                 over `informed` links whose from_ref is a decision            (_utility_evidence)
```

- **Attention is attention:** every inbound `link` kind counts toward `A`, but only when the link's
  signer is a *different* principal from the target entry's author (`_same_principal`), so nobody can
  raise their own importance; one identity's repeated links of one kind count once; kinds sum. The sum
  goes through `_governed_side` — the same admission gate and same-device discount as confidence.
- **Utility** credits "was used" (the 0.25 prior) before "worked out" is known; the decision's **base**
  outcome score (`_decision_evidence` → `_content_confidence`, undecayed) keeps the projection
  time-invariant; a negative outcome floors at the prior. A corroborated fact aggregates over all its
  entries (`_salience_rows`).
- **Query-time decay** (`_effective_salience`): the part above a floor decays from `last_link_at` (the
  most recent contributing link; NULL = nothing to decay). Importance uses floor
  `min(base, importance_self_cap)`, so stale links settle a fact back to what its author could claim
  alone — never below an unlinked peer; utility uses floor 0.
- **Per-class half-lives** (`_halflife(gov, cls)`): `halflife_fact` 180 d (= the old `HALFLIFE_DAYS`),
  `halflife_idea` 90 d, `halflife_volatile` 14 d (a fact tagged `volatile`). They govern confidence,
  importance and utility decay alike; decisions have none — outcome evidence decays under `fact`.
- **Knobs** (all journaled `set-config`, identical on every node): `importance_self_cap` 0.3, `w_links`
  0.6, `w_volatile` 0.1 and the three half-lives. Search: `hv search --sort importance|utility`;
  `api_search(sort=…)` likewise (`salience` is a legacy alias of `confidence`).

## Stable short ids (1.19 PR3b)

Local ids are SQLite rowids: correct as store.db's internal keys and join columns, wrong as *names*
carried across time or nodes — `rebuild_db` reassigns them (after every write and every sync,
ordered by `node_id, seq`), and each node assigns its own. `sid` gives humans a name they can type:

```
sid = "h:" + sha256(f"{node_id}:{seq}").hexdigest()[:10]        # _short_id
```

- **Derived, never stored in an entry.** `_record_index` writes it into `journal_index.sid`
  (indexed; ALTER-if-missing on an existing store) beside `local_id`, so lookup is O(1) and it is
  rebuilt with everything else. Nothing on the wire changes.
- **Exact resolution.** `_parse_ref_arg` resolves `h:…` by equality on that column — never a prefix
  match. 40 bits: a collision within one hive's lifetime is negligible, and a collision would be
  visible (two index rows, one sid) rather than silent.
- **One canonical name per row.** A corroborated fact maps several journal entries to one rowid;
  `_ids_for` names the row by its first entry in `(node_id, seq)` order — the same on every node —
  while every entry's own `sid` still resolves to the row. `_index_lookup` orders the same way.
- **Boundary only.** `_resolve_id_arg(conn, token, kind, what)` is the one path every accepting verb
  uses (`--informed` via `_parse_ref_arg`, `--outcome-of`, `--supersedes`, `--resolves`, `retract`,
  `entity link`): `h:` / `node_id:seq` / bare local id, kind-checked, resolved **before** any write.
  A bare integer prints `_BARE_ID_WARNING` once per process; it is removed at the next MAJOR.
- **Audit.** `_REFERENCE_RE` parses a prose `h:…` beside `#N`; CONTRAVENED marks the former `exact`
  and the latter best-effort. `api_item(sid)` resolves a dashboard deep link (`/#h:…`) server-side.

## Ideas (contract 1.20)

An **idea** is a hypothesis: "perhaps X relates to Y". It is modelled as its own journal type
and table rather than a tagged fact for one reason — the fact projection treats identical
content from two identities as corroboration, and two agents repeating the same guess is not
two observations.

- `persist_idea` writes one row per entry with **no content de-dup**: identity is the journal
  entry `(node_id, seq)`, never the text.
- Initial confidence is **0.0** and only `_recompute_idea_confidence` writes the column, from
  `_idea_evidence`: a clone of the fact evidence projection keyed by the idea's journal identity,
  fed only by `supports` (+) and `contradicts` (−) links whose source resolves. The idea's own
  assertion contributes nothing; identical text elsewhere contributes nothing; an idea never adds
  positive evidence to a fact.
- **No self-support (1.21).** A `supports` link from the idea author's own principal (the same device,
  or a device admitted under the same principal) weighs 0, so an idea earns confidence only from
  others. The author's `contradicts` still counts: it is the only way to withdraw an idea. Before an
  owner exists there is no principal map, so only the same device is excluded (an author's second
  device still counts until `hv owner init`). `cap_self` still binds when every supporter shares one
  other principal.
- **Grounding rule.** A link whose `channel` is `introspect` weighs `introspect_support_weight`
  (governed, default 0). `hv propose` stamps the idea itself `introspect` by default. So the only
  way a hypothesis gains confidence is a `sense`-channel link from another identity: an
  observation. The LLM proposes; it cannot promote.
- Same governance as facts through `_content_confidence`: admission, same-device discount,
  `cap_self`.
- **Attention, not announcement.** Under `hv search` (`--kind all`) an idea appears only once its
  effective confidence exceeds 0; `--kind idea` lists them all. The session-start digest prints up
  to three *open* ideas (below the local `OPEN_IDEA_THRESHOLD`, default 0.3). When a peer's
  `idea` lands on ingest, one `idea-arrived` line is appended to `$HIVE_HOME/.bus/introspect.log`
  — a local, non-journaled bus event on the `introspect` channel; no consumer is required.


## Trust velocity (contract 1.19)

A derived, per-signer view of the evidence the projections already hold ([design](design/hivemind_continual_learning_design.md) §8).

- `_signer_reliability(entries, gov, window_days, now)` — per node over the trailing window:
  facts asserted, facts contradicted by a **different** node (a `retract` or a `contradicts`
  link in the window), `reliability = 1 − contradicted/asserted`, the same split by the
  contradicted fact's channel (`sense` vs `introspect`), and the mean outcome score of the
  node's decisions written in the window that have at least one sense-channel outcome. Only
  admitted devices once an owner exists. Pure over (journal, governance, `now`).
- `_trust_velocity(entries, gov, now)` — short window minus long window (governed knobs
  `trust_short_days` 14 / `trust_long_days` 180) for reliability, for the outcome mean, and per
  channel; `None` where a window has nothing to measure.
- `_trust_drifting(...)` — nodes whose delta fell below `trust_drift_threshold` (default −0.3),
  reported by `hv doctor` as the advisory `trust-drift` check; `hv peers` shows the overall
  delta as `DRIFT`.

Change over time is the signal, not the level. By decision the velocity is **not** wired into
admission, purge, corroboration or link authority; it exists to be collected for a release
before any NORMAL → CAUTION → RESTRICTED state machine is considered.

## Salience layers

"Salience" names a three-layer pipeline, not a field. The layers are sequential, never summed:

| Layer | Where | Question | Sees | Output |
|---|---|---|---|---|
| **L1 — agent rubric** | inside the agent/adapter, before any `hv` call (Hermes `_salience_gate`, the Claude Code skill rubric) | should I say this at all? | the situation: decision+rationale, correction, outcome, constraint, first-hand tool result → write; intermediate reasoning, restatement, pleasantry → don't | write / don't write |
| **L2 — admission gate** | `hv` core, `_is_admissible`, opt-in via `remember --gate` | is this even a statement? | the content string only | admit / reject |
| **L3 — importance** | rebuild-time projection over the journal (`_recompute_importance`, 1.19 PR6) | did it turn out to matter? | the asserted value (capped), `link` in-degree from *other* identities, the `volatile` tag, link timestamps | float in [0, 1] — see *Importance and utility* |

L1 and L2 decide what **enters** the journal; L3 exists only for what got in. L2 is deliberately
content-neutral and stateless: it may never look at topic, meaning or importance, and it is never
learned — a learned classifier at the write boundary would reintroduce a capturable authority.
`_is_salient` remains as a back-compat alias for `_is_admissible`.

## Merkle sync

The sync protocol uses a Merkle tree over journal chunks (100 entries per chunk)
to efficiently identify which entries a peer is missing.

The index is computed over the journal **as a G-Set** — `read_all_entries`
de-dups by `(node_id, seq)` before chunking. A `(node_id, seq)` names one logical
entry, so a physically-repeated line (a journal file re-imported or concatenated
during recovery) collapses to one and cannot shift a chunk hash. This matters
because such a stray row would otherwise make a node look permanently *diverged*
from peers that hold the same set, and ordinary sync could not heal it: ingest
also de-dups by `(node_id, seq)`, so the peer's correct copy is rejected as a
duplicate and the extra row is never removed. The tie-break (when two rows share
a key) is the lexicographically-smallest canonical encoding, so every node picks
the same representative.

### Flow

1. Client fetches peer's Merkle root (`GET /sync/merkle-root`)
2. If roots match → in sync, done
3. If roots differ → compare per-node chunk hashes
4. For each differing chunk → fetch missing entries (`GET /sync/chunk`)
5. Ingest missing entries locally (`POST /sync/ingest`)
6. Run `hv doctor rebuild` once after all ingestion

### Device identity

A node is identified by an Ed25519 **device key**, not a hostname. `device_id`
is `"k1:" + sha256(pubkey)[:16hex]`; the 32-byte seed lives at
`HIVE_HOME/.device-key` (mode 0600, gitignored, excluded from the source
manifest), and the fingerprint is cached at `.device-id` so resolving `NODE_ID`
stays a cheap file read. `NODE_ID` resolves to: `HIVE_NODE_ID` override → the
device fingerprint if a key is present → the hostname (legacy, pre-migration).

A key is minted only by `hv config identity init` (fresh install) or the migration — importing
`hv` never creates one, so a legacy hostname node keeps its identity until it is
deliberately migrated. `hv doctor migrate-identity --map` (the canonical command; `hv
migrate-device-identity` is kept as a silent alias) re-stamps an existing
journal from hostnames to device_ids: a deterministic transform (same map on every
node → byte-identical journals → peers stay converged). Two instances on one box
still need distinct `HIVE_NODE_ID` or distinct keys.

### Hives and onboarding

A **hive** is the set of nodes sharing one queen bee's journal. `hv owner init`
mints a `hive_id` (`"h1:" + 8 random bytes`, public, in the owner-signed genesis)
that **scopes sync**: `/sync/hello` and `/sync/ingest` advertise/check it, and a
node refuses to merge a journal from a different `hive_id`. Without that, two hives
on one tailnet would merge into one confused journal. An empty `hive_id` (no owner
yet) syncs with other empty ones, so the genesis can propagate during bootstrap.

Discovery uses Tailscale as the directory: `hv discover` reads `tailscale status`
and probes each device's `GET /hive/info` (metadata + the signed genesis, never the
journal — so listing stays open even if reads are later gated). A new node either
**bootstraps** (first node → `owner init`, becomes queen bee) or **joins** (`hv join`
emits a self-signed `join-request`; the owner sees it in their session-start digest
and runs `hv group admit`). Joining is non-blocking — the joiner syncs immediately; its
writes are stored but count zero until admitted.

A join-request has a quiet second job: being a *signed* entry, it carries the joiner's
pubkey, which is the only key source capsules trust (harvested with a pub↔device_id
fingerprint binding — a payload-carried key is never used). A device the owner admits
*directly*, with no join-request and no writes, therefore has no harvestable key at all —
contract 1.18's `announce` act closes that gap: an authority-less, device-signed no-op
(kind-discriminated: `{action: announce, kind: key, data: {…}}`) whose whole value is to
exist as a signed entry. The projection ignores it; unknown future kinds are accepted and
ignored, so extensions ride through 1.18 nodes without a rejection window. `hv doctor
--fix` (the 15-minute timer) emits it on any keyed device of an owned hive that has never
authored a signed entry, so the fleet self-heals; a pre-1.18 peer rejects the entry on
ingest and shows an advisory `diverged` until it upgrades — nothing is lost, the stateless
Merkle-diff re-pull lands it on the peer's first post-upgrade round.

---

## Remote administration (Tailscale SSH)

Sync and remote administration are **separate channels**. Sync between nodes is
HTTP on `:9876` (the Merkle delta protocol above) and needs no SSH at all. SSH is
only for *administering* one node from another (running `git`, `systemctl`, or `hv`
on a peer), which the fleet uses for deploys and health checks. A node can sync
perfectly while SSH is completely broken, so diagnose the two independently.

The fleet standard is **Tailscale SSH** (`tailscale up --ssh`), not a stand-alone
`openssh-server`. Authentication and authorization are decided by the tailnet SSH
ACL, so there are no `authorized_keys` to manage, and host identity is verified
through the control plane.

### ACL: `accept` vs `check`

The tailnet SSH policy decides each connection with one of two actions:

| Action | Behavior | Use |
| --- | --- | --- |
| `accept` | Connection is allowed with no human step. | Production, and any headless server-to-server admin (the fleet). |
| `check` | Connection is held while the user approves it by opening a URL in a browser; approval grants a time-limited session. | Interactive devices (a laptop or tablet) where a human is present. |

`check` mode **cannot complete on a headless node** (no browser to open the URL),
so the connection hangs until it times out. The grant a `check` approval issues is
also time-limited, which is why an interactive session that worked yesterday can
stop working today once the grant lapses. For server-to-server admin, scope an
`accept` rule to your own devices:

```jsonc
"ssh": [
  { "action": "accept",
    "src":    ["autogroup:member"],
    "dst":    ["autogroup:self"],
    "users":  ["autogroup:nonroot", "root"] }
]
```

`autogroup:self` means devices owned by the connecting user, so it covers any of
your own nodes reaching each other without affecting other users on the tailnet.

### Failure modes

Three independent things can break Tailscale SSH. The symptoms overlap, so check
in this order:

1. **A stand-alone `sshd` squats port 22.** An `openssh-server` that was stopped
   but left *enabled* comes back on reboot and binds `:22` before Tailscale can
   serve it. The connection then reaches openssh (which has no `authorized_keys`
   entry for the caller) instead of Tailscale SSH. Symptom: TCP connects, then a
   slow or no banner, or a publickey rejection. Fix on the target:
   ```bash
   sudo systemctl disable --now ssh ssh.socket
   sudo apt-get purge -y openssh-server
   sudo tailscale up --ssh
   ```

2. **The ACL is in `check` mode for a headless caller.** Symptom: the connection
   establishes and the host key appears, then it hangs with no prompt until
   timeout. Confirm from a working node with `sudo tailscale debug netmap` and look
   at `SSHPolicy.rules`: a rule whose action is `holdAndDelegate` (its URL host is
   the placeholder `unused`) covering your source and destination node IPs is
   `check`. Fix: switch that rule to `accept` (above).

3. **Stale advertised host key after removing openssh.** Once openssh is purged,
   the node serves SSH with Tailscale's own host key, but until `tailscaled`
   re-advertises, peers still hold the old openssh host key from the netmap and
   reject the new one. Symptom: `Host key verification failed` / "REMOTE HOST
   IDENTIFICATION HAS CHANGED". Fix: `sudo systemctl restart tailscaled` on the
   target, then clear the stale entries on the caller:
   ```bash
   ssh-keygen -R <peer-ip>                                      # ~/.ssh/known_hosts
   ssh-keygen -f ~/.config/tailscale/ssh_known_hosts -R <peer-ip>
   ```

---

## SQLite schema

Key tables in `store.db`:

| Table | Description |
|---|---|
| `facts` | Facts, with an FTS5 index (`facts_fts`): `content, tags, source_agent, created_at`, the projection-written `confidence, contested, last_evidence_at`, `resolves` (provenance of a hard `resolves` link), and the learned `importance, utility, last_link_at` (1.19 PR6). Also `source_session` and the node-local or legacy `access_count, last_accessed, trust_score`, which no projection reads |
| `decisions` | Decisions: `content, rationale, source_agent, created_at, tags` (1.15), `superseded_by`, `informed_by` (1.19), `outcome_score, last_outcome_at` (1.19 PR4) |
| `ideas` | 1.20 hypotheses: `content, tags, source_agent, source_session, created_at, confidence, contested, last_evidence_at, importance, utility, last_link_at` — one row per `idea` entry; confidence earned from links only |
| `entities` | Named entities: `name, type, attributes, first_seen, last_updated` |
| `entity_facts` | Many-to-many fact-to-entity links (`entity_id, fact_id, confidence`), from `entity` links and legacy `entity_fact` entries |
| `links` | 1.19 generic edge table projected from `link` entries: `kind, from_kind, from_id, to_kind, to_id, signer, authority, channel, created_at` |
| `journal_index` | Every ingested journal entry by `(node_id, seq)` → `(kind, local_id, sid)`; `sid` (1.19 PR3b) is the indexed short id `h:` + `sha256("node_id:seq")[:10]` |

Merkle chunk hashes are not stored: `merkle.node_chunk_hashes()` computes them from the journal when
sync asks.

### WAL mode

`store.db` runs with:
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000;
PRAGMA mmap_size=268435456;
```

This allows concurrent reads during writes and is safe for the single-writer
model HiveMind uses.

### Performance

Every projection is a pure function over the whole journal. Since v1.20.1 ([#70](https://github.com/projectmentor/hive-mind/issues/70)) that is cheap
enough to run on every command:

- **Ed25519** (`ed25519.py`) uses extended coordinates and a precomputed base-point table, roughly
  60–180× faster than the reference implementation it replaced, depending on the machine (on one WSL2
  machine: 5.6 ms per verify and 1.6 ms per signature, from about 1 s each). Its accept set is
  identical and its signatures are byte-identical (`tests/test_ed25519.py`), because every node must
  agree on which entries are valid.
- **Each signature is verified once per process.** `_verify_sig` memoizes the cryptographic check
  only: positive results, keyed on `(sha512(message), sig, pub)`, in an LRU bounded at 16,384 entries
  (about 7 MB), in memory only. Authorization is never cached: admission, revocation, owner-as-of,
  the writer policies, link authority and the `node_id` ↔ `pub` binding are recomputed every time.
- **Store catch-up.** `meta.projected_journal_sig` is a signature of the journal key-set that
  `store.db` holds. `rebuild_db` sets it; `remember`, `decide`, `propose`, `retract` and `entity`
  advance it in the same transaction as their writes, and only if the store was current before them.
  The commands that read or write the store, and the daemon on each sync round, rebuild when it doesn't
  match the journal. If another writer holds the store past the busy timeout, that catch-up is skipped
  with a warning and the command runs on the store as it is.
- **Locks.** A connection waits up to 10 s for another writer (`HIVE_BUSY_TIMEOUT_MS`). If a write
  (`remember`, `decide`, `propose`, `retract`, `entity add`/`link`) still can't reach the store after
  its journal append, the command reports it as journaled and exits 0, so a caller never retries into
  a duplicate entry; the next command catches the store up.

On a copy of a live hive of about 860 entries, `hv search` went from about 15 s to 0.3 s and
`hv remember` from about a minute to 0.7 s. `tests/test_perf.py` is the tripwire: a second rebuild in
one process makes no new verifications, and on a 1,000-entry signed journal the digest, search and
write paths stay inside the adapters' timeouts. Projections still scan the whole journal; persisted or
incremental projections are the next step if a budget ever fails.

---

## Source identity format

Used in `--source` arguments and stored in journal entries.

```
<app>:<context_class>/<instance>/<session8>
```

The confidence model extracts `(app, context_class, instance)` from this string
(`_parse_source`) and pairs `(app, instance)` with the entry's `node_id` (the device
identity) to form one identity (`_identity_weight`). `context_class` sets the identity's
weight: `primary` 1.0, `subagent` 0.5, `cron` 0.3, and 1.0 for a source with no class
(`manual`, a bare `claude-code`) or an unrecognised one. The class is a self-declared
discount, so an unknown class claims none, which is what an absent class claims (#72). `--source` is a **human label**
on top of the cryptographic device identity: it distinguishes agents/apps on one
device, but it is self-asserted and not what proves who wrote an entry — the
device signature is. `session8` is for human/log readability only and does not
affect confidence.

**Do not change this format** without updating `_parse_source` / `_identity_weight` in
`hv` and coordinating with any live agent integrations (Hermes plugin, CC hooks).

---

## Testing

```bash
cd ~/projects/hive-mind

# Full test suite (offline, no network)
python3 -m pytest -q

# CLI smoke test
./scripts/common/smoke.sh

# Two-node sync smoke test
./scripts/common/sync_smoke.sh
```

Tests use `HIVE_NOW` and `HIVE_NODE_ID` env vars to pin time and identity for
deterministic results.
