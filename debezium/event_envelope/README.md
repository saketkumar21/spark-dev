# CDC-4 — The CDC event envelope

> **Break → Detect → Fix → Prove.** Every Debezium change record is an **envelope**, not a row:
> `before`, `after`, `op` (`c`/`r`/`u`/`d`), `ts_ms`, and `source` (with the WAL `lsn`). This module
> reads one event of each op to see the envelope's shape, then registers a **second** connector with
> the `ExtractNewRecordState` SMT to **flatten** that envelope into a plain row on a different topic —
> so you can decide, per sink, whether you need the full envelope or just the latest values.

- **Notebook:** [`cdc4_event_envelope.ipynb`](./cdc4_event_envelope.ipynb)
- **Toolkit used:** `common.cdc_helpers` (`seed_orders`, `debezium_pg_config`, `register_connector`,
  `wait_for_connector`, `read_cdc_events`, `op_counts`, `topic_name`, `teardown`, `pg_exec`),
  `kafka.KafkaConsumer` / `KafkaAdminClient` (to read + delete the unwrap topic).
- **Run against:** the CDC stack — `make cdc-up` (Postgres + Kafka Connect), Kafka-UI at
  http://localhost:8080.
- **Time:** ~8 min. **Laptop-safe:** one 20-row table (`public.cdc4_orders`), three DML statements,
  bounded topic reads (`read_cdc_events` has a timeout), **teardown of BOTH connectors + the table +
  both topics** at start and end. **Connect-safe:** `cdc_helpers` + `kafka-python` only — no
  `sparkContext`/RDD.

> **Isolation:** table `cdc4_orders`, connectors `cdc4-orders` (full envelope) and `cdc4-unwrap`
> (flattened, distinct `topic.prefix=dbz4u`). Re-running is idempotent — the first cells tear down
> any leftovers from a previous run.

---

## 1. The scenario

You've got Debezium streaming `public.cdc4_orders` into Kafka (CDC-2/CDC-3). A teammate opens the
topic, expects to see *rows*, and finds something bigger: each message is a JSON **envelope** that
describes a *change*, not the row itself. Before you can build a sink — a Spark `MERGE` into Iceberg
(CDC-7), or a flat dump into a search index — you have to understand that envelope, because **it is
the contract**. Get `op` and `ts_ms` and the key right and your downstream upserts are correct and
idempotent; ignore them and you'll apply a stale update over a newer one, or lose a delete.

The envelope is also more than most simple sinks want. A connector that just needs "the current row"
(write it to Elasticsearch, append it to a flat file) doesn't want a `before`/`after` wrapper — it
wants the row. Debezium solves that with a **Single Message Transform (SMT)**, `ExtractNewRecordState`
(historically "unwrap"), which rewrites each envelope into the flattened `after` row before it ever
hits the topic. So this module is two halves: **read the envelope**, then **flatten it** and compare.

## 2. Break it — well, *observe* it: the envelope shape

Seed the table and register the standard connector (`snapshot.mode=initial`). The initial snapshot
emits one `r` (read) event per row; then we drive one of each remaining op directly in Postgres:

```python
cdc.pg_exec("INSERT INTO public.cdc4_orders(id,customer,amount,status) VALUES (200,'envelope-demo',42.00,'NEW')")  # → c
cdc.pg_exec("UPDATE public.cdc4_orders SET status='SHIPPED', amount=43.00 WHERE id=200")                            # → u
cdc.pg_exec("DELETE FROM public.cdc4_orders WHERE id=200")                                                          # → d (+ tombstone)
time.sleep(5)            # streaming has a small WAL-decode lag
events = cdc.read_cdc_events(cdc.topic_name("cdc4_orders"))
```

Each op carries a different `before`/`after` pairing — that pairing *is* the lesson:

| `op` | meaning | `before` | `after` |
|------|---------|----------|---------|
| `r` | read (snapshot) | `null` | full row |
| `c` | create (insert) | `null` | full row |
| `u` | update | **PK only** by default* | full new row |
| `d` | delete | row image (pre-delete) | `null` |
| *(none)* | **tombstone** | — | message **value is `null`** |

\* With the default `REPLICA IDENTITY`, an UPDATE's `before` contains only the **primary key**, not
the old column values. Capturing the *full* old image needs `REPLICA IDENTITY FULL` on the table —
that's **CDC-6**. (Here `seed_orders(..., replica_identity_full=False)` keeps the default, so you
*see* the partial-`before` gotcha rather than just being told about it.)

The **tombstone** is the null-value record Debezium emits *after* a delete (because
`tombstones.on.delete=true`). It exists so a **log-compacted** topic can physically drop the key:
compaction keeps the last value per key, and a `null` value is the "this key is gone" marker.

## 3. Detect it — op counts + the ordering metadata

`common.cdc_helpers.op_counts(events)` summarizes the drained topic; after the snapshot + three DML
statements you expect every op plus the tombstone:

```python
cdc.op_counts(events)   # → {'r': 20, 'c': 1, 'u': 1, 'd': 1, 'tombstone': 1}
```

The fields that make CDC *correct* aren't `before`/`after` — they're the **ordering metadata** on
the raw envelope:

```python
raw = next(e["raw"] for e in events if e["op"] == "u")
raw["ts_ms"]          # event time (ms) — when Debezium processed the change
raw["source"]["lsn"]  # the WAL log-sequence number — the total order of changes in Postgres
```

`source.lsn` is monotonic in commit order, so it (not wall-clock `ts_ms`) is the tiebreaker that lets
a sink apply changes in the right order even when they arrive out of order or get reprocessed. This
is the seam between CDC-4 and the **idempotent upsert** in CDC-7.

## 4. Diagnose — why an envelope, not a row

A CDC feed has to express things a row can't: *this key was deleted* (`after=null`), *this is the
value as of a consistent snapshot* (`op=r`) vs *a live change* (`op=c/u/d`), and *here's where this
change sits in the WAL* (`source.lsn`) so consumers can order and de-duplicate. A bare row throws all
of that away. The envelope is verbose on purpose: it's a self-describing change event that a sink can
replay safely. The cost is that **simple** sinks now have to dig the row out of `after` themselves —
which is exactly what the SMT automates.

## 5. Fix it — flatten with `ExtractNewRecordState` (the unwrap SMT)

Register a **second** connector over the **same table**, adding the transform and a **distinct
`topic.prefix`** so it publishes to its own topic (`dbz4u.public.cdc4_orders`) instead of colliding
with the first:

```python
unwrap_cfg = cdc.debezium_pg_config(
    "cdc4-unwrap", "cdc4_orders", snapshot_mode="initial",
    extra={
        "topic.prefix": "dbz4u",                                  # → dbz4u.public.cdc4_orders
        "transforms": "unwrap",
        "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState",
        "transforms.unwrap.drop.tombstones": "false",             # keep tombstones (compaction needs them)
        "transforms.unwrap.delete.handling.mode": "rewrite",      # deletes become a row + __deleted flag
    },
)
cdc.register_connector("cdc4-unwrap", unwrap_cfg)
cdc.wait_for_connector("cdc4-unwrap")   # RUNNING
```

Now read **its** topic and compare to the envelope topic. With the SMT:

- a `c`/`u`/`r` event's value is the **flattened `after` row directly** (`{"id":…, "customer":…,
  "amount":…, "status":…, "updated":…}`) — no `before`/`after`/`op` wrapper;
- with `delete.handling.mode=rewrite`, a delete becomes a **row with `__deleted:"true"`** (the keyed
  row plus a marker) instead of being dropped, so a downstream consumer can still react to it;
- `drop.tombstones=false` keeps the null-value tombstone on the topic for log compaction.

`read_cdc_events` parses `before`/`after`/`op` out of the *envelope* schema, so on the **unwrapped**
topic those come back `None` (there's no wrapper) — that's the point. We read the unwrap topic with a
raw `KafkaConsumer` and print the **value as-is** to show it's the bare row.

**When to unwrap vs keep the envelope:**

- **Unwrap** for *simple, current-state* sinks: a JDBC/Elasticsearch sink, a flat file, a "latest row
  per key" view. The sink wants the row, and (with `rewrite`) a `__deleted` flag is enough.
- **Keep the full envelope** when you need `op` and/or `before` to apply changes correctly — most
  importantly an **upsert/MERGE** sink that must distinguish insert/update from delete and may need the
  old image (CDC-7). Flattening throws away exactly the metadata that makes the merge idempotent.

## 6. Prove it — raw envelope vs unwrapped, side by side

The notebook prints, for the same logical change:

| | Envelope topic (`dbz.public.cdc4_orders`) | Unwrapped topic (`dbz4u.public.cdc4_orders`) |
|---|---|---|
| **create value** | `{"before":null, "after":{…row…}, "op":"c", "ts_ms":…, "source":{…lsn…}}` | `{…row…}` (the `after` fields directly) |
| **delete value** | `{"before":{…pre-image…}, "after":null, "op":"d", …}` | `{…row…, "__deleted":"true"}` |
| **tombstone** | value `null` (op parsed as `None`) | value `null` (kept; `drop.tombstones=false`) |

Same source change, two contracts: the left is the full change event; the right is the row a simple
sink consumes. That side-by-side **is** the Prove-it for this module — no metrics table, just the two
payload shapes from one DML stream.

## 7. Takeaways & "in real production…"

- **The envelope is the contract.** `before` / `after` / `op` / `ts_ms` / `source.lsn` — design every
  sink around these, not around "the row".
- **`op` + `ts_ms` + `lsn` drive idempotent upserts.** Order by `source.lsn` (commit order),
  branch on `op` (delete vs upsert), de-dupe by key — that's the CDC-7 MERGE.
- **`before` is partial by default.** Without `REPLICA IDENTITY FULL` an UPDATE's `before` is just the
  PK; turn it on only when a sink actually needs the old values (CDC-6) — it costs WAL volume.
- **Tombstones are for compaction, not noise.** Keep them (`drop.tombstones=false`) on compacted
  topics so deleted keys can be reclaimed.
- **SMTs reshape and route at the connector.** `ExtractNewRecordState` flattens; `topic.prefix`
  (and routing SMTs) decides the topic name `<prefix>.<schema>.<table>`. Transform once at the source
  rather than in every consumer — but only when the consumer truly doesn't need the envelope.

## 8. Teardown

The notebook's final cell tears down **both** connectors and the table:
`cdc.teardown("cdc4-orders", "cdc4_orders")` (deletes the connector, drops its slot + table, deletes
the `dbz.public.cdc4_orders` topic) and, for the unwrap connector, `cdc.delete_connector("cdc4-unwrap")`
+ drop its slot + delete its distinct topic `dbz4u.public.cdc4_orders` via `KafkaAdminClient`
(`cdc.teardown`'s `drop_topic` only knows the default `dbz` prefix, so the `dbz4u` topic is deleted
explicitly). `make clean` clears anything left under `.tmp/`.
