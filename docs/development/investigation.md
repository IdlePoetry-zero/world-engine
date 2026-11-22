# Investigation evidence review

## Opening scenario design

Each opening investigation can use a local witness and an independent physical record. The witness's statement and archival observation name the same subject and historical tick. Both sources preserve their original time when the player obtains them. Receipt time comes from the action that grants access.

| Background | Witness statement | Independent record |
| --- | --- | --- |
| Low magic feudal | The levy clerk heard the ferry gate was closed on the first day of the disputed levy. | The ferry slate records an open service shift on that date. |
| Modern city | A local dispatcher recalls the public depot closing before the replacement route began. | The maintenance incident cabinet records the depot operating during the same shift. |
| Long life cultivation | A water courier recalls the distribution station closing before the annual compact. | The spring measure stone records an open distribution shift on that date. |

Each pack can seed its witness claim and archival source as distinct authored sources. A claim is revealed only through its holder's on-request disclosure policy. An archive source is recorded only after the player reaches and successfully examines its physical item. Both routes preserve the authored source identity and their shared historical tick.

This note describes the bounded R07 evidence comparison currently implemented. It does not mark the full investigation requirement complete.

## Typed source contract

`KnowledgeRecord.fact` supports two closed predicates:

- `entity_status`: a status string and the exact world tick it describes.
- `entity_location`: a place UUID and the exact world tick it describes.

Example:

```json
{
  "predicate": "entity_location",
  "value": "<place-uuid>",
  "effective_tick": 3600
}
```

The source record's `subject_id` identifies the entity. No rule reads a natural-language claim to infer a predicate or value.

The `weigh_evidence` action takes two record IDs:

```json
{
  "kind": "weigh_evidence",
  "observation_id": "<observation-record-uuid>",
  "claim_id": "<fallible-claim-record-uuid>"
}
```

The server requires an actor-accessible `observation` and a `rumor`, `claim`, or `belief`; both must describe the same subject, predicate, and exact non-future `effective_tick`. Their recursively resolved source roots must be disjoint, and a prior evidence assessment cannot be reused as primary evidence. Event and record IDs are stored in the resulting private assessment record's `source_refs` and typed `assessment` field.

The action takes 300 world seconds. It records either `corroborated` or `conflict`, meaning only that the two independent records agree or disagree. It does not decide whether either assertion is true. The reducer repeats the typed comparison and provenance checks during replay. A forged finding, altered source list, inaccessible source, reused assessment, cycle, or shared source root is rejected.

## Explicit NPC claim disclosure

A typed fallible claim may be held privately by a character with `disclosure_policy: "if_asked"`. The closed `ask_npc_claim` action takes the visible NPC ID, a player-known subject ID, and optional predicate/effective-tick filters. It is available only for an active, visible person co-located with the player. The rule selects exactly one eligible source; no match or an ambiguous match has the same generic response.

On success, the server appends a private actor-owned copy. It preserves the source claim, confidence, summary, typed fact (including its original historical `effective_tick`), source-event ID, and provenance; the copy points to the exact NPC-held source record. A typed disclosure receipt records speaker, recipient, location, current receive tick, source ID, and policy version. Replay checks the source opt-in, access, visibility, co-location, and exact copy. Disclosure itself does not advance world time.

The roleplay target resolver can choose this action from the player's explicit request and the public scene. It never receives NPC-held claim text. After disclosure, a later conversation with that same NPC can include the copied claim as shared context; it is not included when speaking to a different character. Ordinary dialogue still does not disclose private knowledge.

## Player access and current source paths

The action compiler receives only typed records returned by the actor-scoped knowledge builder. It is given record IDs, subject IDs, type, claim text, typed predicate and receive tick. Internal source references are not sent to the model. The rule checks the actor's access again before committing.

Successful exploration writes an actor-private location observation for the discovered entity at the action's resolved tick. A successful local improvised examination writes an actor-private location or status observation. Both cite their typed discovery/check event as source provenance.

An item may contain one author-only `historical_evidence` component:

```json
{
  "evidence_id": "<stable-authored-uuid>",
  "subject_id": "<visible-subject-uuid>",
  "claim": "The tablet records the depot as operating during the old winter shift.",
  "fact": {
    "predicate": "entity_status",
    "value": "active",
    "effective_tick": -3600
  },
  "provenance": [
    {"kind": "pack", "reference_id": "<unique-authored-archive-source>"}
  ]
}
```

Only registered items may carry this component. The subject must exist, and both the item and subject must be visible in the actor's player projection. A hidden item must first be discovered and the item must be local to the examiner. The existing `improvised_check` also requires a registered investigation/local-knowledge skill and a passing roll. The artifact data is never included in the action compiler's scene. On success, a private observation copies its claim, typed fact, and authored provenance, references the stable artifact ID, and cites the check audit event. Its `received_tick` is the check's resolved world tick; its `fact.effective_tick` remains authored, including negative pre-opening ticks. On failure, the event contains no artifact ID or digest, no observation is added, and the existing time/needs effects still resolve. The reducer and command-batch validators verify the item digest and exact observation on replay and backup restore.

Character creation accepts free-text secrets and ordinary dialogue records free-text memories; neither is promoted to a typed investigation claim. A pack can seed an NPC-held `KnowledgeRecord` with the typed `fact` shape and `disclosure_policy: "if_asked"`. There is no dedicated form for authoring a typed witness claim or historical artifact in the player-facing editor today.

The standalone record shape for such a fallible NPC claim is:

```json
{
  "id": "<knowledge-record-uuid>",
  "holder_id": "<npc-uuid>",
  "subject_id": "<witness-uuid>",
  "kind": "rumor",
  "disclosure_policy": "if_asked",
  "visibility": {"policy": "private"},
  "claim": "A trader says the witness was at the old bridge.",
  "source_event_id": "<real-source-event-uuid-if-one-exists>",
  "provenance": [
    {"kind": "pack", "reference_id": "<stable-authored-source-reference>"}
  ],
  "fact": {
    "predicate": "entity_location",
    "value": "<place-uuid>",
    "effective_tick": 3600
  }
}
```

The quoted claim and typed predicate are separate: the rule compares only the typed fields. Use an actual source-event ID only when the authored source has one. Otherwise, retain the stable authored pack/source reference in provenance; do not invent a source event ID. The record becomes actor-owned evidence only after the on-request disclosure event commits.

## Remaining R07 acceptance work

The player-state projection exposes `available_case_anchors` with the case ID, anchor ID, title and brief. The actor must be active and at the active item, with an existing observation if the anchor is hidden. `case_files` contains only records owned by the actor and strips author-only fields. The focused projection regressions passed before the case domain integration entered the shared tree.

Live commits bind a case event to the normalized action proposal and actor. Genesis replay looks up the original proposal only for case-event batches. A backup deliberately excludes original request bodies. Archive validation, backup import and replay of a verified restored batch still check event context, case state, evidence and any authored consequence. Both eager and streaming restore regressions reject an incorrect live opening proposal and restore the valid case to the same genesis replay digest.

## Private case files and alternate closure routes

An authored item may carry a hidden `investigation_case` definition. The player projection exposes only the local available anchor's ID, case ID, title and brief. It does not send route requirements, source roots, or consequence policy to the action compiler. A file opens only while its active item anchor is visible and local to the actor.

The authored definition uses OR across routes and AND within each route:

```json
{
  "case_id": "<stable UUID>",
  "title": "冬宴失铃案",
  "brief": "查明冬宴前后铃铛的去向。",
  "routes": [
    {
      "route_id": "archive-ledger",
      "suspect_id": "<character UUID>",
      "finding": "supports",
      "requirements": [
        {
          "subject_id": "<entity UUID>",
          "predicate": "entity_status",
          "effective_tick": -3600,
          "finding": "corroborated",
          "observation_provenance": [{"kind": "pack", "reference_id": "pack.case.archive"}],
          "claim_provenance": [{"kind": "pack", "reference_id": "pack.case.witness"}]
        }
      ]
    },
    {
      "route_id": "bell-ringer-cleared",
      "suspect_id": "<another character UUID>",
      "finding": "rules_out",
      "requirements": ["... another typed source requirement ..."]
    }
  ],
  "wrong_suspect_effect": {"dimension": "trust", "delta": -12}
}
```

Each requirement matches one attached actor-owned assessment with exact subject, predicate, historical tick, comparison finding and authorized observation/claim provenance roots. The source sides must remain independent. One assessment cannot satisfy multiple requirements in the same route. A route completes when every requirement matches; multiple routes can provide alternative paths to a conclusion. Runtime evaluation reads typed records only and never asks a model whether a route is complete.

The action path supports `open_investigation_case`, `attach_case_evidence`, and `close_investigation_case`. The private file stores the stable case ID, owner, anchor, public title/brief, definition hash, revision and attached assessment IDs. It remains private after closure. The hash prevents author edits from silently changing an open file's closure policy. Closure computes one of `supported`, `disconfirmed`, `contested`, or `unresolved` from completed authored routes for the selected suspect. Route IDs and source policy do not appear in player-facing file projections or response text.

A public accusation requires an explicit player action with a visible, co-located active suspect and an applicable completed route. An authored `wrong_suspect_effect` applies only when a completed `rules_out` route disconfirms that selected suspect. It changes the directed suspect-to-player relation through registered relation events, using compare-and-swap for an existing relation or deterministic creation when none exists. The authored delta is bounded to the relation's `[-100, 100]` range. Private filing has no relationship effect. A state-aware batch validator rechecks ownership, evidence ancestry, route outcomes, public-accusation eligibility and the exact relation change during commit, replay and backup/import validation.

An actor-owned observation and assessment remain usable if their original item later becomes unavailable: the file references retained private record IDs and replay checks their source chain, not the current physical item's status.

### Destroying a physical source

`destroy_historical_evidence` is available only when the player explicitly asks to destroy a local, visible, active item that carries a valid registered historical artifact. The compiler receives only eligible target IDs and names, not artifact claims, provenance or digests. The event `core.historical_evidence_destroyed.v1` records the actor, location, world tick, artifact ID and exact artifact digest. It marks the physical item `destroyed` without advancing time, removing its component or changing any knowledge/case record. Later inspection is rejected because the item is no longer active; already acquired copies and case references remain valid. The event creates no NPC memory or relationship change. Commit, replay and archive validators bind live events to the exact action proposal and recheck locality, visibility, item status and digest.
