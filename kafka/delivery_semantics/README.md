# KAF-5 — Delivery semantics (at-least-once vs exactly-once)

> **Break → Detect → Fix → Prove.** Every messaging pipeline lands on one of three delivery
> guarantees, decided by *where you commit* and *whether writes are idempotent*:
> **at-most-once** (commit before processing → may **lose**), **at-least-once** (process then
> commit → may **duplicate**), and **exactly-once** (idempotent producer + transactions +
> `read_committed` consumer + idempotent sink → no loss, no dupes). This module makes duplicates
> *appear*, then walks the machinery that removes them.

- **Notebook:** [`kaf5_delivery.ipynb`](./kaf5_delivery.ipynb)
- **Toolkit used:** `common.kafka_helpers` (`ensure_topic`, `produce_events`, `topic_end_offsets`,
  `delete_topic`) + raw `kafka-python` `KafkaProducer` / `KafkaConsumer` for idempotence and
  `isolation_level` control.
- **Run against:** the Kafka broker (`make up`) — producers/consumers use the host listener
  `localhost:29092`; Spark (mentioned for the end-to-end recipe) reads via `kafka:9092`. Inspect
  the topic live in **kafka-ui** at http://localhost:8080.
- **Time:** ~10 min. **Laptop-safe:** a bounded ~1,000-message batch, no infinite loops; the topic
  is deleted at teardown.

---

## 1. The scenario

A payments service publishes one event per charge. The network hiccups, a `send()` looks like it
failed, and the retry logic fires it **again** — so the same logical charge lands on the topic
**twice**. Downstream, a naive consumer sums the amounts and the daily revenue is **overstated**.
Was the charge really double-spent, or did we just *deliver* it twice? And how do production
systems guarantee a charge is counted **exactly once**?

That is the whole subject of delivery semantics: the guarantee isn't a single switch, it's the
**combination** of producer config, the broker's transaction support, the consumer's isolation
level, and whether your sink can absorb a replay without harm.

## 2. Break it — at-least-once produces duplicates

A non-idempotent producer (`enable_idempotence=False`) that "retries" by sending the *same logical
event* a second time is exactly what at-least-once looks like on the wire:

- `produce_events(TOPIC, 1000)` writes ids `0..999` once.
- A second pass **re-sends a slice of those same ids** (the "retry after a false-failure").
- `topic_end_offsets(TOPIC)` now reports **more offsets than distinct ids**, and a consumer that
  groups by id finds ids with **count = 2**.

The bytes are real and the count is deterministic, so this part **runs** top-to-bottom.

## 3. Detect it

| Where | What you see |
|-------|--------------|
| `topic_end_offsets(topic)` | total offsets **>** the number of distinct logical ids you produced |
| consume-all + group-by-id count | a set of ids with **count ≥ 2** — the duplicates, by value |
| **kafka-ui** → topic → **Messages** | the same id appears in more than one record |

The signature of at-least-once is simply: **offsets produced > distinct ids**, i.e. the log
contains replays. Lag (KAF-2) tells you if you're keeping up; this tells you if you're double-counting.

## 4. Diagnose — the three guarantees

The guarantee is determined by the **order of commit vs processing** and by **sink idempotency**,
not by any single flag:

| Guarantee | How it arises | Failure mode | What it needs |
|-----------|---------------|--------------|---------------|
| **At-most-once** | commit the offset **before** processing | crash after commit, before work → message **lost** | nothing extra (it's the unsafe default of auto-commit) |
| **At-least-once** | process **then** commit; producer retries on uncertain sends | a retry or a redelivered offset → **duplicate** | manual commit after work (KAF-2); dedupe downstream |
| **Exactly-once (EOS)** | idempotent producer **+** transactions **+** `read_committed` consumer **+** idempotent sink | none for in-scope failures (replays are absorbed/aborted) | all four pieces together |

Key insight: **exactly-once is not "deliver once"** — messages may still be delivered more than
once at the transport layer. EOS means each message *affects the result* once, because dedupe and
transactional atomicity make replays harmless.

## 5. Fix it — the four pieces of exactly-once

### (A) Idempotent producer — dedupes producer **retries** within a session  *(runnable config)*
`KafkaProducer(acks="all", enable_idempotence=True)` stamps every batch with a **producer id +
monotonic sequence number** per partition. If a retry re-sends a batch the broker already wrote,
the broker recognises the sequence and **drops the duplicate** — so a *transport* retry no longer
duplicates the record. (It does **not** dedupe two separate `send()` calls for the same business
event — that's the application's job, see (D).) The notebook configures this and explains it; on
`kafka-python` forcing an internal retry deterministically inside a notebook is unreliable, so the
mechanism is **described precisely** rather than timing-raced — same honesty stance as the SPK-2/3
OOM modules.

### (B) Transactions — atomic, all-or-nothing writes  *(described; recipe shown)*
A transactional producer (`transactional_id="..."`, then `init_transactions()` /
`begin_transaction()` / `commit_transaction()` / `abort_transaction()`) writes a group of records
that become visible **together or not at all**. Aborted records are physically in the log but
marked aborted. This is what lets "consume → transform → produce" be one atomic step
(`send_offsets_to_transaction` ties the consumed offsets into the same transaction).

### (C) `read_committed` consumer — never sees aborted/uncommitted records  *(runnable)*
`KafkaConsumer(isolation_level="read_committed")` filters out records from open or aborted
transactions; a `read_uncommitted` consumer (the default) sees everything. The notebook creates
both and contrasts what each returns, so the isolation level's effect is **observable** on
committed data even where we don't drive a live abort.

### (D) Idempotent sink — makes any surviving replay harmless  *(the practical EOS in this stack)*
Even with (A)–(C), the robust production pattern is to **make the write idempotent**: dedupe on a
stable key (event id / `(partition, offset)` / LSN) so a replay overwrites rather than
double-counts. For **Kafka → Spark → Iceberg**, you do not hand-roll Kafka transactions — you lean
on **Structured Streaming's checkpoint + an idempotent `MERGE`/upsert into Iceberg**, which gives
**effectively-once** into the lakehouse. That recipe is built in **STR-2**.

## 6. Prove it

Duplicate count, naive resend vs deduped:

| State | offsets on topic | distinct ids | duplicate ids | revenue sum |
|-------|-----------------:|-------------:|--------------:|------------:|
| at-least-once (naive resend) | > 1000 | 1000 | a non-empty set | **overstated** |
| deduped sink (`dropDuplicates` / keyed MERGE) | > 1000 | 1000 | — (collapsed) | **correct** |

The topic still physically contains the replays — the proof is that an **idempotent read**
(dedupe by id) recovers the correct distinct count and the correct revenue, exactly as an
idempotent Iceberg `MERGE` would. Same data on the wire, harmless on landing.

## 7. Takeaways & "in real production…"

- **The guarantee is the *combination*,** not a flag: commit order decides loss vs duplicate;
  idempotency decides whether duplicates matter.
- **Prefer at-least-once + an idempotent sink** over chasing perfect once-only transport — it's
  simpler, cheaper, and survives more failure modes. Duplicates you can dedupe; lost data you can't.
- **Full Kafka EOS** = idempotent producer **+** transactions **+** `read_committed` **+** idempotent
  sink, all four. Use it when the sink genuinely can't be made idempotent.
- **For Kafka → Spark → Iceberg, don't hand-roll transactions** — use the streaming **checkpoint +
  idempotent MERGE** (STR-2) for effectively-once into the lakehouse.
- **Alert on `offsets ≫ distinct keys`** in a sink's input as a duplicate-rate signal, the way you
  alert on consumer lag (KAF-2).

## 8. Teardown

`delete_topic(TOPIC)` removes the topic. `make clean` also clears any local Kafka/`.tmp/` state.
