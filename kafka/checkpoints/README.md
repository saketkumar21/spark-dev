# STR-2 — Idempotency, checkpoints & restart

> **Break → Detect → Fix → Prove.** A streaming job must survive restarts **without losing or
> double-processing data**. The Structured Streaming **checkpoint** stores the Kafka offsets it has
> committed (plus any operator state); on restart the query resumes from exactly those offsets.
> Pair that with an **idempotent sink** (Iceberg) and you get effectively **exactly-once** ingestion
> across crashes, redeploys, and re-runs.

- **Notebook:** [`str2_checkpoints.ipynb`](./str2_checkpoints.ipynb)
- **Toolkit used:** `common.kafka_helpers` (`ensure_topic` / `produce_events` / `topic_end_offsets` /
  `delete_topic`), `common.iceberg_meta` (`table_health` — the "Prove it"), `common.spark_session`.
- **Run against:** the unified Spark server (`make up`). Producers/admin talk to Kafka on
  `localhost:29092`; Spark reads on the internal listener `kafka:9092`. Spark UI at
  http://localhost:4040; inspect the topic live in kafka-ui at http://localhost:8080.
- **Time:** ~10 min. **Laptop-safe:** bounded produces (1000 + 500 events) and **`trigger(availableNow=True)`**
  reads — Spark drains all available data and **stops on its own** (never an unbounded stream). The
  checkpoint and Iceberg table live under `.tmp/`; the **Teardown** cell deletes the topic and drops
  the table, and `make clean` clears the checkpoint.

---

## 1. The scenario

A streaming job tails a Kafka topic and writes into an Iceberg table. It will be **restarted** all
the time — deploys, config changes, node failures, an operator pressing stop. The question every
data engineer must answer: *after a restart, does it pick up exactly where it left off, or does it
lose data / reprocess and create duplicates?*

The answer is the **checkpoint**. On every micro-batch Structured Streaming atomically commits the
Kafka **start/end offsets** for that batch (and any state) to `checkpointLocation` **before** the
batch is considered done. On restart it reads the last committed offsets and resumes from there —
data already written is not re-read, and data not yet read is not skipped. Point the same query at
the **same** checkpoint and restart is safe; point it at a **new/empty** checkpoint and it has no
memory of what it processed.

We demonstrate this the way it actually behaves in production: a sequence of **bounded
`availableNow` runs that all share ONE checkpoint**. Each run is a "restart".

## 2. Break it / contrast — what a lost checkpoint does

The "break" here is the mistake people make under pressure: *"the stream is stuck, let me just wipe
the checkpoint and restart it."* A fresh/empty checkpoint with `startingOffsets=earliest` has no
committed offsets, so the query **re-reads the topic from the beginning** and **reprocesses every
event** — duplicates in the sink. The notebook shows this directly: a second query with a *different*
`checkpointLocation` re-ingests all 1000 rows into a scratch table.

```python
# DON'T do this to "reset" a healthy stream — it reprocesses everything:
(parsed.writeStream.format("iceberg")
   .option("checkpointLocation", ".tmp/checkpoint_str2_BAD")   # new, empty checkpoint
   .trigger(availableNow=True).toTable(SCRATCH))               # startingOffsets=earliest -> from 0
```

The checkpoint is precisely what makes restart safe — deleting it throws that safety away.

## 3. Detect it — offsets per batch & row counts

Two complementary tells:

- **`q.lastProgress`** (and `q.recentProgress`) — after each run, the `sources[0].startOffset` and
  `endOffset` show which Kafka offsets that run consumed. On a resuming run the start offset equals
  the previous run's end offset; on a no-new-data run start == end and `numInputRows == 0`.
- **Row counts / `table_health`** — `SELECT COUNT(*), COUNT(DISTINCT id)` on the sink. The two
  numbers staying equal (and matching what you produced) is the exactly-once proof; a `COUNT(*)`
  that jumps past what you produced is the duplicate signature.

| Run | New events produced | Expected sink `COUNT(*)` | `COUNT(DISTINCT id)` | What it proves |
|-----|--------------------:|-------------------------:|---------------------:|----------------|
| 1 (first start) | 1000 | 1000 | 1000 | initial ingest |
| 2 (produce 500 more, **same checkpoint**) | 500 | **1500** | 1500 | resumes from committed offsets — only the new 500 |
| 3 (no new data, **same checkpoint**) | 0 | **1500** | 1500 | restart is idempotent — nothing reprocessed |
| contrast (**new/empty checkpoint**, `earliest`) | 0 | re-reads all → duplicates | < `COUNT(*)` | a wiped checkpoint reprocesses everything |

## 4. Diagnose

Structured Streaming's checkpoint is the query's durable memory. Each batch's offset range is
committed to the checkpoint's `offsets/` (and `commits/`) log **atomically with** the batch
completing, so a restart always knows the exact boundary between "done" and "not done". Kafka
offsets are monotonic per partition, so resuming from the committed end offset reads each record
**at-least-once**. Iceberg writes are **atomic appends** and the same query+checkpoint won't re-emit
a committed batch — so a healthy restart is **exactly-once** end to end. Remove the checkpoint and
the query loses that boundary: with `earliest` it starts from offset 0 and reprocesses; with
`latest` it would instead **skip** everything already in the topic. Either way you've broken
restart safety.

## 5. Fix it — one durable checkpoint + an idempotent sink

The fix is the discipline, not a config flip:

- **Keep ONE stable `checkpointLocation` per query** and never delete it casually. It is the source
  of truth for "what have I already processed". Treat it as part of the job's state, not scratch.
- **Write to an idempotent sink** (Iceberg here). Atomic appends + checkpointed offsets give
  effectively exactly-once; if you instead key the write (upsert/`MERGE` by `id`), even an
  at-least-once replay can't create duplicates (that's the CDC-7 pattern).
- **Bounded reruns** with `.trigger(availableNow=True)` make this safe to demonstrate and to operate:
  the query drains available data and stops, and the *next* run resumes from the committed offsets.

```python
q = (parsed.writeStream.format("iceberg")
       .option("checkpointLocation", CKPT)          # SAME path every run
       .trigger(availableNow=True)                  # drain-and-stop, never unbounded
       .toTable(SINK))
q.awaitTermination()
```

## 6. Prove it

Counts go **1000 → 1500 → 1500** with `COUNT(*) == COUNT(DISTINCT id)` at every step — the second
run ingested only the new 500 (resumed from committed offsets), the third reprocessed nothing
(idempotent restart), and there are **no duplicate `id`s**. The contrast run against a fresh
checkpoint re-ingests all 1000 into a scratch table, making the duplicates explicit. `lastProgress`
shows the start offset of each resuming run equal to the prior run's end offset.

## 7. Takeaways & "in real production…"

- **Never delete a checkpoint to "reset" a stream casually** — it reprocesses (`earliest`) or skips
  (`latest`). Wiping the checkpoint is a deliberate, last-resort reset, paired with a sink that can
  tolerate the replay.
- **Checkpoint + idempotent sink = exactly-once into Iceberg.** The checkpoint guarantees
  at-least-once delivery of each offset range; the idempotent/atomic sink removes the duplicates.
- **One checkpoint per query**, on durable storage, owned alongside the job's state. Changing the
  query shape can invalidate it — version your streaming jobs.
- **This ties to KAF-2** (offset/commit semantics — the checkpoint *is* the offset store for
  Structured Streaming) and to **CAP** (restart/replay drills in the incident simulator). For
  keyed exactly-once on top of replay, see **CDC-7** (`MERGE` upsert by primary key / LSN).

## 8. Teardown

The notebook ends with a **Teardown** cell that `delete_topic`s the Kafka topic and `DROP`s the
Iceberg sink (and the scratch contrast table). `make clean` removes everything under `.tmp/`,
including the `checkpoint_str2` directory.
