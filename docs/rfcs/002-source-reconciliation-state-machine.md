# RFC 002 source reconciliation state machine

This is the durable implementation matrix for the source-adapter dispatch path.
A source item is scoped by `(adapter_name, source_file)`.

| State / transition | Durable state | Crash/interruption recovery | Visibility / invariants |
|---|---|---|---|
| Absent → candidate staged | `.mempalace/source-reconciliation/<hash>.json` is atomically written with `prepared`, old IDs, candidate generation, and all expected candidate IDs **before** any drawer write | A later run removes only the recorded candidate IDs and leaves the known-good old generation intact | Candidate rows carry `source_generation`; no old IDs are removed while `prepared` |
| Candidate staged → candidate durable | Every expected candidate row has the marker generation; marker remains `prepared` until all expected IDs exist | Missing/partial candidates are removed; old generation remains | Logical `(source_file, chunk_index)` identities are unique before any write |
| Candidate durable → old retired | Marker atomically changes to `committing` before deleting exact old IDs | A later run completes exact old-ID deletion, retaining the complete candidate generation | Never broadly delete by `source_file` after candidate writes |
| Old retired → absent marker (committed) | Old IDs gone; marker is removed only after retirement | A stale `committing` marker is idempotently completed | Exactly one current generation is retained |
| Tombstone | Prepared marker has no candidates; it changes to `committing` before deleting scoped drawer and closet rows | `prepared` preserves old data; `committing` finishes deletion | Deletion is `(adapter_name, source_file)` scoped |
| Skip/current | No marker and no mutation | Restart is a no-op | `is_current` sees only current source metadata |
| Adapter exception before commit | No marker or a `prepared` marker | In-process cleanup removes candidates; restart cleanup is authoritative | Existing durable generation is untouched |
| Privacy upgrade | New class is at least as restrictive as stored class | Normal reconciliation | Upgrade is allowed and stamped on every new row |
| Privacy downgrade | Rejected before extraction/writes unless a future RFC migration/audit protocol is implemented | No marker / no mutation | Existing classification cannot silently be weakened |
| Closets/index | Derived rows are replaced/deleted after a source generation commits, scoped by adapter and source | Reconciliation re-runs the scoped derived lifecycle | No stale closet row remains after update/tombstone |
| Routing | CLI options override config/adapter hints; config/adapter hints precede generic fallback | Stateless | Only adapters with `adapter_owns_routing` may emit routing hints |
| Invocation routes | CLI, MCP, and daemon reject explicit source plus legacy `mode` | Stateless | Source URI/options follow the same policy on each route |

**Privacy migration note:** RFC 002 currently defines a floor, not an audit-record
format for classification downgrades. Therefore this implementation rejects
`existing → less restrictive` transitions. A future migration must add an
auditable approval record before it may bypass that rejection.
