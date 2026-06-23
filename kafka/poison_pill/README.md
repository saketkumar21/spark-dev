# KAF-6 — Poison Pill / Dead-Letter

> **Break → Detect → Fix → Prove.** One corrupt, unparseable message in a partition can stall a
> naive consumer (it keeps failing on the *same* offset, never advancing) or crash the whole job.
> The production cure is the **dead-letter** pattern: isolate the bad records, keep moving.

- **Notebook:** [`kaf6_poison_pill.ipynb`](./kaf6_poison_pill.ipynb)
- **Toolkit used:** `common.kafka_helpers` (`ensure_topic`, `produce_events`, `delete_topic`,
  `SPARK_BOOTSTRAP`), `common.spark_session` (Spark Connect session).
- **Run against:** the unified Spark server (`make up`) — Kafka at `localhost:29092` (producers,
  from the host) and `kafka:9092` (Spark, inside the Docker network). Browse topics live in
  **kafka-ui** at http://localhost:8080.
- **Time:** ~10 min. **Laptop-safe:** a small bounded batch (~500 good + a few bad messages),
  read with `.trigger(availableNow=True)` so the stream consumes everything and **stops on its
  own** — no infinite stream pinning the laptop. Teardown deletes the topic and Iceberg tables;
  `make clean` clears any local state.

---

## 1. The scenario

An events pipeline reads JSON order events off Kafka, parses them, and lands them in an Iceberg
table. It runs happily for weeks — then one upstream service ships a bad serializer for an hour
and emits a handful of **non-JSON / wrong-schema** messages into the topic.

A naive consumer that decodes each message and calls `json.loads()` (or reads with
`from_json(..., mode='FAILFAST')`) now **throws on the bad offset**. If it doesn't commit past
it, every restart re-reads the same poison message and dies again — **the partition is blocked**.
Worse, a "PERMISSIVE" reader silently turns the bad rows into **all-NULL** structs and writes
garbage, so the corruption goes unnoticed downstream.

## 2. Break it

We produce ~500 good JSON events with `common.kafka_helpers.produce_events`, then interleave a few
**raw poison messages** with a plain `kafka-python` producer (because `produce_events`
JSON-encodes its values, the only way to inject genuinely malformed bytes is a raw `.send(...)`):

- `b"NOT-JSON-this-is-a-poison-pill"` — not JSON at all,
- a JSON object missing the required `id` field — right format, wrong schema.

Spark reads the topic and parses with `from_json(value, schema)`. In Spark's default **PERMISSIVE**
mode, an unparseable row doesn't raise — the parsed struct comes back **all-NULL**. So a naive
"parse and write everything" pipeline silently lands NULL rows; switch the reader to
`mode='FAILFAST'` and the **whole batch errors** instead. Either way, one bad message breaks you.

## 3. Detect it

The signature of a poison pill: **the parsed struct (or a required field) IS NULL while the raw
`value` is non-empty.** A genuinely empty/absent payload would also be NULL, so we anchor on a
*required* field — here `parsed.id IS NULL AND value IS NOT NULL`. The notebook counts those rows;
in kafka-ui you can also see the consumer of a naive FAILFAST job stuck at one offset.

| Signal | Poison present | Healthy |
|--------|----------------|---------|
| `from_json` struct | all-NULL for the bad rows | fully populated |
| Naive `FAILFAST` read | batch **throws** | completes |
| Naive consumer offset (kafka-ui) | **stuck** on the bad offset, lag grows | advances |
| DLQ rate | **> 0** (alert!) | 0 |

## 4. Diagnose

`from_json` returns a struct typed by your schema. When the bytes can't be parsed into that
schema, every field is NULL (PERMISSIVE) — there is no exception to catch unless you opt into
FAILFAST, and FAILFAST takes down the *entire micro-batch* for *one* bad row. A bare
decode-and-loop consumer is even more fragile: it raises on the offset and, without committing
past it, reprocesses the same poison forever. The root problem is **coupling progress to
parse-success** — one undecodable record must not be allowed to block the partition.

## 5. Fix it — the dead-letter pattern

**Split the stream by parse outcome and write both sides**, so the bad records are quarantined and
the good ones flow through (the partition is never blocked):

- rows where the required field parsed (`parsed.id IS NOT NULL`) → the **main** Iceberg table
  (flattened, typed columns),
- rows that failed to parse (`parsed.id IS NULL`) → a **dead-letter (DLQ)** Iceberg table that
  keeps the raw `value` plus `topic`, `partition`, `offset`, `timestamp` for forensics/replay.

We run it laptop-safely with `.trigger(availableNow=True)`: two bounded writes (main + DLQ), each
with its **own checkpoint**, consume all available data and stop. The job **commits and continues**
— a poison pill becomes a DLQ row, not an outage. (`foreachBatch` with two writes inside one
batch is the equivalent single-stream variant; two `availableNow` writes is the simplest
Connect-safe form.)

## 6. Prove it

The arithmetic that proves correctness and liveness:

```
main_count (parsed OK)  +  dlq_count (poison)  ==  total produced
```

and the pipeline **finished without stalling** (both `availableNow` streams terminated on their
own). No good data was lost; no bad data was silently written into the main table; the DLQ holds
exactly the poison messages with their raw bytes for replay.

## 7. Takeaways & "in real production…"

- **Never let one bad message block a partition.** Decouple progress from parse-success — isolate
  unparseable records to a **dead-letter queue/table** and commit past them.
- **Don't trust PERMISSIVE silence.** All-NULL structs from `from_json` are corruption hiding in
  plain sight — explicitly detect `required_field IS NULL AND value IS NOT NULL` and route it.
- **Alert on the DLQ rate.** A non-zero (or rising) dead-letter rate is your early warning that an
  upstream producer has gone bad; page on it.
- **Validate the schema upstream.** A schema registry with compatibility checks (or producer-side
  validation) stops most poison pills before they ever reach the topic — the DLQ is the safety net,
  not the primary defense.
- **Keep raw bytes + coordinates in the DLQ** (`value`, `topic`, `partition`, `offset`,
  `timestamp`) so you can diagnose and **replay** once the upstream bug is fixed.
