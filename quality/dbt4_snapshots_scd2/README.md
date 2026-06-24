# DBT-4 — Snapshots / SCD Type 2

> **Break → Detect → Fix → Prove.** A `stg_customers` view always shows a customer's **current**
> tier — overwrite the source and the old value is gone. But "what tier was this customer on *when
> they placed that order*?" needs **history**. dbt **snapshots** implement **Slowly Changing
> Dimension Type 2 (SCD2)**: instead of overwriting, each tracked change **closes** the old row and
> **inserts** a new current one, so every version is preserved with a validity window. The "Break"
> here is the limitation snapshots remove (a view that forgets the past); the "Fix" is the snapshot
> that records every version; and the subtlety that bites is **frequency** — you only ever capture
> the states you actually snapshot at.

This expands the project under [`dbt/`](../../dbt/). Run everything from there:

```bash
cd dbt && source .env        # sets DBT_PROFILES_DIR + Thrift connection vars
dbt <cmd>                     # Thrift JDBC → Spark; snapshot is a Delta table in schema `snapshots`
```

- **Snapshot:** [`customers_snapshot`](../../dbt/snapshots/customers_snapshot.sql) — `strategy='check'`,
  `check_cols=['membership_tier']`, `unique_key='customer_id'`, `target_schema='snapshots'`, Delta.
- **Source:** [`snap_customers_src`](../../dbt/models/staging/snap_customers_src.sql) (view) — a
  `promote_c001` var flips **C001**'s tier from `gold` to `platinum`, so a source change is reproducible.
- **Data:** 20 customers from [`customers.csv`](../../dbt/seeds/customers.csv).
- **Laptop-safe:** 20 tiny rows, one var, no infra beyond the running Spark server (`make up`).

---

## 1. The scenario

`stg_customers` is a **view** — it reflects whatever the source says *right now*. If Alice (C001)
upgrades from `gold` to `platinum`, the view shows `platinum` and her `gold` history is gone. Any
report that joins orders to "the tier at order time" silently rewrites the past. SCD2 is the standard
fix: keep **every** version of each row, stamped with the window it was valid for.

dbt snapshots add four bookkeeping columns:

| Column | Meaning |
|--------|---------|
| `dbt_valid_from` | when this version became the truth |
| `dbt_valid_to`   | when it was superseded — **`NULL` ⇒ this is the *current* version** |
| `dbt_scd_id`     | surrogate key, unique per version |
| `dbt_updated_at` | when the snapshot recorded this version |

## 2. Break it → first snapshot (the baseline)

**Run 1 — build the source, then snapshot it:**

```bash
dbt run -s snap_customers_src
dbt snapshot -s customers_snapshot
```

Result: **20 versions, 20 current** — one row per customer, every one with `dbt_valid_to IS NULL`.
This is the initial load: dbt has never seen these keys, so it inserts a current version for each.
At this point the snapshot is no better than the view — it has captured *one* state. The point of
SCD2 only shows up on the **second** run, when the source changes.

## 3. Fix it → capture a change (SCD2 versioning)

Flip C001 to `platinum` (the `promote_c001` var rewrites the source view), then snapshot again. The
var must be passed to **both** commands — to `run` so the source view actually changes, and to
`snapshot` so the snapshot reads the changed source:

```bash
dbt run      -s snap_customers_src   --vars '{promote_c001: "yes"}'
dbt snapshot -s customers_snapshot   --vars '{promote_c001: "yes"}'
```

Now **C001 has 2 versions**:

- the old `gold` row is **closed** — its `dbt_valid_to` is set (no longer `NULL`), so it's the
  *historical* version;
- a new `platinum` row is **inserted** as the current version (`dbt_valid_to IS NULL`).

The other 19 customers are unchanged, so they each still have exactly one current row. dbt detected
the change because the **`check` strategy** compared `check_cols=['membership_tier']` against the
stored version and saw `gold ≠ platinum`.

## 4. Detect it — query the version history

```sql
SELECT customer_id, membership_tier, dbt_valid_from, dbt_valid_to
FROM   snapshots.customers_snapshot
WHERE  customer_id = 'C001'
ORDER BY dbt_valid_from;
```

You'll see two rows for C001: `gold` with a non-null `dbt_valid_to`, then `platinum` with
`dbt_valid_to IS NULL`. That closed-then-opened pair **is** the SCD2 signature.

**Current vs as-of-historical** — the two queries SCD2 enables:

```sql
-- CURRENT dimension (what stg_customers would show):
SELECT * FROM snapshots.customers_snapshot WHERE dbt_valid_to IS NULL;

-- AS-OF a point in time (the tier when an order was placed):
SELECT * FROM snapshots.customers_snapshot
WHERE '2024-03-02 00:00:00' >= dbt_valid_from
  AND ('2024-03-02 00:00:00' <  dbt_valid_to OR dbt_valid_to IS NULL);
```

## 5. Diagnose — strategy & frequency

**`check` vs `timestamp` strategy.** This snapshot uses **`check`**: dbt compares the listed
`check_cols` and writes a new version when any differ. Use it when the source has **no reliable
"last updated" column**. The alternative is **`timestamp`**: you point dbt at an `updated_at` column
and it trusts that — cheaper (no column-by-column compare) and it catches changes to *untracked*
columns too, but only as trustworthy as that timestamp. `check` is robust but only notices changes in
the columns you explicitly list (change a column **not** in `check_cols` and the snapshot won't
version it).

**Frequency vs missed intraday changes — the subtle trap.** A snapshot captures **only the states it
observes at run time**. If C001 went `gold → silver → platinum` between two daily snapshots, you'd
record `gold → platinum` and the `silver` interlude is **lost forever** — there is no row for it. SCD2
preserves history at your **snapshot cadence**, not continuously. If you need every transition, you
need CDC (the [`debezium/`](../../debezium/) track), not a periodic snapshot. Snapshot **as often as
your worst-case "states I cannot afford to miss" requires**, and no more (each run rescans the source).

## 6. Prove it

| Run | command | C001 versions | C001 current tier | total current rows |
|-----|---------|:-------------:|:-----------------:|:------------------:|
| 1 — baseline | `dbt snapshot -s customers_snapshot` | **1** | `gold` | 20 |
| 2 — promote  | `... --vars '{promote_c001: "yes"}'` (run + snapshot) | **2** | `platinum` | 20 |

Run 2 turning C001's single row into a closed `gold` + current `platinum` pair — while the current-row
count stays at 20 — is the proof: history was **added**, not overwritten, and "current" still resolves
to exactly one row per customer.

## 7. Takeaways & "in real production…"

- **Snapshots are append-only history.** Never overwrite a dimension you might need to look back
  through — snapshot it. Joining facts to `dbt_valid_from`/`dbt_valid_to` gives correct as-of-event
  attributes (point-in-time joins).
- **Pick the strategy deliberately:** `timestamp` when you trust an `updated_at`; `check` when you
  don't — but then **list every column whose change matters** in `check_cols`, or you'll miss it.
- **Cadence defines fidelity.** You capture only the states you snapshot at; intraday flips between
  runs vanish. For lossless change history use CDC instead.
- **`dbt_valid_to IS NULL` = current** is the single most useful predicate — build your "current
  dimension" view on it, and your "as-of" joins on the `[valid_from, valid_to)` window.
- Snapshots are **run separately** (`dbt snapshot`, not `dbt run`) and persist across `--full-refresh`
  of your models — they're durable history, deliberately insulated from model rebuilds.

## 8. Teardown

The snapshot table `snapshots.customers_snapshot` is durable by design — drop it manually
(`DROP TABLE snapshots.customers_snapshot`) to re-run from a clean baseline. `make clean` clears all
generated data under `.tmp/`.
