# DBT-2 — Incremental strategies on Spark / Iceberg

> **Break → Detect → Fix → Prove.** An incremental model avoids the table's full-rebuild bill by
> processing **only new/changed rows**. But "process new rows" hides a choice: do you **upsert**
> them by key (`merge`), **replace whole partitions** (`insert_overwrite`), or just **append**? The
> strategy you pick decides whether re-runs are idempotent — and on Spark it also decides whether
> the model even *works*, because `merge` needs a merge-capable file format. This module builds
> `fct_orders` as a `merge` incremental on Delta and proves a re-run adds new rows **with no
> duplicates**.

This track expands the dbt project in [`dbt/`](README.md). Run dbt with:

```bash
cd dbt && source .env && dbt <cmd>
```

- **Connection:** Thrift → the unified Spark server (`make up`), catalog `spark_catalog`
  (Delta / Hive managed tables). Spark UI at http://localhost:4040.
- **Model:** [`marts/fct_orders.sql`](../models/marts/fct_orders.sql), sourced from
  `stg_orders`.
- **Laptop-safe:** the `orders` seed is ~15 rows; the `load_through` var gates how many are
  "available" so the build is small and reproducible.

---

## 1. The scenario

`fct_orders` is the order fact table. It's incremental, keyed by `order_id`:

```sql
{{ config(
    materialized='incremental',
    file_format='delta',
    incremental_strategy='merge',
    unique_key='order_id'
) }}

select order_id, customer_id, amount, status, ordered_at, loaded_at
from {{ ref('stg_orders') }}

{% if is_incremental() %}
where ordered_at > (select max(ordered_at) from {{ this }})
                   - interval '{{ var("lookback_hours", 0) }}' hour
{% endif %}
```

`stg_orders` reads the seed but filters on a **`load_through`** var (`where loaded_at <=
'{{ var("load_through") }}'`) — that simulates "data available as of this load time", so we can
replay batches deterministically. Each `dbt run` is one batch arriving.

## 2. The three incremental strategies

`incremental_strategy` decides what dbt does with the new rows the model selects:

| Strategy | What it does | Dedups? | Format requirement |
|----------|--------------|---------|--------------------|
| **`merge`** | **upsert** by `unique_key` — update matching rows, insert the rest | **yes** (by key) | **merge-capable: Delta or Iceberg** |
| **`insert_overwrite`** | replace **whole partitions** the batch touches | per-partition (idempotent reload) | partitioned table |
| **`append`** | just add the rows — fastest, no matching | **no** | any |

`fct_orders` uses **`merge`**, which is why `file_format='delta'` is mandatory: a **plain Hive
table cannot run `MERGE`** on Spark — only a transactional format (Delta here, or Iceberg) supports
upsert-by-key. Set `merge` on a Hive table and the run fails. (This is also why the broader project
default catalog is `spark_catalog` with Delta/Hive: Delta gives you MERGE, Hive doesn't.)

- **`insert_overwrite`** is the right call when a batch re-delivers an entire partition (e.g. a
  daily reload of `dt=2024-03-04`) — overwriting the partition is naturally idempotent without a key.
- **`append`** is fastest and dedup-free — fine for immutable event logs where re-running can't
  happen, dangerous anywhere a batch might replay.

## 3. Break / Detect — why `merge` rewrites a whole partition

`merge` feels surgical — "update these few rows" — but under the hood Delta and Iceberg are
**copy-on-write** by default: to change *any* row in a data file, the engine **rewrites the entire
file** (a whole partition's worth) into a new file and swaps it in. So a `merge` that touches one
`order_id` can rewrite a full partition. On a big fact table that's real I/O, and it's exactly the
copy-on-write vs merge-on-read trade-off taught in
[LAK-8](../../iceberg/merge_cow_mor/) — MOR defers the rewrite by writing delete files instead.
You **detect** this in the Spark UI as a write stage that reads and rewrites far more data than the
handful of rows in the batch, and (on Iceberg) as new data files in the table metadata.

The `is_incremental()` block is what gates all of this:

- **First run / `--full-refresh`** → `is_incremental()` is false, the `where` is dropped, dbt builds
  the table from **all** available rows.
- **Subsequent runs** → it's true, the `where ordered_at > max(ordered_at) ...` filter narrows the
  scan to new rows only. That `max(ordered_at)` is the **incremental watermark** — and rows whose
  event time predates it (late arrivals) get dropped unless `lookback_hours` widens the window. That
  late-arrival problem is the bridge to **DBT-3**.

## 4. Diagnose

> **`merge` + `unique_key` makes re-runs idempotent; the watermark makes them cheap — but the
> watermark is also what silently drops late data.**

The `unique_key='order_id'` is the safety net: even if the incremental filter re-scans a row that's
already in the table (because a lookback window overlaps, or a batch replays), the `MERGE` matches
on `order_id` and **updates in place instead of inserting a duplicate**. That's what "idempotent"
buys you — you can re-run a batch and the row count doesn't drift.

## 5. Fix it — build, then incrementally load

### Verified: full build (first run)

```bash
cd dbt && source .env
dbt run -s stg_orders fct_orders --full-refresh \
  --vars '{load_through: "2024-03-03 23:59:59"}'
```

`load_through` exposes only the rows that landed on/before 2024-03-03 (seed rows `1000`–`1011`).
`--full-refresh` drops `is_incremental()`, so `fct_orders` is built from scratch → **12 rows**.

### Verified: incremental load (later batch)

```bash
cd dbt && source .env
dbt run -s stg_orders fct_orders \
  --vars '{load_through: "2024-03-04 23:59:59"}'
```

Now `load_through` admits the rows that landed on 2024-03-04 (`1100`, `1101`, …). With no
`--full-refresh`, `is_incremental()` is true: dbt selects only orders newer than the stored
`max(ordered_at)` and **`MERGE`s them in by `order_id`**. The new orders are added; nothing is
duplicated.

## 6. Prove it

| Run | Command flags | `is_incremental()` | Rows processed | Result in `fct_orders` |
|-----|---------------|--------------------|----------------|------------------------|
| **Full build** | `--full-refresh`, `load_through=2024-03-03 23:59:59` | **false** | **all 12** available rows | 12 rows (table built from scratch) |
| **Incremental** | `load_through=2024-03-04 23:59:59` | **true** | only orders past the watermark | newer orders **merged in by `order_id`, no duplicates** |

The proof of idempotency: re-running the incremental step does **not** grow the row count or create
duplicate `order_id`s — the `MERGE` upserts on the key. Counting `order_id` vs `count(distinct
order_id)` stays equal.

## 7. Takeaways & "in real production…"

- **Pick the strategy for the data shape.** `merge` for keyed upserts (CDC sinks, dimensions);
  `insert_overwrite` for idempotent partition reloads; `append` only for truly immutable,
  never-replayed events.
- **`merge` needs a transactional format.** Delta or Iceberg — **plain Hive can't MERGE**. That
  constraint alone often decides your table format.
- **`merge` is copy-on-write under the hood** — touching one row can rewrite a whole partition. On
  big tables, weigh COW vs merge-on-read ([LAK-8](../../iceberg/merge_cow_mor/)) and watch the write
  stage in the Spark UI.
- **`unique_key` is your idempotency guarantee** — without it, overlapping incremental windows or
  replayed batches duplicate rows.
- **The incremental watermark is a double-edged sword:** it makes runs cheap by scanning only new
  rows, but it **drops late-arriving data** that falls behind the high-water mark. Recapturing that
  with a `lookback_hours` window (and the `merge` dedup that makes the re-scan safe) is **DBT-3**.

## 8. Teardown

`fct_orders` lives in the shared [`dbt/`](README.md) project. `dbt run --full-refresh` rebuilds it
from the seed; `make clean` clears all generated data under `.tmp/`.
