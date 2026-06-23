# `kafka/` — Kafka & Structured Streaming robustness (Phase 3) ✅ complete

Streaming failure modes that bite in production — **topic-level** (partitioning, lag, rebalancing,
retention, delivery, poison messages) and **Spark-streaming-level** (watermarks, checkpoints,
backpressure). Each module follows **Break → Detect → Fix → Prove** (see
[`docs/CURRICULUM_BRIEF.md`](../docs/CURRICULUM_BRIEF.md)) and reuses the [`common/`](../common/)
toolkit — especially [`common/kafka_helpers.py`](../common/kafka_helpers.py) (`ensure_topic` /
`produce_events` / `topic_end_offsets` / `consumer_group_lag` / `delete_topic`).

> **Laptop-safe:** bounded produce (hundreds–few-thousand events), batch reads or
> `trigger(availableNow=True)` streams that **stop on their own**, all state under `.tmp/`; every
> notebook ends with `delete_topic` teardown and `make clean` recovers the rest.
>
> **Connect-safe:** topic admin / produce / consume run through `kafka-python` against the host
> listener `localhost:29092`; Spark reads via the internal listener `kafka:9092`
> (`SPARK_BOOTSTRAP`). No `spark.sparkContext` / RDD APIs (unavailable over Spark Connect).
>
> **The honesty rule:** a few behaviors can't be triggered deterministically and laptop-safely in
> one notebook process — true multi-process rebalancing, broker-scheduled retention/compaction GC,
> forced transactional retries. Those modules **demonstrate what *is* observable** (assignment
> splits, applied configs, latest-per-key semantics, duplicate counts) and **describe** the rest
> with correct snippets — the same stance as the SPK-2/SPK-3 OOM modules.
>
> **Run any module:** `make up` → `make jupyter` → open its notebook. Inspect topics live in
> **kafka-ui** at http://localhost:8080.

## Modules

`[ ]` not started · `[~]` in progress · `[x]` built & live-tested (headless `nbconvert`)

| ID | Module | Status |
|----|--------|--------|
| `KAF-1` | [Partitioning & hot partitions](partitioning/) — bad key → one hot partition; per-partition ordering; fix by key design / salting | `[x]` |
| `KAF-2` | [Consumer lag & offset semantics](consumer_lag/) — auto vs manual commit; reprocess/duplicates on crash; lag as the headline metric | `[x]` |
| `KAF-3` | [Consumer groups & rebalancing](consumer_groups/) — join/leave → partitions reassigned; stop-the-world pause; timeouts, static membership, cooperative-sticky | `[x]` |
| `KAF-4` | [Retention & compaction](retention/) — offline past `retention.ms` → `OffsetOutOfRange`; log compaction (latest-per-key) for state topics | `[x]` |
| `KAF-5` | [Delivery semantics](delivery_semantics/) — at-least-once vs exactly-once; idempotent producer, `read_committed`, idempotent sinks | `[x]` |
| `KAF-6` | [Poison pill / dead-letter](poison_pill/) — a corrupt message stalls a partition; route bad records to a dead-letter sink and continue | `[x]` |
| `STR-1` | [Watermarking & late data](watermarking/) — event- vs processing-time; `withWatermark`; watch a late event get dropped | `[x]` |
| `STR-2` | [Idempotency, checkpoints & restart](checkpoints/) — kill/restart safely; checkpoint resume; dedup; exactly-once into Iceberg | `[x]` |
| `STR-3` | [Backpressure & micro-batch sizing](backpressure/) — `maxOffsetsPerTrigger` / `maxFilesPerTrigger`; the streaming small-files problem (ties to `LAK-2`) | `[x]` |

## Layout

```
kafka/
├── README.md             # this file (Phase 3 track index)
├── partitioning/         # KAF-1
├── consumer_lag/         # KAF-2
├── consumer_groups/      # KAF-3
├── retention/            # KAF-4
├── delivery_semantics/   # KAF-5
├── poison_pill/          # KAF-6
├── watermarking/         # STR-1
├── checkpoints/          # STR-2
└── backpressure/         # STR-3
```

Each `kafka/<topic>/` holds a `README.md` (the Break→Detect→Fix→Prove writeup) and a runnable
`<id>_<topic>.ipynb`. All built and **live-verified** end-to-end against the Spark + Kafka stack.

## Suggested order

`KAF-1` (partitioning) → `KAF-2` (lag) → `KAF-3` (rebalancing) → `KAF-4` (retention) →
`KAF-5` (delivery) → `KAF-6` (poison pill) → then the Spark-streaming trio
`STR-1` (watermarking) → `STR-2` (checkpoints) → `STR-3` (backpressure). The `KAF-*` set is
Kafka-broker behavior (mostly `kafka-python`); the `STR-*` set is Spark Structured Streaming
(`readStream`/`writeStream` → Iceberg). `STR-2` (exactly-once into Iceberg) and `KAF-5`
(delivery guarantees) are the bridge to the Phase 4 CDC track.

## Producers in `app/`

The original [`app/utils/sales_producer.py`](../app/utils/sales_producer.py) (Kafka) and
[`app/notebooks/04_sales_streaming_to_iceberg`](../app/notebooks/) remain as a worked
end-to-end example; this track generalizes their patterns into the reusable
[`common/kafka_helpers.py`](../common/kafka_helpers.py) used by every module above.
