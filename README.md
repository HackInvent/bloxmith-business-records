# Business Records

Look up explicit local inventory/order/reference data and apply reviewed changes
with exact proposal digests and optimistic revisions. This first adapter is a
**local demonstration catalog**, not a universal ERP connector or an automatic
synchronizer. It never contacts an external service.

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

[![Business Records — controlled local reference data](media/thumbnail.webp)](media/cover.png)

## Ports

| Direction | Name | Meaning |
| --- | --- | --- |
| Input | `request` | JSON `list`, `get`, `match`, `propose_update` or `get_receipt` |
| Input | `decision` | Exact proposal ID/digest and explicit boolean approval |
| Output | `records` | Bounded page of normalized objects with local source IDs/revisions |
| Output | `proposal` | Stored immutable before/after preview, expiry and SHA-256 digest |
| Output | `receipt` | Emitted only after an approved local update commits successfully |
| Output | `status` | Correlated outcome, including refusals and receipt inspection |

Each event triggers independently. Cached requests are not replayed when a decision
arrives. Manual execution with both inputs populated, or a multi-event activation,
is refused before business work; the model uses the runtime's per-event policy.
Empty inputs are skipped. Receipts are facts about this node's SQLite catalog,
never evidence of a remote ERP update.

## Configure the local source

`seed_data` is a trusted JSON array, imported atomically on the first request:

```json
[
  {"object_type":"inventory","source_id":"item-1","data":{"sku":"REF-1","name":"Sample item","stock":12}}
]
```

Set `field_mapping` to `{"reference":"sku","label":"name","quantity":"stock"}`
and `match_fields` to `["reference"]`. Mapping is explicit canonical field → source
field, not a nested path/expression. Unmapped fields are not emitted; missing
fields are omitted, not replaced by zero. Two canonical fields cannot alias one
source field. A type/ID pair uniquely identifies a source object. IDs are not
mutable. No real business data or secrets are included by default.

The seed fingerprint is stored per `namespace`. Changing seed data later refuses
access rather than overwriting edited records; select a new namespace for another
dataset. Changing the graph or restarting a Run never reseeds an existing partition.
Each node's namespaces are private to that node; they are not shared across blocks.

## Read and match

```json
{"request_id":"lookup-1","operation":"match","object_type":"inventory","field":"reference","value":"REF-1","limit":20}
```

`get` instead takes `source_id`. `list` takes only type and optional `limit`/`after`.
Pages contain up to 100 objects, ordered by source ID; pass `next_after` as `after`
to continue. Matching is exact, case-sensitive and type-sensitive: `true`, `1`
and `"1"` are distinct. Multiple matches remain multiple records, never an invented
unique resolution. Pagination is not a snapshot across concurrent updates.

## Propose, review and apply

Writes default to disabled and `writable_fields` defaults to empty. Choose permitted
canonical fields and enable `allow_writes` only for this local dataset when needed.

```json
{"request_id":"update-1","operation":"propose_update","object_type":"inventory","source_id":"item-1","expected_revision":1,"changes":{"quantity":15}}
```

Review the emitted proposal, then send on `decision`:

```json
{"request_id":"approval-1","proposal_id":"update-1","digest":"<exact 64-character digest from proposal>","approved":true}
```

The decision must match the stored immutable proposal and current permission/mapping
policy, be unexpired and target the same revision. An accepted update and its receipt
commit in one SQLite transaction. A competing update at the same revision loses
without changing data. `approved:false` closes the proposal without mutation.
Repeating an applied/rejected decision emits only status, not a second receipt.
Reusing a proposal request ID with changed content is refused. A digest identifies
the approved content; it is not a signature or proof of a human's identity. Use
an upstream authorized review path, not untrusted document text, for decisions.

The block does not claim exactly-once graph delivery. A crash after the database
commit but before output publication can lose the receipt event. Inspect it using
`{"request_id":"check-1","operation":"get_receipt","proposal_id":"update-1"}`.
The retained receipt is returned under `status.receipt`; this recovery read does
not re-emit the business `receipt` output. No network retry or mutation happens.

## Bounds and storage

- Source data: at most 10,000 records, 64 fields each, 4 MiB of initial JSON.
- Request/record size: configurable 1–256 KiB; JSON depth is limited to 24.
- Result page: maximum 100 records and 4 MiB; no unbounded exports or SQL input.
- Proposals: at most 20,000 retained per namespace (10,000 default), no automatic
  purge/expiry deletion that would silently remove replay protection.
- Approval validity: 30 seconds–24 hours; 10 minutes by default. Expiration is
  checked when a decision arrives and never performs an action on its own.
- SQLite: at most 16,384 pages (normally 64 MiB), two-second busy wait, full
  synchronous transactions and owner-only file creation. The framework supplies
  the node-owned directory via `get_block_storage_dir()`; there is no guessed path.

Run/Stop/Reset preserve `records.sqlite3`. Cancel before commit rolls back; an
already committed update is not undone by Stop. Corrupt/unknown schemas fail
visibly without resetting records. This is neither encrypted storage nor an OS
sandbox. Read requests may initialize a new local partition but perform no business
mutation after initialization. Simulation executes explicit decisions just as
Active Runtime does: it is not a dry-run flag.

## UI and tests

The same settings appear in the responsive modal and inspector. Source data and
limits are under advanced settings, drafts stay local until Apply, and changes
take effect on the next loaded Run. English/French catalogs and all ES modules/CSS
are release-scoped and owned by this repository.

Owned tests exercise real SQLite, competing revisions, replay, rejection, expiry,
cancel rollback, seed changes, storage limits, both runtimes, all package origins
and real responsive browser surfaces. They use only disposable data. The proprietary
framework/test harness is not bundled here. No remote ERP certification is claimed.
