# CDC-6 — Tombstones, deletes & replica identity

> **Break → Detect → Fix → Prove.** A `DELETE` in Postgres is not one CDC record — it's **two**.
> Debezium emits a **delete event** (`op="d"`, `after=null`, the key still in `before`) and then,
> on the same key, a **tombstone**: a record with a **null value**. Downstream that pair means
> *"this key is gone — drop it."* But what the `before` image of a delete (or an update) actually
> *contains* is decided by the source table's **`REPLICA IDENTITY`**: by default Postgres only logs
> the **primary key**, so `before` is key-only; set **`REPLICA IDENTITY FULL`** and `before` carries
> the **entire old row**. Pick the wrong one and you either pay for WAL you don't need, or discover
> too late that you can't reconstruct the value a row *had* before it changed.

- **Notebook:** [`cdc6_deletes.ipynb`](./cdc6_deletes.ipynb)
- **Toolkit used:** `common.cdc_helpers` (`seed_orders` with `replica_identity_full=`,
  `debezium_pg_config`, `register_connector`, `wait_for_connector`, `pg_exec`, `topic_name`,
  `read_cdc_events`, `op_counts`, `teardown`) + `kafka-python` (inside `read_cdc_events`). No
  `sparkContext`/RDD — Connect-safe.
- **Run against:** the CDC stack (`make cdc-up`): Postgres `localhost:5432`, Kafka Connect
  `localhost:8083`, Kafka host listener `localhost:29092`. Browse the data topics live in
  **kafka-ui** at http://localhost:8080.
- **Isolation:** two source tables (`cdc6_orders`, `cdc6_orders_full`) and two connectors
  (`cdc6-orders`, `cdc6-orders-full`). **Laptop-safe:** ~20-row tables, bounded topic reads, both
  connectors/tables/topics removed at start (clean slate) **and** at teardown.
- **Time:** ~10–12 min.

> **Read this first — honesty about timing.** The CDC parts here are deterministic: a `DELETE`
> *will* produce a `d` event and (with `tombstones.on.delete=true`, the default) a tombstone, and
> `REPLICA IDENTITY` *will* change what `before` contains. What is **not** instant is the streaming
> **decode lag** — Debezium reads the WAL on its own clock — so the notebook does a short bounded
> `time.sleep(5)` after each DML and reads the topic with a bounded `max_ms`; it never blocks
> waiting on the broker. One thing we **describe rather than execute**: suppressing the tombstone
> (`tombstones.on.delete=false`, or an `ExtractNewRecordState` SMT with `delete.handling.mode`) —
> we keep the default on so you can *see* the tombstone, and explain how you'd turn it off.

---

## 1. The scenario

A `customers` (here `orders`) table is the source of truth for a lakehouse mirror kept in sync by
CDC. Inserts and updates flow fine. Then a row is **deleted** in Postgres — and two questions
surface that every CDC pipeline must answer:

1. **How does a delete travel?** The sink has to learn *"remove this key,"* and a compacted Kafka
   topic has to be allowed to physically drop the key's history. Debezium does both with a **`d`
   event + a tombstone**.
2. **What did the row look like before it changed/vanished?** An audit trail, a "what was the old
   status?" rule, or a downstream that keys on a non-PK column all need the **old values** — and
   whether they're available is governed entirely by **`REPLICA IDENTITY`**, a property of the
   *source table*, not of Debezium.

This module makes both concrete and puts the two replica-identity behaviors **side by side**.

## 2. Break it — the default `before` is (almost) empty

We seed `public.cdc6_orders` with the **default** replica identity
(`seed_orders("cdc6_orders", replica_identity_full=False)`), register a Debezium connector, then run
a real **`UPDATE`** and **`DELETE`** against one row. Reading the topic back:

- the **update** arrives as `op="u"` — but its **`before`** carries **only the primary key**
  (`{"id": …}`), every other column `null`/absent;
- the **delete** arrives as `op="d"` with **`after=null`** and, again, a **key-only `before`**;
- a **tombstone** (a `None`-op, null-value record) follows the delete on the same key.

So with default identity you *can* apply the delete (you know the key), but you **cannot tell what
the row used to be** — the old `customer`, `amount`, `status` were never written to the WAL.

## 3. Detect it

| Where | What you see |
|-------|--------------|
| `read_cdc_events(topic)` → the `u` event's `before` (default identity) | **only `{"id": …}`** — key columns present, the rest null/absent |
| `read_cdc_events(topic)` → the `d` event | `op="d"`, **`after=null`**, `before` again **key-only** |
| `op_counts(events)` after the delete | includes a **`tombstone`** entry (the `None`-op record) |
| `read_cdc_events(topic)` on the **FULL** table's `u`/`d` events | `before` now carries the **whole old row** (`id`,`customer`,`amount`,`status`,`updated`) |
| **kafka-ui** → topic `dbz.public.cdc6_orders` → **Messages** | the `d` record (value with `op:d`, `after:null`) immediately followed by a record whose **value is null** (the tombstone) |

The unmistakable signatures: a delete is a **`d` (after=null) + a null-value tombstone**; and the
**richness of `before`** flips entirely on the table's `REPLICA IDENTITY`.

## 4. Diagnose

- **Why the tombstone?** Kafka **log compaction** keeps the latest value per key. To let a deleted
  key be *physically* removed from a compacted topic, the changelog needs a record that says "this
  key has no value" — a **null-value record = a tombstone**. Debezium emits one after every delete
  when **`tombstones.on.delete=true`** (the default in `debezium_pg_config`). The compactor retains
  the tombstone for `delete.retention.ms`, then drops the key entirely. (Contrast **KAF-4**, which
  covers compaction/tombstones from the *topic* side.)
- **Why is `before` key-only by default?** `REPLICA IDENTITY` tells Postgres **what to write to the
  WAL for the OLD row** on an UPDATE/DELETE. The default (`DEFAULT`) logs only the columns of the
  **primary key / replica-identity index** — enough to *identify* the row, nothing more. So
  Debezium's `before` can only contain the key.
- **What `REPLICA IDENTITY FULL` changes.** `FULL` tells Postgres to write the **entire old row** to
  the WAL on every UPDATE/DELETE. Debezium then fills `before` with **all columns**. The cost is a
  **larger WAL** (every change now carries a full old-row image) — paid on the source, all the time,
  whether or not any consumer needs the old values.
- **The trade-off in one line:** default identity = *"enough to delete, not enough to audit"*; FULL
  = *"complete old-row image, at the price of more WAL."*

## 5. Fix it / guidance

| Need | Setting | Why / cost |
|------|---------|------------|
| Apply deletes/updates downstream (key is enough) | **default** `REPLICA IDENTITY` | the PK in `before` (and the message key) is all a keyed `MERGE`/upsert needs; minimal WAL |
| Old values: audit trail, "previous status", before/after diffs | **`ALTER TABLE … REPLICA IDENTITY FULL`** | `before` carries the full old row; costs a bigger WAL on every UPDATE/DELETE |
| Key on a **non-PK** unique column | `REPLICA IDENTITY USING INDEX <unique_idx>` (or `FULL`) | logs that index's columns as the identity so they appear in `before` |
| Let a compacted topic drop deleted keys | keep **`tombstones.on.delete=true`** (default) | the null-value tombstone is what lets compaction GC the key |
| Want delete info **inside one flattened record** instead of a separate tombstone | `ExtractNewRecordState` SMT (`delete.handling.mode=rewrite`/`drop`) + often `tombstones.on.delete=false` | flattens the envelope and adds a `__deleted` flag; suppresses the standalone tombstone — *described here, not executed* |

**Downstream handling (forward-ref CDC-7).** A `d` event (plus its tombstone) means *"remove this
key from the sink."* In **Spark → Iceberg** that's an idempotent upsert keyed on the PK:

```sql
MERGE INTO iceberg_catalog.mirror.orders t
USING cdc_batch s
ON t.id = s.id
WHEN MATCHED AND s.op = 'd' THEN DELETE
WHEN MATCHED               THEN UPDATE SET *
WHEN NOT MATCHED AND s.op <> 'd' THEN INSERT *
```

— the `d` branch deletes the key; tombstones (null values) are filtered out before the MERGE since
the `d` event already carries the delete. The full pipeline is **CDC-7**.

## 6. Prove it

1. **The before-image table (the core proof).** Side by side, for the **same logical UPDATE/DELETE**
   on two tables that differ *only* in replica identity:

   | Table (`REPLICA IDENTITY`) | `u` event `before` | `d` event `before` |
   |----------------------------|--------------------|--------------------|
   | `cdc6_orders` (**DEFAULT**) | `{"id": k}` — key only | `{"id": k}` — key only |
   | `cdc6_orders_full` (**FULL**) | full old row (all columns) | full old row (all columns) |

   Same DML, same connector settings — the **only** difference is `REPLICA IDENTITY`, and that alone
   decides whether you can see the old values.

2. **Tombstone count.** `op_counts(events)` on the deleted table reports a **`tombstone`** entry
   (the `None`-op null-value record) alongside the `d`, proving the delete→tombstone pair.

## 7. Takeaways & "in real production…"

- **A delete is two records.** Build sinks that handle the `d` (after=null) event *and* tolerate the
  **tombstone** (null value) — don't let a null-value record crash your deserializer or get counted
  as a real row. In compacted topics the tombstone is *load-bearing*: it's what lets the key be GC'd.
- **`REPLICA IDENTITY` is a source-side decision with a downstream blast radius.** It's set on the
  Postgres table, but it determines what every CDC consumer can ever know about old values. Decide it
  per table: **default** where the PK is enough to apply changes; **FULL** where you genuinely need
  old values — and budget for the extra WAL it writes on every UPDATE/DELETE.
- **Don't reach for FULL reflexively.** It's a permanent WAL tax. If only a few tables need audit/old
  values, set FULL only there; consider `USING INDEX` when a non-PK unique key is what you join on.
- **Tombstone handling is a config, not a given.** `tombstones.on.delete=true` (default) emits them;
  an `ExtractNewRecordState` SMT with `delete.handling.mode` can flatten/suppress them and surface a
  `__deleted` flag instead — choose based on whether your sink consumes the raw envelope or a
  flattened record.
- **Tie it to compaction (KAF-4) and the upsert sink (CDC-7).** Tombstones + a compacted topic +
  a keyed idempotent `MERGE` is the standard way a lakehouse mirror stays an exact, delete-aware copy
  of its source.

## 8. Teardown

`teardown("cdc6-orders", "cdc6_orders")` and `teardown("cdc6-orders-full", "cdc6_orders_full")`
each delete the connector, drop its replication slot, drop the table, and delete the Debezium data
topic. `make clean` also clears local `.tmp/` state. (The notebook also tears both down at the
*start* so a re-run begins from a clean slate.)
