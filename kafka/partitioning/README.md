# KAF-1 — Partitioning & hot partitions

> **Break → Detect → Fix → Prove.** A Kafka topic's parallelism *is* its **partitions**. The
> producer picks a partition by **hashing the message key**, so a lopsided key (one dominant value,
> or `null`/constant) sends most messages to **one partition** — a *hot partition*. That partition's
> consumer falls behind while the others sit idle, and ordering is only guaranteed **within** a
> partition. This is the streaming cousin of SPK-1 data skew.

- **Notebook:** [`kaf1_partitioning.ipynb`](./kaf1_partitioning.ipynb)
- **Toolkit used:** `common.kafka_helpers` (`ensure_topic` / `produce_events` / `topic_end_offsets` /
  `delete_topic`), `common.spark_session`
- **Run against:** the unified Spark server + Kafka (`make up`). Producers talk to
  `localhost:29092`; Spark reads via `kafka:9092`. Inspect topics live in **kafka-ui** at
  http://localhost:8080.
- **Time:** ~10 min. **Laptop-safe:** a few hundred bounded events, batch read (no infinite
  stream), `delete_topic` teardown (`make clean` clears the rest).

---

## 1. The scenario

You key a Kafka topic by something that *feels* natural — `country`, `tenant`, an event `type` —
but the distribution is lopsided: 90% of orders are one country. The producer hashes the key to
choose a partition (`partition = hash(key) % num_partitions`), so 90% of the traffic lands on **one
partition**. Add as many consumers as you like — only one of them gets the hot partition, and it
falls steadily behind while the rest idle. You bought parallelism and got a single-lane road.

## 2. Break it — a dominant key

Create `kaf1_orders` with **3 partitions** and produce events where 90% share the key `"HOT"`:

```python
ensure_topic("kaf1_orders", num_partitions=3)
produce_events("kaf1_orders", 300, key_fn=lambda i: "HOT" if i % 10 else f"cust-{i}")
```

`topic_end_offsets("kaf1_orders")` then shows one partition's offset far ahead of the other two —
the fingerprint of the pathology.

## 3. Detect it

Two views of the same skew:

| View | What you see |
|------|--------------|
| **kafka-ui** (http://localhost:8080 → `kaf1_orders` → Partitions) | one partition's end offset far ahead of the others |
| **Spark** (the Kafka source exposes a `partition` column) | `groupBy("partition").count()` shows one partition carrying the load; its consumer task does most of the work and lags |

## 4. Diagnose

`partition = hash(key) % num_partitions`. A dominant key collapses to one partition, so:

- that partition's consumer lags while the others idle (wasted parallelism),
- you **can't** fix it by adding partitions — the hot key still hashes to a single one,
- ordering holds only *within* a partition (rarely the global order you imagined).

## 5. Fix it — a high-cardinality key (or salt the hot one)

Key by something that spreads evenly — here a per-customer id:

```python
produce_events("kaf1_orders", 300, key_fn=lambda i: f"cust-{i}")
```

If one key is *legitimately* hot, **salt** it (`key + "-" + rand(0..N)`) and merge downstream —
the same trick as SPK-1 salting.

## 6. Prove it

`topic_end_offsets` after the rekey shows the events spread roughly evenly across the 3 partitions
(≈100 each) instead of ≈270/15/15. Even partitions → even consumer load → no single laggard.

## 7. Takeaways & "in real production…"

- **Partition key = parallelism + ordering.** Choose a high-cardinality key aligned to how you
  consume; avoid `null` / constant / lopsided keys.
- A hot key **can't** be fixed by adding partitions — salt it (and merge downstream) or rekey.
- Watch **per-partition consumer lag** (kafka-ui / `consumer_group_lag`, see [KAF-2](../consumer_lag/));
  a single lagging partition is the hot-partition signature.
- Ordering is **per-partition only** — design for that.

## Teardown

`delete_topic("kaf1_orders")` at the end of the notebook; `make clean` clears any generated data.
