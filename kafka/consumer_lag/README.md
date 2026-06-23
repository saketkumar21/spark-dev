# KAF-2 — Consumer lag & offset semantics

> **Break → Detect → Fix → Prove.** Producers append to a topic; consumers read and **commit**
> how far they've gotten. The gap between them — **lag** = `end offset − committed offset` — is
> the headline health metric for any consumer. Lag creeping up means your consumers can't keep
> pace. *How* you commit (auto vs manual, before vs after processing) decides what happens to a
> message when a consumer crashes: lose it, or reprocess it.

- **Notebook:** [`kaf2_consumer_lag.ipynb`](./kaf2_consumer_lag.ipynb)
- **Toolkit used:** `common.kafka_helpers` (`ensure_topic`, `produce_events`, `topic_end_offsets`,
  `consumer_group_lag`, `delete_topic`) + raw `kafka-python` `KafkaConsumer` for commit control.
- **Run against:** the Kafka broker (`make up`) — producers/consumers use the host listener
  `localhost:29092`; inspect lag live in **kafka-ui** at http://localhost:8080 → topic → **Consumers**.
- **Time:** ~10 min. **Laptop-safe:** a bounded 2,000-message batch, no infinite loops; the topic
  is deleted at teardown.

---

## 1. The scenario

An orders service publishes ~2,000 events to a Kafka topic in a burst. A downstream consumer
group (`kaf2-app`) reads them to update a dashboard — but it processes each message slowly, so it
only gets through part of the backlog before the on-call engineer checks in. The dashboard is
stale. *How far behind is it, and is anything being lost?*

Two questions, two halves of this module:
1. **How far behind = lag.** Measure the gap between what producers wrote and what the group committed.
2. **What happens on a crash?** That depends entirely on *when* offsets get committed.

## 2. Break it — fall behind, then watch a crash

- `ensure_topic(TOPIC, 1)` + `produce_events(TOPIC, 2000)` puts a full backlog on the topic.
- A `KafkaConsumer(group_id="kaf2-app")` polls and commits only **~500** of the 2,000 messages,
  then stops — simulating a slow/under-provisioned consumer.
- `consumer_group_lag("kaf2-app", TOPIC)` now reports a large **lag** (~1,500): the committed
  offset trails the end offset.

Then we expose the offset-commit hazard with a second consumer:
- A consumer reads a batch **without committing**, then "crashes" (we close it).
- A **new** consumer with the *same* group id starts where the last *commit* left off — so it
  **re-reads** the uncommitted messages. That's a **duplicate / reprocess**.

## 3. Detect it

| Where | What you see |
|-------|--------------|
| `consumer_group_lag(group, topic)` | per-partition `{committed, end, lag}`; the `lag` is the number this module drives to ~0 |
| **kafka-ui** → topic `kaf2_orders` → **Consumers** → `kaf2-app` | the same lag per partition, updated live |
| `topic_end_offsets(topic)` | the producer high-water mark (the `end` side of the lag subtraction) |

Lag is the universal "are my consumers keeping up?" signal — flat/zero is healthy, steadily
rising means consumers are falling behind producers.

## 4. Diagnose — offset commit semantics

The committed offset is a **promise**: "everything below this, I'm done with." When that promise
is made relative to your processing decides the failure mode.

| Commit strategy | Order of operations | On a crash | Guarantee |
|-----------------|---------------------|------------|-----------|
| **Auto-commit** (`enable_auto_commit=True`) | poll → commit on a timer → process | offsets may be committed **before** processing finished → those messages are **never reprocessed** → **lost** | at-most-once (risk of loss) |
| **Manual commit after processing** | poll → process → `commit()` | crash before commit → next consumer **re-reads** them → **duplicates** | **at-least-once** |

So lag isn't just a speed metric — paired with the commit strategy it tells you whether a restart
will silently drop work or safely redo it. At-least-once (reprocess) is almost always preferable
to at-most-once (loss): duplicates you can dedupe; lost data you can't recover.

## 5. Fix it

| Fix | How | Why |
|-----|-----|-----|
| **Commit after processing** | `enable_auto_commit=False`; call `consumer.commit()` only once a batch is durably handled | turns silent loss into safe reprocess → **at-least-once** |
| **Idempotent sink** | dedupe on a stable key / offset / event id when writing (e.g. `MERGE` into Iceberg, keyed upsert) | makes reprocessing harmless → effective **exactly-once** end-to-end (ties to **STR-2**) |
| **Right-size consumers & partitions** | enough partitions and consumer instances that throughput ≥ producer rate | keeps lag near 0 so you never build a dangerous backlog |

## 6. Prove it

`consumer_group_lag` before vs after **draining** the group (read & commit the rest of the backlog):

| State | committed | end | lag |
|-------|----------:|----:|----:|
| after partial read (broken) | ~500 | 2000 | **~1500** |
| after draining (fixed) | 2000 | 2000 | **~0** |

Lag collapsing to ~0 is the proof the group caught up. In the duplicate demo, the proof is the
opposite direction: the re-reading consumer returns messages the previous (uncommitted) consumer
had already seen — showing exactly why commit-after-processing + an idempotent sink is the
production pattern.

## 7. Takeaways & "in real production…"

- **Alert on consumer lag.** Rising lag is the earliest warning that consumers can't keep up;
  flat-zero is healthy. (Per-partition lag also surfaces hot-partition skew — see **KAF-1**.)
- **Choose auto vs manual commit deliberately.** Auto-commit is convenient but risks **loss** on
  crash; manual commit *after* processing gives **at-least-once**.
- **At-least-once + idempotent sink = effective exactly-once.** Make reprocessing harmless rather
  than chasing perfect once-only delivery (continued in **STR-2**).
- **Right-size for throughput.** Enough partitions and consumers that you drain as fast as you
  fill, so lag never grows into an outage.

## 8. Teardown

`delete_topic(TOPIC)` removes the topic. `make clean` also clears any local Kafka/`.tmp/` state.
