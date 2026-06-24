# DBT-5 — Schema-change handling (`on_schema_change`)

> **Break → Detect → Fix → Prove.** An upstream column appears mid-stream and your
> **incremental** model has months of rows already written. Does the next `dbt run` error,
> silently drop the new column, `ALTER TABLE` to add it, or fully reconcile? dbt's
> `on_schema_change` config decides — and getting it wrong means either a broken build or
> silently lost data. This module walks every mode on a real incremental model.

- **Model:** [`dbt/models/marts/fct_orders_evolving.sql`](../../dbt/models/marts/fct_orders_evolving.sql)
- **Extends:** the [`dbt/`](../../dbt/) project (this is a dbt-modeling module, not a notebook).
- **Run via:** `cd dbt && source .env && dbt <cmd>` — Thrift JDBC → the unified Spark server,
  Delta-backed managed tables.
- **Time:** ~5 min. **Laptop-safe:** 14 rows total, all under `.tmp/`; a plain `dbt build`
  cleanly resets it (see the honesty note below); `make clean` clears the warehouse.

---

## 1. The scenario

`fct_orders_evolving` reads typed rows from `stg_orders` and is **incremental** — each run
appends only orders newer than the current max `order_id` (`incremental_strategy='append'`).
Quarter one, the fact has `(order_id, amount, status)`. Quarter two, finance wants tax broken
out, so a new run introduces a `tax = round(amount * 0.08, 2)` column — gated behind an
`add_tax` var so the lab can toggle it:

```sql
{{ config(
    materialized='incremental',
    file_format='delta',
    incremental_strategy='append',
    on_schema_change='sync_all_columns'
) }}
select order_id, amount, status
    {% if var("add_tax", false) %}
    , round(amount * 0.08, 2) as tax   -- a NEW column introduced on a later run
    {% endif %}
from {{ ref('stg_orders') }}
{% if is_incremental() %}
where order_id > (select max(order_id) from {{ this }})
{% endif %}
```

The question this module answers: when the SELECT's columns no longer match the existing
table, what does dbt do to the table on the **incremental** run?

## 2. The four `on_schema_change` modes

`on_schema_change` only applies to **incremental** models (full-refresh always rebuilds from
the current SELECT, so there's nothing to reconcile). The modes:

| Mode | Behavior when the SELECT's columns differ from the table |
|------|-----------------------------------------------------------|
| **`fail`** | Raise an error and stop — the run aborts so a human decides. Safest for contracts. |
| **`ignore`** (default) | Keep the existing table schema; **silently drop** any new column from the insert. The data is computed and then thrown away — the classic "why is my new column empty?" |
| **`append_new_columns`** | `ALTER TABLE … ADD COLUMN` for new columns; existing rows get **NULL**. Removed columns are left in place. |
| **`sync_all_columns`** | Add **and** remove columns so the table matches the SELECT exactly; type changes are synced where the adapter allows. |

> **The classic gotcha — non-nullable adds.** ADD COLUMN backfills existing rows with `NULL`.
> If the new column is declared `NOT NULL`, the old rows can't satisfy it, so the `ALTER`
> fails. You must either allow NULL on the new column or backfill it in the same migration.
> Spark/Delta `ADD COLUMN` is nullable by default, which is exactly why the additive modes are
> safe here — but on a stricter store this is where schema evolution bites.

## 3. Two honesty notes (read before running)

- **Why `sync_all_columns`, not `append_new_columns`.** This model is configured
  `sync_all_columns` *specifically so the lab is robust to run order*. With `add_tax`
  **not** set, the SELECT has no `tax` column; `sync_all_columns` then cleanly **removes**
  `tax` to match, so a plain `dbt build` resets the table instead of erroring. Under
  `append_new_columns` (or `fail`) a no-var run after a `tax` run would either leave a stale
  column or abort — annoying in a teaching repo you re-run constantly. Swap the mode and
  re-run the sequence below to *see* the difference.
- **Why `file_format='delta'` (the Thrift+Iceberg classloader gotcha).** Iceberg **managed**
  tables aren't reliably writable from the Thrift server (the HiveServer2 classloader
  isolation bug — see [CLAUDE.md](../../CLAUDE.md)), so every dbt mart in this repo uses
  Delta. Notebooks use Iceberg directly over Spark Connect; dbt uses Delta over Thrift. Schema
  evolution behaves the same for this lab on either format — the lesson is dbt's
  `on_schema_change`, not the storage layer.

## 4. Break it — add a column on the incremental run (VERIFIED sequence)

**RUN 1 — establish the table, no `tax` column** (full-refresh so we start clean):

```bash
dbt run -s stg_orders fct_orders_evolving --full-refresh \
  --vars '{load_through: "2024-03-03 23:59:59"}'
```

→ **12 rows, columns `(order_id, amount, status)` — no `tax`.** (`load_through` caps
`stg_orders` at orders loaded on/before 2024-03-03.)

**RUN 2 — widen the load window AND flip `add_tax`** (incremental — appends new orders and
must reconcile the new column):

```bash
dbt run -s stg_orders fct_orders_evolving \
  --vars '{load_through: "2024-03-04 23:59:59", add_tax: true}'
```

→ **14 rows total; the `tax` column is ADDED.** The 12 pre-existing rows read back
`tax = NULL` (they were inserted before the column existed); the **2 newly appended** rows
(`order_id > 12`'s max) carry a computed tax value, so `count(tax) = 2`.

The "break" isn't a crash — it's the mental trap that an additive change either rewrites
history or fails. With the right mode it's a metadata `ALTER` plus NULL-backfill, and only
fresh rows get values.

## 5. Detect it — compare columns, count populated values

Schema change is a **metadata** event, so the tells are in the table's column list and a
populated-count, not the Spark UI Stages tab:

- **Column list before vs after** — `tax` is absent after RUN 1, present after RUN 2. From
  dbt: `dbt run-operation` or just query the table.
- **Populated vs NULL split** — the smoking gun that the add was non-destructive:

  ```sql
  select count(*) as total_rows,
         count(tax) as rows_with_tax            -- count() skips NULLs
  from spark_catalog.marts.fct_orders_evolving;
  ```

  → `total_rows = 14`, `rows_with_tax = 2`. Old rows survived (still 14, not 2); the new
  column is real (2 populated); history wasn't rewritten (the other 12 are NULL, not
  recomputed).
- **dbt run log** — on the RUN 2 the log shows the `alter table ... add columns` dbt issued to
  bring the table in line; that line is the audit trail of the schema sync.

## 6. Diagnose

> **`on_schema_change` is the contract between an evolving SELECT and an existing
> incremental table.** dbt diffs the SELECT's columns against the destination table on every
> incremental run and applies the configured policy.

- **`ignore`** computes the new column then drops it on insert → the column never reaches the
  table, and the data is silently lost. The #1 "my new field is always empty" bug.
- **Additive modes** (`append_new_columns` / `sync_all_columns`) `ALTER TABLE ADD COLUMN`,
  which only edits metadata — existing files aren't rewritten, so old rows read back `NULL`.
- **`sync_all_columns`** additionally **drops** columns no longer in the SELECT — powerful,
  but it means a column vanishing from your model **removes it from the table** (data in old
  files becomes unprojectable). Exactly the behavior that makes a no-var `dbt build` reset this
  lab.
- **`fail`** is the right call when the table is a published contract: stop and make a human
  migrate, rather than mutating a downstream-consumed schema automatically.

## 7. Fix it — pick the mode for the table's role

- **Append-only facts that gain optional columns** → `append_new_columns` (or
  `sync_all_columns` if you also want stale columns reaped). New fields arrive without a
  full-refresh; old rows are NULL until backfilled.
- **Published / contracted tables** → `fail`, and migrate deliberately (a `--full-refresh`, or
  an explicit DDL + backfill) so a consumer is never surprised.
- **Never leave `ignore` on a model whose schema is meant to grow** — it's the silent
  data-loss default. Set the mode explicitly.
- **Non-nullable additions need a plan** — add as nullable then backfill, or backfill in the
  same migration; don't expect `ADD COLUMN` to populate history.
- **When in doubt, `--full-refresh`** rebuilds the whole table from the current SELECT and
  sidesteps reconciliation entirely (at the cost of reprocessing all rows) — fine on a 14-row
  lab, a real decision at scale.

## 8. Prove it

| Run | `add_tax` | Total rows | Rows with `tax` | `tax` column present? |
|-----|-----------|-----------:|----------------:|-----------------------|
| RUN 1 (`--full-refresh`) | _(unset)_ | 12 | — | **No** |
| RUN 2 (incremental) | `true` | 14 | 2 | **Yes** (old 12 = NULL) |

The proof is the second row: the column was **added** (present + 2 populated) without
rewriting the 12 existing rows (still there, NULL for `tax`) and without a full rebuild — a
pure metadata `ALTER` driven by `on_schema_change='sync_all_columns'`.

## 9. Takeaways & "in real production…"

- **`on_schema_change` only governs incremental models** — full-refresh always matches the
  current SELECT. Set it explicitly; the silent `ignore` default loses new columns.
- **Additive evolution is metadata-only** — `ADD COLUMN` backfills NULL and doesn't rewrite
  history, so it's cheap even on huge facts. `sync_all_columns` also *drops* removed columns —
  power and footgun in one.
- **Non-nullable adds are the classic break** — backfill or allow NULL; `ADD COLUMN` won't
  populate old rows.
- **This is the dbt-layer counterpart to [LAK-6](../../iceberg/schema_evolution/)** — the
  table format absorbs the schema change in metadata, and `on_schema_change` decides whether
  the *model* lets it through. Both must agree or the pipeline breaks even when the storage is
  fine.

## 10. Teardown

A plain `dbt build` (no vars) re-runs the model with `add_tax` off; `sync_all_columns` removes
the `tax` column to match the SELECT, returning the table to its base shape. `make clean`
removes everything under `.tmp/` for a fully fresh warehouse.
