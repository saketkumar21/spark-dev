# CDC-8 — CDC schema evolution

> **Break → Detect → Fix → Prove.** The upstream table changes — someone runs `ALTER TABLE` on the
> source. Postgres logical decoding does **not** emit a DDL event. There is no "schema changed"
> message on the topic. Instead, **the very next change event simply carries the new shape**: an
> added column just appears in the event's `after` object, with no connector restart and no signal.
> Additive changes flow through transparently; **renames / drops / type-narrowing** can desync a
> strict downstream consumer and may need an ad-hoc snapshot. This module makes the additive case
> concrete — you watch the `after` key-set grow across an `ALTER` — and **describes** the breaking
> cases precisely (they can't be staged deterministically and laptop-safely in one run).

- **Notebook:** [`cdc8_schema_evolution.ipynb`](./cdc8_schema_evolution.ipynb)
- **Toolkit used:** [`common.cdc_helpers`](../../common/cdc_helpers.py) — `seed_orders`,
  `debezium_pg_config`, `register_connector`, `wait_for_connector`, `connector_status`,
  `topic_name`, `read_cdc_events`, `op_counts`, `pg_exec`, `teardown`. No Spark for the live demo
  (`kafka-python` + the Connect REST API only); the downstream-evolve section ties to **LAK-6**.
- **Run against:** the CDC stack (`make cdc-up` → Postgres + Kafka Connect + Kafka). Producers/admin
  use the host listeners (`localhost:5432`, `localhost:8083`, `localhost:29092`); inspect the topic
  live in **kafka-ui** at http://localhost:8080 → topic `dbz.public.cdc8_orders`.
- **Time:** ~10–12 min. **Laptop-safe:** one tiny (≤20-row) table, one connector, bounded topic
  reads, short bounded `sleep`s, full teardown at the **start** (clean slate for re-runs) and the
  **end**.

> **Read this first — honesty about what flows and what doesn't.** The thing this module proves
> outright is **deterministic and observable**: after an upstream `ADD COLUMN`, a fresh change event's
> `after` object contains the new column, while events captured *before* the `ALTER` did not — the
> envelope schema evolved on its own. What we **describe** rather than stage:
> (a) Debezium emits **no explicit DDL event** for a Postgres `ALTER` — the schema change is *observed
> only via the changed payload* (and Debezium's internal schema history, which for Postgres is
> reconstructed from the WAL, not surfaced as a topic event); (b) a **breaking** change (DROP / RENAME
> COLUMN, type narrowing) can break a strict downstream schema and may require an **ad-hoc /
> incremental snapshot** (a `execute-snapshot` signal) or a connector recreate — our table is too small
> and the failure too consumer-specific to force deterministically, exactly like the SPK-2/3 OOM and
> CDC-3 mid-snapshot cases. The notebook runs top-to-bottom under `nbconvert` regardless.

---

## 1. The scenario

A `cdc8_orders` table has been streaming cleanly into Kafka via Debezium for weeks: every row carries
`(id, customer, amount, status, updated)` and downstream consumers (a Spark→Iceberg upsert sink like
CDC-7, a dashboard, a search index) are built against exactly those fields. Then product ships a
feature: orders can now carry a `discount`. An engineer runs, on the **source** database:

```sql
ALTER TABLE public.cdc8_orders ADD COLUMN discount NUMERIC(10,2) DEFAULT 0;
```

Nothing is reconfigured on the connector. No one touches Kafka Connect. The question every CDC
operator eventually asks: **what does the connector do when the upstream schema changes underneath
it?** Does it emit a "schema changed" event? Restart? Fail the task? Silently keep the old shape?

The answer for Postgres logical decoding: **none of those for an additive change.** There is no DDL
event. The connector keeps streaming, and the **first change event for a row touched after the
`ALTER`** simply has `discount` in its `after` object. The schema *of the envelope* evolved with no
ceremony. That is the good case — and it's also the trap, because a *breaking* change (drop, rename,
narrow) flows through the **same silent channel**, and the first your strict downstream consumer hears
of it is a deserialization error or a missing/extra column.

## 2. Break it — `ALTER TABLE` upstream, then read the topic across the change

We do the whole thing through the source database and the topic — no Spark needed to see it:

1. **Seed + register.** `seed_orders("cdc8_orders", n=20)` creates `public.cdc8_orders` with the v1
   shape and `register_connector(debezium_pg_config("cdc8-orders", "cdc8_orders"))`; wait for
   `RUNNING`. The connector snapshots the 20 rows (`op="r"`).
2. **Capture a baseline event.** Read the topic and print `sorted(after.keys())` for a snapshot/`r`
   event — e.g. `['amount', 'customer', 'id', 'status', 'updated']`. This is the **v1 envelope**.
3. **Evolve the source.** `ALTER TABLE public.cdc8_orders ADD COLUMN discount NUMERIC(10,2) DEFAULT 0`.
   No DDL event appears on the topic — we note that explicitly.
4. **Touch a row with the new column.** `INSERT` a new row that sets `discount`, and `UPDATE` an
   existing row's `discount`. `time.sleep(5)` for the decode lag.
5. **Read again.** The new `c` / `u` events' `after` objects now include `discount`. We print
   `sorted(after.keys())` for the post-`ALTER` event — `['amount', 'customer', 'discount', 'id',
   'status', 'updated']` — next to the v1 set. The envelope grew a column with **no connector
   restart and no DDL event**.

The "break" isn't a crash — it's the realization that a schema change reached your pipeline through a
**back channel** (the payload), not an explicit contract event. For an additive change that's benign;
for a breaking one it's a silent desync waiting to happen.

## 3. Detect it

| Where | What you see |
|-------|--------------|
| `sorted(ev_before["after"].keys())` (a pre-`ALTER` `r`/`c` event) | `['amount','customer','id','status','updated']` — the v1 shape |
| the topic, immediately after the `ALTER` (before any DML) | **nothing new** — logical decoding emits **no DDL event**; the schema change is invisible until a row changes |
| `sorted(ev_after["after"].keys())` (a post-`ALTER` `c`/`u` event) | `['amount','customer','discount','id','status','updated']` — `discount` has appeared |
| set diff `after_keys - before_keys` | `{'discount'}` — the single, additive delta, observed purely from the payload |
| `connector_status("cdc8-orders")` | still `RUNNING` with a `RUNNING` task — the connector never restarted or faulted |

The headline signal is the **diff of `after` key-sets across the `ALTER`**: the new key appears in
events emitted *after* the schema change and is absent from events emitted *before* it — and it got
there with no DDL event and no connector lifecycle change. For a **breaking** change the tell is
different and lives **downstream**: a strict sink (a Spark schema, an Avro/JSON-Schema contract, an
Iceberg table without the column) throws on the unknown/missing field — that error is your detector
(see §5).

## 4. Diagnose

- **Logical decoding carries data, not DDL.** Postgres's `pgoutput` plugin (CDC-2) decodes **row
  changes** from the WAL. A DDL statement like `ALTER TABLE` changes the catalog but is **not itself a
  logical-decoding message** you receive as a change event. Debezium learns the new shape by observing
  the **relation metadata** attached to the next change for that table and reconstructs its internal
  schema from it — so the new column surfaces **only when a row is inserted/updated/deleted after the
  `ALTER`**, embedded in that event's `before`/`after`. (Some Debezium connectors — notably MySQL —
  keep an explicit **schema-history topic** and parse DDL; the Postgres connector does **not** parse
  DDL and has no DDL events. This is a per-connector detail worth knowing.)
- **Additive (`ADD COLUMN`) is backward-compatible, so it flows transparently.** Old events simply
  lacked the field; new events have it. A tolerant consumer (one that reads fields by name and treats
  absent fields as `NULL`/default) absorbs it with zero changes — which is why additive evolution is
  the safe default everywhere from Avro to Iceberg.
- **Renames / drops / type-narrowing are *not* backward-compatible.** A **drop** makes the field vanish
  from later events (a consumer requiring it now gets `NULL`/missing); a **rename** looks like *drop old
  + add new* on the wire (the old name's values stop arriving, a new key appears half-populated — the
  exact failure mode LAK-6 shows for positional Parquet); a **type narrowing** can make values
  unparseable against a stricter downstream type. Any of these can **desync a strict downstream schema**.
  Because there's still no DDL event, the connector keeps running and the breakage shows up *only* at
  the consumer — late, and far from the cause.
- **The remedy for a breaking change is an explicit re-sync.** When a consumer must be rebuilt against
  the new shape (e.g. it needs historical rows re-emitted with the new columns), trigger an **ad-hoc /
  incremental snapshot** via Debezium's **`execute-snapshot` signal** (a row written to a signal table),
  or recreate the connector. That re-reads the table — chunked and resumable for incremental snapshots —
  so the downstream store can be repopulated with the evolved schema. We **describe** this; staging a
  real downstream break is consumer-specific and not deterministic in one tiny run.

## 5. Fix it / guidance — evolve the sink (tie to LAK-6)

The connector did its job; the work is making the **downstream** tolerate evolution.

- **Prefer additive, backward-compatible changes.** `ADD COLUMN` with a default is the gold standard:
  it flows through CDC untouched and every tolerant consumer absorbs it. Make this the *normal* way the
  schema grows.
- **Evolve the Iceberg sink to match — additively, by field-id.** When the CDC stream gains `discount`,
  the downstream mirror just needs the column:

  ```sql
  ALTER TABLE iceberg_catalog.default.cdc8_orders_sink ADD COLUMN discount DECIMAL(10,2);
  ```

  This is **exactly LAK-6**: Iceberg assigns the new column a stable **field-id**, the change is
  **metadata-only** (no data-file rewrite — `data_files` stays flat), and old rows read back `NULL`
  for `discount` while new MERGE-ed rows carry the value. The Spark→Iceberg upsert sink (CDC-7) then
  keeps MERGE-ing; the new field lands in the new column. An additive upstream change maps to an
  additive sink change — both sides stay backward-compatible.
- **Coordinate breaking changes; never let them ride the silent channel.** A DROP/RENAME/narrow needs a
  planned migration: evolve consumers first (or dual-write), then change the source, and **re-snapshot**
  (`execute-snapshot` signal) if downstream needs history under the new shape. In dbt this is the
  `on_schema_change` setting (`fail` / `ignore` / `sync_all_columns`) — that's **DBT-5**.
- **Use contracts / a schema registry at scale.** With many consumers, put the envelope behind a
  **schema registry** (Avro/Protobuf + compatibility rules: `BACKWARD` allows adds, blocks unsafe drops)
  or a data contract, so a breaking change is **rejected at publish time** instead of discovered as a
  downstream incident. The registry turns "silent desync" into "CI failure".

## 6. Prove it

The proof is the **before/after `after`-key sets** the notebook prints:

```
baseline after keys (pre-ALTER) : ['amount', 'customer', 'id', 'status', 'updated']
post-ALTER  after keys          : ['amount', 'customer', 'discount', 'id', 'status', 'updated']
new keys (after - before)       : {'discount'}
connector state throughout      : RUNNING
DDL event observed on topic     : none
```

`discount` is present in events emitted **after** the upstream `ADD COLUMN` and absent from events
emitted **before** it — and it arrived with **no DDL event** on the topic and **no connector restart**
(`connector_status` stays `RUNNING`). The envelope schema evolved on its own, carried by the payload.
That single key-set diff is the whole demonstration: schema evolution in Postgres CDC is **observed**,
not **announced**.

## 7. Takeaways & "in real production…"

- **Postgres CDC has no DDL events — schema changes ride the payload.** The new shape appears in the
  next change event's `before`/`after`, not as a separate "schema changed" message. Your detector is a
  **diff of event keys over time**, not a DDL feed. (MySQL/Debezium *does* keep a schema-history topic;
  know which connector you run.)
- **Additive is safe; breaking is silent until it isn't.** `ADD COLUMN` flows transparently to tolerant
  consumers. DROP/RENAME/narrow flow through the *same* channel with no warning and surface as a
  **downstream** deserialization/contract error — late and far from the cause. Treat additive as the
  default and gate breaking changes behind a migration.
- **Evolve the sink additively, by field-id (LAK-6).** Mirror an upstream add with an Iceberg
  `ALTER TABLE … ADD COLUMN`: metadata-only, no rewrite, old rows read `NULL`. An additive source change
  maps cleanly to an additive sink change — both backward-compatible.
- **Breaking changes need a plan + a re-snapshot.** Coordinate consumer changes, then use an **ad-hoc /
  incremental snapshot** (`execute-snapshot` signal) to repopulate downstream under the new schema, or
  recreate the connector. Don't rely on the silent channel to carry a breaking change safely.
- **Contracts/registry turn desync into CI failures.** At scale, enforce compatibility (registry rules
  or data contracts) so an incompatible change is rejected at publish time instead of paging someone
  downstream. This is the same lesson as dbt's `on_schema_change` (**DBT-5**), one layer up.

## 8. Teardown

The notebook calls `teardown("cdc8-orders", "cdc8_orders")` at the **start** (clean slate for re-runs)
and again at the **end** — it deletes the connector, drops its replication slot, drops the table, and
deletes the Debezium topic so the next run starts clean. `make clean` clears any remaining local
`.tmp/` state.
