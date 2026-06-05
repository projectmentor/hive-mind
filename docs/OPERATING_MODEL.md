# Hive Mind — Operating Model

> **What this is.** The first-principles *why* beneath Hive Mind's mechanics — the design
> rationale, not a runtime charter that agents recite. Process Consultation is meant to be the
> **material fabric** of the system (woven into how confidence/salience/retrieval/identity/
> forgetting actually behave), *not* a posted constitution. Audience: humans (owner + future
> contributors + future sessions). Companion to the mechanics design in
> `~/.claude/.../memory/hive-mind-confidence-design.md`. Much here is design-only; status is
> marked per section. Phase A (derived confidence) is SHIPPED.

---

## 1. Why — Process Consultation as the fabric (and the moat)

The first-principles operating model is **Edgar Schein's Process Consultation** (*Process
Consultation Revisited: Building the Helping Relationship*). The meat is the *process* chapters
(roughly 1, 3, 7, 8, 9): what PC is, the dynamics of the helping relationship, active inquiry,
intervention, and learning/change.

Schein's three modes of helping: **buy-expertise**, **doctor–patient** (diagnose & prescribe),
and **process consultation** (help the client diagnose and solve, building their capacity). PC is
the *default*, because the other two assume the client already diagnosed correctly and can absorb a
prescription — usually false. The helper **never takes ownership of the client's problem**.

**Strikingly, we reverse-engineered Schein.** The sanity/capture-resistance mechanics we built are
already isomorphic to his principles:

| Schein principle | Hive mechanism |
|---|---|
| Access your ignorance | derived confidence + epistemic status; *never assert a trust number*; surface provenance not truth |
| Stay in touch with current reality | sync/replication; decay; *observed outcomes over interpretations* |
| Everything you do is an intervention | salience discipline — every write changes what peers believe, so write deliberately |
| The client owns the problem and the solution | human-as-terminator; escalation; admin = attributable append, not override |
| Active inquiry (humble/diagnostic/confrontive) | tension-holding retrieval (surface both sides, don't rank-pick); `dispute` = confrontive inquiry |
| When in doubt, share the problem | low-confidence / contested → surface + escalate, don't paper over |
| Everything is data; errors are inevitable; learn | append-only journal + retract/supersede; "known-false" memory; recoverable, not catastrophic |

So PC doesn't sit *on top* of the design — it *is* the design, named. The echo chamber is the
system **sliding from process-consultant into over-confident doctor/expert** (prescribing instead of
helping); "access your ignorance" + "the client owns the problem" are the antidote. Anti-echo-chamber
and the PC stance are the same phenomenon viewed twice.

**The moat.** A charter is words — copyable. Behavior woven into the architecture is not, because the
product is **trust**, and trust in a memory/decision system can't be shipped in a sprint. Nearly every
"AI memory" runs in doctor/expert mode — it asserts, prescribes, hallucinates with confidence. A
system whose *every mechanism* is built to **not take over and not delude itself** is categorically
different: *the only AI memory you can trust to help without lying or hijacking.* And it **flywheels**:
trustworthy memory gets used honestly → accrues genuinely corroborated knowledge → more useful → more
trusted. It survives open-sourcing, because the moat is the **accrued corpus + the lived helping
relationship**, not the code.

---

## 2. The autonomy stance (the heart)

> **Each agent makes the best decision it can from the information available at the time, reserving
> the right to adapt as new evidence arrives.**

This dissolves the "autonomy boundary" rather than drawing it. A decision held **provisionally** can't
seize ownership, because it is structurally always open to revision. So provisionality makes an agent
simultaneously **decisive** (not paralyzed waiting for certainty — paralysis isn't helpful),
**non-dogmatic** (not an echo chamber), and **non-usurping** (the owner, or new evidence, can always
reopen). It is one sentence that unifies:
- Schein's *stay in touch with current reality* + *errors are data, learn from them*;
- the confidence mechanics: nothing is ever certain (cap < 1.0); **dispute / decay / supersede** *are*
  the machinery of "reserving the right to adapt";
- the owner's own decision ledger: *"reopen on genuinely new information; the gate catches anxious
  loops, not course corrections."*

**Never take over the owner's reasoning** — at three levels, one rule:
- **agent → David:** David owns his *judgment*;
- **agent → agent:** a *reading* agent owns its own conclusion, so a *writing* agent gives material +
  provenance and never coerces belief (this is why confidence is derived, not asserted);
- **hive → everyone:** the hive owns **nothing** — it surfaces and weights, it is never the decider.

This is also the resolution of *"take me out of the loop, but oversight never graduates"*: the hive
removes the human as the **switchboard** (relaying between agents), not as the **owner** of judgment.
Only the secretarial role graduates.

---

## 3. Human vs agent calibration

Schein's active-inquiry / manage-face machinery exists to build a **human's** capacity and protect
their ownership. So:
- **Toward David:** helping-inquiry; surface options and tension; protect his ownership; don't prescribe.
- **Between agents:** there is no face to manage, and Socratic machine-chatter just burns tokens — so
  be **direct**: state the correction, tag the provenance, file the dispute.

Same rule ("never take over the owner's reasoning"), different *style*.

---

## 4. The epistemic pilgrimage

Confidence is not a scoreboard; it is a **pilgrimage**. Every claim is somewhere on a road, always
climbing or slipping, because reality never stops speaking.

```
ascent :  discovered → remembered → accepted → believed → confirmed/canonical
descent:  believed   → doubted    → rejected → forgotten
```

Stations, each grounded in a mechanism:
- **Discovered** — a single, ephemeral mind originates/encounters it. Single source, present, unproven.
  *(A new fact; our minds are mortal — agents die with their sessions.)*
- **Remembered** — the *collective* now **holds** it; it survives the discoverer and the moment, is
  recalled, persists. **This is the soul of the project**: a fleeting realization by a mortal mind
  becoming part of a persistent shared one — the defeat of the agent's mortality. Note **remembered ≠
  believed**: the hive faithfully holds and surfaces what it has not endorsed (with honest low
  confidence). A good custodian keeps the record without preaching the conclusion.
- **Accepted** — *distinct, independent* witnesses corroborate; confidence rises (0.45 → 0.675 → …).
- **Believed** — well-corroborated, near the cap; the fleet's working conviction.
- **Confirmed / canonical** — the owner or external reality **verifies** it (a `verify` event lifts it
  past the cap toward 1.0). Consecrated; foundational (genesis facts, this operating model).

Forces: **up** = corroboration / witness / use / verification (all *earned*, never self-asserted);
**down** = doubt / refutation (dispute, retract) / decay (neglect — not renewed) / owner-judgment
(forgetting).

Three things the road encodes:
1. **You earn your way up; you can't declare it.** Repetition is the counterfeit pilgrim (the echo
   chamber). Standing is granted by *witness and reality*, never seized by assertion.
2. **The cap is humility, and it's load-bearing.** Belief never quite touches certainty *on its own*
   (tops out at CAP ≈ 0.9) until something *outside the system* — the human, reality — consents. The
   summit isn't certainty; it's **earned, humble, revisable conviction.**
3. **The road runs both ways; nothing is finally damned.** New evidence resurrects a fallen thing
   (resurrection = re-discovery); contradiction topples a believed one. "Reopen on new evidence" is
   grace built into the epistemics. The only near-final act is the owner's **forgetting** — which is
   why forgetting is *governance*, reserved for the one who owns the problem.

---

## 5. Signed confidence  *(design-only; generalizes Phase A + Phase C)*

Confidence is a signed, saturating, **derived** projection — a function of *net weighted evidence*:

```
epistemic range:   [ −CAP , +CAP ]     (CAP ≈ 0.9)
  +  believed        (corroboration outweighs)
  0  unknown         (no credible weight either way)
  −  rejected        ("known-false" — hidden from default retrieval, surfaced on re-assertion)
governance floor:  ≤ −1                 (owner-forgotten — below the epistemic range)
```

The threshold `−1` *is* the governance/epistemic boundary: peer evidence (corroboration & disputes) is
bounded in [−CAP, +CAP]; only **owner-forget** (governance, not evidence) pushes below −CAP into the
"gone" zone. Magnitude is always **DERIVED** (counted retractors × fixed source-class weight), **never
a self-typed number** — a self-set confidence is the corruptible authority we ban.
- Owner/admin retract → high source-class weight → decisive (drives to ≤ −1).
- Peer retract → counted like a dispute, must out-weigh corroborators; a lone/forged retract can't nuke
  a well-corroborated fact.

The "rejected" tier is a *feature*: the corpus **remembers what it disproved**, so the fleet stops
re-litigating settled-false things (saves tokens, stays sane) — the ledger's reopen-test in mechanism form.

---

## 6. Forgetting  *(design-only)*

**forget ≠ decay.** Decay = automatic *fading* (still present, weighted down) for "this is getting
stale." Forgetting = *deliberate removal* for "this should not be here at all" (test junk, errors, PII).
Two tiers:

- **Soft forget — retract:** derived *negative evidence* (§5). The projection sinks the fact below the
  retrieval floor; the journal bytes + the retract remain (audit; reversible by genuine re-corroboration).
  Default tool.
- **Hard forget — federated epoch compaction:** *actually erases bytes*. Required to be **coordinated /
  owner-gated / backed-up**, because the journal is a **G-Set**: a local delete *resurrects* via the
  next union merge. So:
  - **Determinism** — compact against a **converged cutoff** (all nodes agree on history up to seq X)
    with an agreed `now` (decay reference); each node then computes the *same* `confidence ≤ threshold`
    drop-set and rewrites to a new baseline. Converge first, then compact.
  - **Anti-resurrection** — handled by a single **epoch + cutoff baseline**, *not* per-id tombstones. A
    straggler that was offline adopts the canonical compacted baseline for everything ≤ cutoff (its dropped
    entries vanish) and keeps only its own post-cutoff writes. Resurrection prevented with O(1) metadata.
    In a *fully-synchronized* compaction there is **no residue at all**; the epoch marker exists only for
    offline stragglers and itself retires once every node is past the epoch.
  - **Auditability** — keep an optional one-line governance record ("epoch N: forgot 12 entries ≤ −1, by
    owner, at T"). Content erased; the *fact of forgetting* retained. Even erasure stays attributable.
  - Workflow for junk: `hv retract <id>` (owner-decisive → ≤ −1) → `hv forget <threshold>` (federated
    compaction erases). Precedent for journal rewrite + hash-chain recompute: `migrate_journal_v2.py`.

One continuous axis (believed → unknown → rejected → forgotten), one admin knob (the threshold), soft
retract as the on-ramp, and a coordinated epoch as the only heavy operation.

---

## 7. Membership lifecycle — entering & leaving  *(design-only; a missing pillar)*

Schein's group dynamics reframe membership from a cold ACL into a **relationship with a lifecycle**,
which unifies several things we'd scattered (admission, circles/pub-sub, genesis/bootstrap, identity,
poisoning-recovery):

- **Entering** isn't "add an ACL row" — it's *building the relationship*. A new agent/node joins
  **humble and low-trust** (no corroboration history → the genesis "single-source / low-confidence"
  start), is *oriented to current reality* (a catch-up read of what the group knows), inherits the
  operating fabric, and **earns standing by contributing + being corroborated over time.** Trust is a
  track record, not a grant.
- **Working** — ongoing contribution; the relationship (trust) deepens.
- **Leaving** — graceful exit: the departing member's contributions persist as history, but their
  *future authority* ceases (admission revoked; no longer a live corroborating source). Forced exit =
  removing a **compromised** member — where this meets poisoning-recovery (*leave + forget*).

Entering/leaving and forgetting are the same layer: the **boundaries and lifecycle of the collective** —
what comes in, what earns standing, what is let go. A healthy mind isn't just calibrated; it onboards,
closes relationships gracefully, and **forgets on purpose.**

---

## 8. Principle → mechanism → status

| Operating principle | Mechanism | Status |
|---|---|---|
| Access your ignorance / no self-set truth | derived confidence projection | **SHIPPED (Phase A)** |
| Corroboration not repetition | distinct-source count, diminishing returns, cap | **SHIPPED (Phase A)** |
| Stay in touch with current reality | P2P sync; decay | sync shipped; decay design-only (Phase C) |
| Everything is an intervention | salience write-discipline | design-only (Salience pillar) |
| Surface, don't prescribe (active inquiry) | tension-holding retrieval | design-only (Retrieval pillar) |
| Errors are data; reopen on evidence | dispute / supersede / signed confidence | dispute/decay design-only (Phase C) |
| The client owns; never usurp | escalation, admin-as-append, autonomy stance | partial (escalation design-only) |
| Forget on purpose | retract (soft) + federated epoch compaction (hard) | design-only |
| Membership lifecycle | identity (D0), admission, circles, genesis | design-only |
| Identity that survives ephemerality | structured `agent` identity | design-only (Phase B / D0) |

This operating model is the *why*; the confidence / salience / retrieval / identity / privacy mechanics
(see `hive-mind-confidence-design`) are its implementation. Build proportionately, lowest-risk-first,
and only when a real need exists — but let every mechanism be derivable from a principle here.
