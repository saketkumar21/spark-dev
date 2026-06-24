# CDC-9 — (Deep) failure-mode tour

> **Break → Detect → Fix → Prove.** The capstone of the CDC track: a senior-level tour of how a
> **Postgres → Debezium (Kafka Connect) → Kafka → Spark → Iceberg** pipeline *fails in production*
> and how you recover. CDC-1…CDC-8 built the pipeline; this module steps back and asks the on-call
> questions — *what happens when the connector restarts? are events ordered? can they duplicate? is
> any of this exactly-once?* The through-line: **Kafka Connect is at-least-once with per-key
> ordering, so you design for replay + idempotency, not for magic exactly-once.**

- **Notebook:** [`cdc9_failure_modes.ipynb`](./cdc9_failure_modes.ipynb)
- **Toolkit used:** `common.cdc_helpers` (`seed_orders`, `register_connector`, `wait_for_connector`,
  `connector_status`, `read_cdc_events`, `op_counts`, `list_slots`, `teardown`, and the Connect REST
  helper `cdc._req` for `pause` / `resume` / `restart`).
- **Run against:** the CDC stack (`make cdc-up`) — Postgres at `localhost:5432`, Kafka Connect REST
  at `localhost:8083`, Kafka producers/admin on `localhost:29092`. Watch the connector live in
  **kafka-ui** at http://localhost:8080 (topic `dbz.public.cdc9_orders`, plus the internal
  `connect_offsets` / `connect_status` topics).
- **Time:** ~10–12 min. **Laptop-safe:** an 8-row table, bounded topic reads with timeouts, and a
  **Teardown** cell (start *and* end) that deletes the connector, drops the slot, drops the table,
  and deletes the data topic. Isolated names — connector `cdc9-orders`, table `cdc9_orders`.

> **Honesty note (the same stance as the SPK-2/3 OOM and KAF-5 modules).** This module is a
> **failure-mode *tour***, not ten separate breakages. The items that are **deterministic** —
> the connector lifecycle (**pause → resume → restart**) and **offset recovery** from the
> `connect_offsets` topic — we **run for real** and assert on. The items that need a real crash,
> a multi-key race, or a second runtime — **ordering across partitions, duplicate-on-crash,
> end-to-end exactly-once, Connect vs Debezium Server, slot/WAL risk** — we **describe precisely,
> with correct reasoning and snippets**, and tie to the module where each is demonstrated for real
> (CDC-5, CDC-7, KAF-1/5, STR-2). The notebook runs top-to-bottom under `nbconvert`.

---

## 1. The scenario

You own the CDC pipeline. It has worked for weeks. Then a deploy restarts Kafka Connect, a Postgres
failover blips the connection, an operator pauses the connector to do maintenance — and your phone
buzzes. The questions you must answer cold, at 3am:

1. After Connect restarts, does the connector **resume where it left off**, or re-snapshot / lose
   the changes that happened while it was down?
2. Are the change events **in order**? If two updates hit the same row, can the downstream MERGE
   apply them out of order?
3. Can the same change be delivered **twice**? If so, does my sink double-count?
4. Is any of this **exactly-once**, end to end, or am I lying to myself?

CDC-9 answers each one — demonstrating the deterministic ones and reasoning carefully through the
rest, because the wrong mental model here is what causes the 3am page in the first place.

## 2. Break it / explore — the failure modes

### (1) Connector lifecycle & offset recovery  *(REAL demo)*
Kafka Connect stores each source connector's progress — for Debezium, the **WAL position (LSN)** it
has emitted up to — in the internal **`connect_offsets`** Kafka topic, committed periodically and on
clean shutdown. So a **pause** or a **restart** is *not* a reset: on resume the connector reads its
stored offset and continues from the next change. The notebook proves this directly:

- register + snapshot (8 `r` events), then INSERT a row → one `c` event streams;
- **`PUT /connectors/cdc9-orders/pause`** → `connector_status` shows **PAUSED** (connector *and*
  task);
- INSERT 3 more rows **while paused** → they sit unread in the WAL; a topic drain shows the `c`
  count **unchanged** (the connector is not consuming);
- **`PUT /connectors/cdc9-orders/resume`** → back to **RUNNING**; after a short decode lag the
  topic drain shows **all 4** `c` events — the 3 changes made during the outage **streamed through
  on resume**, from the stored offset, not from scratch.

### (2) The restart endpoint  *(REAL demo)*
**`POST /connectors/cdc9-orders/restart`** restarts the connector instance (→ `204`, back to
RUNNING); **`?includeTasks=true&onlyFailed=false`** also restarts the task (→ `202`). This is the
first thing you try when a task is wedged — and because offsets are durable, a restart re-reads from
the committed LSN, so it is safe and non-destructive. The notebook prints the state before and after.

### (3) Connect vs Debezium Server  *(described)*
We deploy Debezium **inside Kafka Connect** (the brief mandates this — it mirrors most production
shops). The alternative is **Debezium Server**, a standalone runtime that sends change events
straight to a sink (Kinesis, Pub/Sub, Pulsar, an HTTP endpoint) **without a Kafka cluster or the
Connect framework**. Trade-off: Connect gives you the whole ecosystem — REST lifecycle, distributed
workers, offset/config/status topics, SMTs, converters, a fleet of sink connectors — at the cost of
running Kafka + Connect; Debezium Server is lighter to operate but you give up Connect's
distribution, the sink-connector catalogue, and the REST control plane this very module uses.

### (4) Ordering guarantees  *(described, ties to KAF-1)*
Debezium routes each table's events to one topic and **partitions by the primary key** (hash of the
key, exactly like KAF-1). Kafka guarantees order **within a partition**, so you get **per-key
ordering**: every change to row `id=42` arrives in commit order, always. What you do **not** get is
**global / cross-key ordering** — changes to different keys live in different partitions and may
interleave arbitrarily. For a CDC mirror this is the right contract: the MERGE for a given key
applies that key's changes in order; it never needs a global order.

### (5) Out-of-order / duplicate delivery  *(described, ties to KAF-5 / STR-2 / CDC-7)*
Kafka Connect is **at-least-once**. The danger window: the connector emits a batch of records to
Kafka, then crashes **before** committing the new offset to `connect_offsets`. On restart it resumes
from the *last committed* offset and **re-emits** the records after it — so downstream sees
**duplicates** (and, across partitions, possible reordering relative to a different key). There is no
at-least-once setting that removes this; the cure is a **downstream sink that absorbs replays** —
dedupe / upsert on a stable key with an **LSN-monotonic guard** so an older replayed change can't
overwrite a newer applied one. That is precisely the CDC-7 sink.

### (6) End-to-end exactly-once reasoning  *(described — the senior point)*
There is **no magic EOS** across `PG → Kafka → Spark → Iceberg`. Each hop is at-least-once. What you
*can* engineer is **effectively-once** by stacking idempotency at each layer:

| Layer | Mechanism | Module |
|-------|-----------|--------|
| Kafka Connect → Kafka | at-least-once + per-key order (offsets in `connect_offsets`) | **CDC-9** (here) |
| Kafka → Spark | Structured Streaming **checkpoint** = the durable offset store; safe restart | **STR-2** |
| Spark consumer | `isolation.level=read_committed` (ignore aborted txns) | **KAF-5** |
| Spark → Iceberg | idempotent **MERGE** keyed by PK, **LSN-monotonic guard** (older LSN ⇒ skip) | **CDC-7** |

The combination is the answer: *at-least-once delivery + an idempotent, LSN-guarded MERGE +
checkpointed offsets + read_committed* ⇒ each source change affects the Iceberg mirror **once**,
even though the wire may carry it twice.

### (7) Slot / WAL risk on prolonged outage  *(described, ties to CDC-5)*
The pause demo is safe because it's seconds. **Left paused (or failed) for hours**, the connector
stops advancing its slot's `restart_lsn`, so **Postgres retains every WAL segment since that LSN** —
unbounded disk growth, the CDC-5 pathology. The notebook lists slots and points at `retained_bytes`;
the fix lives in CDC-5 (monitor slot age, cap with `max_slot_wal_keep_size`, keep consumers healthy
or delete a dead slot).

## 3. Detect it

| Failure | Where you look | Signal |
|---------|----------------|--------|
| Connector / task down or wedged | `GET /connectors/<n>/status` (`connector_status`); kafka-ui | state `PAUSED` / `FAILED`, or task `FAILED` with a trace |
| Not making progress / lag | `connect_offsets` topic; consumer-group lag on the data topic | committed offset / LSN not advancing |
| Duplicates downstream | sink input: offsets produced **≫** distinct keys (the KAF-5 signature) | replayed records after a crash |
| Slot / WAL growth | `list_slots()` → `pg_replication_slots` | inactive slot with rising `retained_bytes` (CDC-5) |
| Out-of-order across keys | per-partition offsets in kafka-ui | only ever *across* partitions, never within one |

## 4. Diagnose

Every item above traces back to one fact about the runtime: **Kafka Connect provides at-least-once
delivery with per-key (per-partition) ordering, and persists its progress as offsets in
`connect_offsets`.** From that single property everything follows — pause/restart resume from the
stored offset (not from scratch); duplicates appear only in the emit-then-crash-before-commit window;
ordering holds within a key but not across keys; and a stalled connector pins WAL because its slot's
`restart_lsn` stops moving. There is no stronger guarantee hiding in a config flag; the strength you
want is built **downstream**, by making the sink idempotent and LSN-aware.

## 5. Fix it

- **Design for at-least-once + idempotency.** Make the sink absorb replays: upsert/`MERGE` on the
  primary key, guarded by a **monotonic LSN/`ts_ms`** so an older (replayed) change never overwrites
  a newer applied one. (CDC-7.)
- **Lean on the checkpoint, don't hand-roll transactions.** For Kafka → Spark → Iceberg, the
  Structured Streaming **checkpoint** is the offset store that makes restart exactly-once-into-the-
  sink; pair it with `read_committed`. (STR-2, KAF-5.) Don't attempt Kafka EOS transactions across
  this boundary.
- **Operate the connector with offsets in mind.** `pause`/`resume`/`restart` are safe because
  offsets are durable — use them freely for maintenance and to clear wedged tasks. Keep config in
  version control (`PUT .../config` is idempotent).
- **Bound outages.** A paused/failed connector retains WAL — alert on slot `retained_bytes` and task
  state, and cap WAL with `max_slot_wal_keep_size` (CDC-5).

## 6. Prove it

The notebook prints the connector state at each lifecycle step and the topic `op_counts` around the
pause window:

| Step | Connector state | Topic `op_counts` | What it proves |
|------|-----------------|-------------------|----------------|
| snapshot + 1 live insert | RUNNING | `{r: 8, c: 1}` | snapshot then stream (CDC-2 recap) |
| **paused**, 3 rows inserted in PG | **PAUSED** | `{r: 8, c: 1}` (unchanged) | a paused connector consumes nothing |
| **resumed**, drain after lag | **RUNNING** | `{r: 8, c: 4}` | the 3 outage-time changes **streamed on resume** — offset recovery from `connect_offsets`, not a re-snapshot |
| **restart** | RUNNING (`204`) | — | restart re-reads from the committed LSN; non-destructive |

The `c` count going **1 → (still 1 while paused) → 4 after resume** is the offset-recovery proof: the
connector picked up exactly the changes it had not yet emitted, with no gap and no re-snapshot.

## 7. Takeaways & "in real production…"

- **Per-key ordering is the contract.** Build downstream logic that needs only per-key order; never
  assume global order across a CDC topic's partitions.
- **At-least-once + idempotency beats chasing once-only delivery.** Replays you can dedupe; lost
  changes you can't. The robust CDC sink is an LSN-guarded, PK-keyed MERGE (CDC-7).
- **There is no end-to-end EOS switch** — effectively-once is *assembled*: Connect offsets +
  Streaming checkpoint + `read_committed` + idempotent MERGE. Know which layer gives which guarantee.
- **Connector lifecycle is safe by design** (offsets are durable) — but a *prolonged* outage is a
  **WAL-retention incident** (CDC-5). Monitor task state, lag, and slot `retained_bytes` together;
  any one going wrong is your page.
- **Connect vs Debezium Server** is a deployment trade-off (ecosystem & control plane vs operational
  weight), not a correctness one — both are CDC, both at-least-once.

## 8. Teardown

The notebook ends with a **Teardown** cell that `teardown()`s the connector (delete connector → drop
slot → drop table → delete data topic). `make clean` clears any local `.tmp/` state. A start-of-
notebook teardown makes re-runs idempotent.
