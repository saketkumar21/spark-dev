# STR-1 — Watermarking & late data

> **Break → Detect → Fix → Prove.** Event-time aggregations (per-minute counts, hourly revenue)
> must tolerate **out-of-order** events — a record stamped `10:00:30` can arrive after one stamped
> `10:02:00` because of network delay, retries, or a mobile device that was offline. But you can't
> keep every open window's state in memory **forever** waiting for stragglers. A **watermark** is
> the engine's promise — *"I will not accept events older than `max_event_time − delay`"* — that
> lets it **finalize** a window, **drop its state**, and **discard** late events that fall behind
> the frontier.

- **Notebook:** [`str1_watermarking.ipynb`](./str1_watermarking.ipynb)
- **Toolkit used:** `common.kafka_helpers` (`ensure_topic`, `produce_events`, `delete_topic`,
  `SPARK_BOOTSTRAP`) + `common.spark_session` (Spark Connect). Spark reads/writes via
  Structured Streaming (`readStream` / `writeStream`) — no `sparkContext`/RDD (Connect-safe).
- **Run against:** the unified Spark server + Kafka (`make up`). Producers use the host listener
  `localhost:29092`; Spark consumes via the internal listener `kafka:9092` (`SPARK_BOOTSTRAP`).
  Inspect the topic live in **kafka-ui** at http://localhost:8080.
- **Time:** ~12 min. **Laptop-safe:** every stream uses **`.trigger(availableNow=True)`** — Spark
  drains all available data and **stops on its own** (no infinite stream pinning the laptop).
  Checkpoint lives under `.tmp/checkpoint_str1`; the sink is an Iceberg table in
  `iceberg_catalog.default`. Teardown deletes the topic and drops the sink; `make clean` clears
  `.tmp/`.

---

## 1. The scenario

A clickstream service publishes `page_view` events to Kafka, each carrying its own **`event_time`**
(when the click happened on the user's device). A streaming job counts views **per 1-minute
event-time window** to drive a near-real-time dashboard.

Events do **not** arrive in event-time order: a phone that briefly lost signal flushes a buffer of
older clicks minutes later. The job must (a) bucket each event into the right minute by its
*event-time*, not its arrival time, and (b) decide how long to **wait** for late events before it
declares a minute "final" and stops updating it. Wait too short → you drop valid late clicks. Wait
forever → window state grows without bound and eventually OOMs.

## 2. Break it — a windowed count with **no watermark**

```python
no_wm = (events
    .groupBy(F.window("event_time", "1 minute"))
    .count())
```

Without `withWatermark(...)`, Spark has **no upper bound on lateness**, so it must assume *any*
future micro-batch might contain an event for *any* past minute. Every 1-minute window stays
**open forever** and its count is kept in **state**. On a 24/7 stream that's one state entry per
minute per key, climbing indefinitely — unbounded state, growing checkpoints, eventual memory
pressure. (We use `availableNow` so the demo still terminates, but the *semantics* are
"keep everything.")

## 3. Detect it — event-time vs processing-time & the watermark frontier

| Where | What you see |
|-------|--------------|
| `SELECT max(event_time)` over the batch | the **watermark frontier candidate** — the latest event the engine has seen |
| `frontier − delay` | the cutoff: any event with `event_time < cutoff` is "too late" |
| `event_time` vs the time the row was *read* | **event-time** (in the payload) ≠ **processing-time** (now) — the whole reason late data exists |
| **Spark UI** → http://localhost:4040 → **Structured Streaming** → query → `numRowsDroppedByWatermark` | once a watermark is set, the count of events discarded for being too late |

The notebook prints the frontier and tags each produced event as on-time or late relative to
`frontier − delay`, so you can see *which* events the watermark will exclude **before** running the
aggregation.

## 4. Diagnose — what a watermark actually bounds

A watermark on `event_time` with delay `D` tells the engine two things at once:

1. **State eviction.** Once the frontier passes `window_end + D`, that window can never change
   again, so its state is **dropped** (bounded memory). This is the fix for the unbounded-state
   problem in step 2.
2. **Late-event rejection.** An event whose `event_time` is **older than** `frontier − D` arrives
   for a window that's already finalized → it is **excluded** from the result (counted in
   `numRowsDroppedByWatermark`).

Event-time = when it happened (in the data). Processing-time = when Spark saw it. The watermark is
expressed in **event-time** and advances as `max(event_time) − D`; it is the bridge that lets an
event-time aggregation run on a processing-time stream without waiting forever.

## 5. Fix it — `withWatermark` before the windowed aggregation

```python
windowed = (events
    .withWatermark("event_time", "2 minutes")        # tolerate up to 2 min of lateness
    .groupBy(F.window("event_time", "1 minute"))
    .count())                                          # outputMode("append") on the sink
```

With a 2-minute watermark:
- Closed windows' state is **evicted** once the frontier moves 2 minutes past their end → bounded
  memory, bounded checkpoint size.
- A **late** event (timestamp well behind `frontier − 2 min`) is **dropped** — it does **not**
  change the already-finalized window's count.

We demonstrate exactly that: produce an on-time batch, aggregate it with the watermark (run #1,
`availableNow`), then produce a single **very late** event for a minute that's already past the
watermark and run again (run #2). The finalized window's count is **unchanged** — the late event
was discarded.

> **`append` output mode:** with a watermark, a windowed aggregation emits each window's final
> result **once**, when the watermark closes it — so the sink is append-only (the natural fit for
> writing finalized windows to Iceberg). Without a watermark you'd need `complete`/`update` mode,
> which re-emits the whole (unbounded) result table every batch — another reason the watermark is
> the production pattern.

## 6. Prove it

| Run | What's produced | Window `10:00` count | Late events dropped |
|-----|-----------------|---------------------:|--------------------:|
| #1 (on-time batch, watermarked) | the in-order events | **N** (correct total) | 0 |
| #2 (one late event for `10:00`) | a straggler past the watermark | **still N** (unchanged) | **1** |

The proof is two numbers moving in opposite directions: the **accepted** count for the finalized
window does **not** increase when a late event arrives, and `numRowsDroppedByWatermark` ticks up by
the number of stragglers. Side-by-side, the window result **with vs without** the late data is
identical — the watermark held the line. (We also contrast the no-watermark result, where the same
late event *would* have mutated the old window, to make the bound concrete.)

## 7. Takeaways & "in real production…"

- **Event-time, not processing-time.** Aggregate on the timestamp in the data; treat arrival time
  as unreliable. The watermark is how you reconcile the two.
- **Tune the watermark to your real lateness SLA.** It's a direct trade-off: **too tight** drops
  valid late data (silent under-counting); **too loose** keeps state (and checkpoints) large and
  delays results. Measure your actual lateness distribution and set the delay above the p99.
- **Watch `numRowsDroppedByWatermark`.** A rising drop rate means events are arriving later than
  your watermark allows — either widen it or fix the upstream delay. Flat-near-zero is healthy.
- **Watermark + append + idempotent sink.** Finalized windows write once, in append mode, into an
  idempotent table (Iceberg) — late stragglers can't corrupt closed windows. State lives in the
  **checkpoint**, which is the subject of **STR-2** (idempotency, checkpoints & restart).

## 8. Teardown

`delete_topic(TOPIC)` removes the Kafka topic and `DROP TABLE` removes the Iceberg sink.
`make clean` also clears `.tmp/` (checkpoint `checkpoint_str1`, warehouses, event logs).
