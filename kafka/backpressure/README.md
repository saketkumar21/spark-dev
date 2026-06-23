# STR-3 — Backpressure & micro-batch sizing

> **Break → Detect → Fix → Prove.** A Structured Streaming query runs as a series of
> **micro-batches**. How many records each batch pulls is the single biggest lever on a
> streaming job's behavior: pull **too many** and a batch goes huge → memory pressure, long
> batch times, and a flood of writes (the streaming **small-files** problem); pull **too few**
> and per-batch overhead dominates. The Kafka source option **`maxOffsetsPerTrigger`** (and the
> file source's `maxFilesPerTrigger`) **caps the input per micro-batch**, turning one unbounded
> gulp into a steady, predictable stream of bounded batches.

- **Notebook:** [`str3_backpressure.ipynb`](./str3_backpressure.ipynb)
- **Toolkit used:** `common.kafka_helpers` (`ensure_topic`, `produce_events`, `delete_topic`,
  `SPARK_BOOTSTRAP`) + `common.iceberg_meta` (`table_health`) for the small-files proof.
- **Run against:** the unified Spark server (`make up`). Producers use the host listener
  `localhost:29092`; Spark's `readStream` uses the internal listener `kafka:9092` (`SPARK_BOOTSTRAP`).
- **Time:** ~10 min. **Laptop-safe:** a bounded ~5,000-event batch, and every query uses
  `.trigger(availableNow=True)` so it **drains what's there and stops on its own** — no infinite
  stream pinning the laptop. Checkpoints/sinks live under `.tmp/`; the topic is deleted at teardown.

---

## 1. The scenario

An orders service publishes a burst of ~5,000 events to a Kafka topic, and a Spark Structured
Streaming job appends them into an Iceberg table. It works fine in the demo. But in production a
consumer that's been down for an hour comes back to a **backlog of millions** of records — and
the question that decides whether the job survives the catch-up is: *how much does each
micro-batch try to swallow at once?*

We reproduce the dynamics safely with the **`availableNow` trigger**, which behaves like a
catch-up run: it processes **all currently-available data, then stops**. The subtlety the whole
module hinges on:

- With **no input cap**, `availableNow` pulls the *entire* backlog in **one giant micro-batch**.
- With **`maxOffsetsPerTrigger` set**, `availableNow` still drains everything — but across
  **multiple bounded batches**, each capped at the limit. Same total work, very different shape.

`q.recentProgress` (a list of per-batch dicts with `batchId` and `numInputRows`) lets us *see* the
batch shape directly, and the Iceberg sink's **data-file count** (`table_health`) shows how that
shape turns into the small-files problem.

## 2. Break it — one giant batch

- `ensure_topic(TOPIC, 1)` + `produce_events(TOPIC, ~5000)` puts the full backlog on the topic
  (each event carries a small text payload so the batch has realistic **bytes**, not just rows).
- Read it with `.trigger(availableNow=True)` and **no** `maxOffsetsPerTrigger`, writing to a fresh
  Iceberg sink; `q.awaitTermination()` blocks until the (bounded) run finishes.
- `q.recentProgress` shows a **single batch** (`batchId = 0`) with `numInputRows ≈ 5000` — the
  whole backlog in one gulp.

At laptop scale 5,000 rows is harmless — but that single batch *is* the failure mode in miniature:
a real backlog of millions would be one massive batch that has to be held and shuffled in memory at
once (memory pressure / long batch time), exactly what `maxOffsetsPerTrigger` exists to prevent.

## 3. Detect it

| Where | What you see |
|-------|--------------|
| `q.recentProgress` → list length & per-batch `numInputRows` | **broken:** length 1, one batch of ~5000. The batch-size distribution is the headline signal. |
| `table_health(spark, SINK)` → `data_files` | data files written ≈ batches × files-per-batch; one batch → few files (but a real one-shot batch can still emit many parts) |
| **Spark UI** http://localhost:4040 → **Structured Streaming** tab | batch duration & input-rows-per-batch; a single fat bar vs a steady train of small ones |

The per-batch `numInputRows` series *is* the diagnosis: a single huge value means the query is
unbounded and will scale its memory footprint with the size of the backlog.

## 4. Diagnose — why one batch is dangerous

Without an input cap, a micro-batch's size is bounded only by **how much data is available**. That
couples the job's peak memory and batch latency to the **backlog**, not to your cluster:

- a normal trigger holds ~one trigger-interval of data — fine;
- but after downtime (or under `availableNow`), "available" can be the entire backlog, so the batch
  balloons → **memory pressure / OOM risk**, a **long** batch the rest of the pipeline waits on, and
  a write that's hard to size.

The other horn of the dilemma: making batches *tiny* (or triggering very frequently) means lots of
small batches, each paying fixed per-batch overhead **and** writing its own set of files — the
streaming **small-files** problem (see **LAK-2**). Batch sizing is a balance, not a "smaller is
always better."

## 5. Fix it — bound the batch with `maxOffsetsPerTrigger`

Set `.option("maxOffsetsPerTrigger", 1000)` on the Kafka reader. Now even under `availableNow`,
Spark drains the same ~5,000-record backlog across **~5 bounded micro-batches** of ~1,000 rows
each — steady, predictable memory and latency regardless of how big the backlog is.

```python
stream = (spark.readStream.format("kafka")
          .option("kafka.bootstrap.servers", SPARK_BOOTSTRAP)
          .option("subscribe", TOPIC)
          .option("startingOffsets", "earliest")
          .option("maxOffsetsPerTrigger", 1000)   # <- the cap: ≤1000 records per micro-batch
          .load())
```

The file-source equivalent is **`maxFilesPerTrigger`**; the same idea (a per-batch input ceiling)
applies to both sources.

## 6. Prove it

Same total rows, different batch shape — read straight off each query's `recentProgress`:

| Run | `maxOffsetsPerTrigger` | batches | rows / batch | total rows |
|-----|------------------------|--------:|--------------|-----------:|
| broken | (none) | **1** | ~5000 | ~5000 |
| fixed | 1000 | **≈5** | ~1000 each | ~5000 |

The batch count going from **1 → ~5** while the total stays ~5,000 is the proof: the cap converted
one unbounded gulp into bounded, steady batches. `table_health(spark, SINK)` on each sink shows the
**small-files tie** — more frequent bounded batches write **more (smaller) files**, which is why
LAK-2 (`rewrite_data_files` / compaction) is the natural companion to frequent triggers.

## 7. Takeaways & "in real production…"

- **Bound your batches.** Set `maxOffsetsPerTrigger` (Kafka) / `maxFilesPerTrigger` (files) so peak
  memory and batch latency track your **budget**, not the size of an unexpected backlog.
- **Size it to memory + latency.** Larger batches = fewer files + more throughput but higher peak
  memory & latency; smaller / more frequent = lower latency but more overhead and **more small
  files**. Pick the point that fits your SLA and heap.
- **Mind the small-files tie (LAK-2).** Frequent bounded triggers keep memory safe but multiply tiny
  files — schedule compaction (`rewrite_data_files` / `OPTIMIZE`) alongside a streaming sink.
- **Watch the Structured Streaming tab.** Per-batch input rows and batch duration are the live
  signals; a single fat batch (or batch time climbing toward the trigger interval) means you're
  unbounded or under-provisioned.

## 8. Teardown

`delete_topic(TOPIC)` removes the topic and the notebook drops both Iceberg sinks. `make clean`
also clears the checkpoints and any local `.tmp/` state.
