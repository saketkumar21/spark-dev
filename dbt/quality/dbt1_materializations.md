# DBT-1 — Materializations & cost

> **Break → Detect → Fix → Prove.** A dbt model is just a `SELECT`; the **materialization**
> decides what dbt *does* with it — recompute it on every read (view), store it once (table),
> inline it into its consumer (ephemeral), or only process new rows (incremental). Pick the wrong
> one and you either pay a full rebuild every run, or push that cost onto every reader. This module
> builds one model in each materialization and reads the cost off both sides of the ledger: **build
> cost** (what `dbt run` pays) vs **read cost** (what every downstream query pays).

This track expands the dbt project in [`dbt/`](README.md). Run dbt with:

```bash
cd dbt && source .env && dbt <cmd>
```

- **Connection:** Thrift → the unified Spark server (`make up`), catalog `spark_catalog`
  (Delta / Hive managed tables). Spark UI at http://localhost:4040.
- **Models used:** `staging/stg_customers` + `staging/stg_orders` (view),
  `marts/dim_customers` + `marts/agg_customers` (table), `marts/fct_orders` (incremental),
  `intermediate/int_high_value_orders` (ephemeral) → `marts/high_value_orders` (view).
- **Laptop-safe:** the seeds are ~15 rows each; nothing here generates volume. The point is the
  *shape* of the cost, not the size.

---

## 1. The scenario

You have a layered dbt project: cleaned **staging** models, enriched **marts**, and some shared
intermediate logic. Every model compiles to one `SELECT`. The question DBT-1 answers is: for each
model, *what object does dbt create in the warehouse, and who pays for it?* The four answers are
the four materializations.

| Materialization | Object created? | Build cost (`dbt run`) | Read cost (every query) |
|-----------------|-----------------|------------------------|-------------------------|
| **view**        | a **view** (no data stored) | cheap — just a `CREATE VIEW` | **the full query, re-run every read** |
| **table**       | a **table** (data stored) | **full rebuild every run** | cheap — scan stored rows |
| **ephemeral**   | **none** — inlined as a CTE | none (no statement of its own) | folded into the consumer's cost |
| **incremental** | a **table**, built once | **only new/changed rows** after the first run | cheap — scan stored rows |

The rest of this README walks each one against the real models.

## 2. Break it — the cost lives on the side you weren't looking at

### view — cheap to build, expensive to read (and it compounds)

`stg_customers`, `stg_orders` and `high_value_orders` are views (`+materialized: view` is the
default for `staging/`; the marts view sets it explicitly):

```sql
{{ config(materialized='view') }}
```

`dbt run` on a view does almost nothing — it issues a `CREATE VIEW`. No data is stored. The catch:
**the `SELECT` runs in full on every read**, and if a view selects from another view, the reader
recomputes the *whole chain*. Stack `stg_orders` → an intermediate view → a marts view and a single
query at the top re-derives every layer beneath it. On Spark, where each layer can mean a fresh
scan + shuffle, this is **view-chain bloat**: the build is free, but you've quietly moved a large,
repeating cost onto every consumer.

### table — fast to read, but you pay a full rebuild every run

`dim_customers` and `agg_customers` are tables (the project default `+materialized: table`):

```sql
-- dim_customers: enriched customer dimension (region, tier_rank, tenure_segment)
-- agg_customers: per-tier aggregate (uses QUALIFY → transpiled by dbt-spark-qualify)
```

`dbt run` **materializes** the result once, so reads are a cheap scan of stored rows. The cost
moves to build time: a plain table is **dropped and fully recreated every single run** — even if
only one source row changed. Cheap on 15 rows; on a billion-row fact this is the bill that makes
people reach for *incremental* (DBT-2).

### ephemeral — no object at all

`int_high_value_orders` is ephemeral:

```sql
{{ config(materialized='ephemeral') }}
select order_id, customer_id, amount, status
from {{ ref('fct_orders') }}
where amount > 100
```

dbt creates **no database object** for it. Instead it compiles the model into an **inlined CTE**
inside every model that `ref()`s it. Its consumer, `high_value_orders`, is a view that just selects
from it — so the ephemeral logic is pasted into the view's compiled SQL. Ephemeral is for **DRY
intermediate logic** you want to reuse without littering the warehouse with throwaway objects. The
trade-off: it can't be queried directly, it can't be tested in isolation, and inlining it into many
consumers duplicates the work (and can bloat the compiled SQL).

### incremental — only the new rows

`fct_orders` is incremental (the deep dive is [DBT-2](dbt2_incremental.md)):

```sql
{{ config(materialized='incremental', file_format='delta',
          incremental_strategy='merge', unique_key='order_id') }}
```

Like a table, it stores its result and is cheap to read. Unlike a table, after the first build it
processes **only new/changed rows** instead of rebuilding — the answer to the table's full-rebuild
bill on large data.

## 3. Detect it — read the compiled SQL and the warehouse

The materialization is invisible in the model file's `SELECT`; you see it in two places:

- **Compiled SQL** — `target/compiled/spark_dev/models/.../<model>.sql` after a run. For
  `high_value_orders` you'll see the **ephemeral model inlined as a CTE**, not a `ref` to a table.
- **The warehouse** — `dim_customers` exists as a **table**, `stg_customers` as a **view**, and
  `int_high_value_orders` **does not exist as any object**. That absence is the tell that ephemeral
  produced nothing.

## 4. Diagnose

> **Every materialization moves cost between build time and read time — there is no free one.**

- A **view** is a saved query: free to build, re-executed on every read; chained views recompute
  the whole chain.
- A **table** is a snapshot: cheap reads, but a full rebuild each run.
- **Ephemeral** is textual reuse: no object, no independent cost — its work is folded into (and
  duplicated across) its consumers.
- **Incremental** is the escape hatch for tables that got too big to rebuild.

## 5. Fix it — match materialization to access pattern

- **View** when the model is cheap to compute, read rarely, or must always reflect live upstream
  data — and the chain underneath it is shallow. Watch for view-chain bloat: if a deep stack of
  views is read often, **materialize a midpoint as a table**.
- **Table** when reads are frequent and the rebuild is affordable. The default for marts here.
- **Ephemeral** for shared intermediate CTE logic you don't want to persist
  (`int_high_value_orders`). Promote it to a table/view the moment you need to query or test it
  directly.
- **Incremental** when a table's full rebuild gets too expensive — process new rows only
  ([DBT-2](dbt2_incremental.md)).

**When a full-refresh is unavoidable** even on an incremental model: the model logic changed, the
schema changed, or **late data fell outside the incremental window** (the late-arrival problem →
DBT-3). In those cases you rebuild from scratch with `dbt run --full-refresh`.

### Verified command

```bash
cd dbt && source .env
dbt run -s high_value_orders
```

dbt builds `high_value_orders` and its upstream graph. The ephemeral `int_high_value_orders` is
**inlined as a CTE** — no object is created for it — so the only thing materialized for that leaf is
the **view** `high_value_orders`.

## 6. Prove it

Read the cost off the two ledgers. Build cost is what `dbt run` does; read cost is what a downstream
query pays.

| Materialization | Model | Object created? | Build cost | Read cost |
|-----------------|-------|-----------------|-----------|-----------|
| view        | `stg_customers`, `high_value_orders` | view (no data) | cheap (`CREATE VIEW`) | **full query every read; chains recompute the chain** |
| table       | `dim_customers`, `agg_customers` | table (data stored) | **full rebuild each run** | cheap (scan stored rows) |
| ephemeral   | `int_high_value_orders` | **none — inlined CTE** | none of its own | folded into the consumer |
| incremental | `fct_orders` | table (built once) | **new rows only** after first run | cheap (scan stored rows) |

The proof that ephemeral worked: after `dbt run -s high_value_orders`, the warehouse has the
`high_value_orders` **view** but **no `int_high_value_orders` object**, and its logic appears as a
CTE in the compiled SQL.

## 7. Takeaways & "in real production…"

- **Materialization is a cost-placement decision, not a detail.** Default to **view** for staging
  (cheap, always-fresh), **table** for marts read often, **incremental** for facts too big to
  rebuild, **ephemeral** for shared CTE logic.
- **Watch view-chain bloat.** Deep stacks of views read frequently re-derive the whole chain on
  every query — on Spark that's repeated scans + shuffles. Materialize a midpoint as a table.
- **Ephemeral keeps models DRY without warehouse clutter**, but you can't query or test it directly,
  and it's duplicated into every consumer — promote it the moment either matters.
- **A table's full rebuild is the bill that pushes you to incremental** ([DBT-2](dbt2_incremental.md)),
  and a changed schema / late data is what pushes you *back* to `--full-refresh` (DBT-3).

## 8. Teardown

These models live in the shared [`dbt/`](README.md) project; there's nothing module-specific to
tear down. `dbt build` rebuilds them from the seeds, and `make clean` clears all generated data
under `.tmp/`.
