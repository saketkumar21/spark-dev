# DBT-7 — The quarantine pattern

> **Break → Detect → Fix → Prove.** Bad rows are inevitable — a negative amount, an orphan foreign
> key, a status the schema never anticipated. The reflex is a hard dbt test that **fails the build**
> the moment one appears, which stops every downstream model and pages someone at 2am over five bad
> rows out of millions. The quarantine pattern flips that: **route the bad rows to a quarantine
> table and keep the pipeline moving.** The clean mart contains only rows that passed every rule, the
> bad rows are triaged out-of-band with a reason attached, and a **reconcile test** guarantees no row
> was silently lost or duplicated in the split.

This track expands the dbt project in [`dbt/`](../../dbt/). Run dbt with:

```bash
cd dbt && source .env        # sets DBT_PROFILES_DIR + Thrift connection vars
dbt <cmd>                     # Thrift JDBC → Spark; marts are Delta tables
```

- **Connection:** Thrift → the unified Spark server (`make up`), catalog `spark_catalog`
  (Delta / Hive managed tables). Spark UI at http://localhost:4040.
- **Artifacts:**
  - Seed [`seeds/orders_quality_raw.csv`](../../dbt/seeds/orders_quality_raw.csv) — 5 rows with
    deliberate defects.
  - [`models/staging/stg_orders_quality.sql`](../../dbt/models/staging/stg_orders_quality.sql) — typed view.
  - [`models/marts/orders_clean.sql`](../../dbt/models/marts/orders_clean.sql) — rows passing **all** rules.
  - [`models/marts/orders_quarantine.sql`](../../dbt/models/marts/orders_quarantine.sql) — rows
    **failing** a rule, tagged with `quarantine_reason`.
  - [`tests/assert_orders_reconcile.sql`](../../dbt/tests/assert_orders_reconcile.sql) — singular
    test: `clean + quarantine == raw`.
- **Laptop-safe:** 5 tiny rows, no infra beyond the running Spark server.

---

## 1. The scenario

Five raw orders land. Two are clean; three each break a different rule:

| `order_id` | `customer_id` | `amount` | `status`  | Defect |
|-----------:|:-------------:|---------:|:----------|:-------|
| 2000 | C001 | 50.00 | completed | — (good) |
| 2001 | C002 | **-9.99** | completed | **negative amount** |
| 2002 | **C999** | 30.00 | completed | **orphan customer** (no such row in `stg_customers`) |
| 2003 | C003 | 20.00 | **shipped** | **invalid status** (not in the accepted set) |
| 2004 | C004 | 75.00 | completed | — (good) |

The three business rules a clean order must satisfy:

1. `amount > 0`
2. `status in ('completed', 'refunded', 'pending')`
3. `customer_id` exists in `stg_customers` (referential integrity)

The naive design enforces all three as **ERROR-severity dbt tests on the raw model**. The first run
that sees rows 2001/2002/2003 fails the build — `orders_clean` never gets built, the marts that
depend on it go stale, and the on-call engineer is woken to discover the "outage" is three malformed
rows that should simply have been set aside.

## 2. Break it — the hard test that blocks the build

The brittle approach puts the rules where they **halt the run**:

```yaml
# the naive design — business rules as ERROR tests on the raw data
- name: stg_orders_quality
  columns:
    - name: amount
      data_tests: [non_negative]                 # fails on row 2001
    - name: status
      data_tests:
        - accepted_values: { values: ['completed','refunded','pending'] }   # fails on row 2003
    - name: customer_id
      data_tests:
        - relationships: { to: ref('stg_customers'), field: customer_id }   # fails on row 2002
```

Any single bad row turns `dbt build` red and **stops the DAG**. There is no clean output at all —
the good rows (2000, 2004) are held hostage by the bad ones. That is the failure mode: a quality
problem becomes a **pipeline-availability** problem.

## 3. Detect it — count the rows by reason

The detection query is a one-liner against the quarantine table — it tells you *how many* rows
failed and, crucially, *why*:

```sql
SELECT quarantine_reason, COUNT(*)
FROM <schema>.orders_quarantine        -- analytics.orders_quarantine
GROUP BY 1;
```

| `quarantine_reason` | count |
|:--------------------|------:|
| `non_positive_amount` | 1 |
| `orphan_customer`     | 1 |
| `invalid_status`      | 1 |

Each reason is a triage bucket. A spike in `orphan_customer` points at a broken upstream join; a
spike in `invalid_status` points at a new enum value the model hasn't been taught yet. The reason
column turns "some rows are bad" into an actionable signal.

## 4. Diagnose

> **A hard quality test conflates two different failures: "this row is bad" and "this pipeline must
> stop." They are not the same. Most bad rows should be set aside, not allowed to halt every model
> downstream of them.**

The quarantine pattern separates the two. Rules still run — but instead of *asserting* the data is
clean, the models **partition** it into a clean side and a quarantine side. The clean mart is, by
construction, the rows that passed; the quarantine table is the rows that didn't, each carrying the
specific rule it broke. The pipeline never stops on routine bad data; it stops only if the split
*itself* is wrong (see the reconcile test).

## 5. Fix it — split into clean + quarantine, then reconcile

**`orders_clean`** keeps only rows passing every rule:

```sql
{{ config(materialized='table', file_format='delta') }}
select s.*
from {{ ref('stg_orders_quality') }} s
where s.amount > 0
  and s.status in ('completed', 'refunded', 'pending')
  and s.customer_id in (select customer_id from {{ ref('stg_customers') }})
```

**`orders_quarantine`** is the exact complement — rows failing *any* rule — with a
`quarantine_reason` derived from which rule tripped first:

```sql
select s.*,
  case
    when s.amount <= 0 then 'non_positive_amount'
    when s.status not in ('completed','refunded','pending') then 'invalid_status'
    when s.customer_id not in (select customer_id from {{ ref('stg_customers') }}) then 'orphan_customer'
  end as quarantine_reason
from {{ ref('stg_orders_quality') }} s
where s.amount <= 0
   or s.status not in ('completed','refunded','pending')
   or s.customer_id not in (select customer_id from {{ ref('stg_customers') }})
```

The safety net is the **singular reconcile test** — the one test that *is* allowed to fail the
build, because a mismatch means a row was lost or double-counted by the split:

```sql
-- assert_orders_reconcile.sql — fails iff clean + quarantine != raw
with raw   as (select count(*) as n from {{ ref('stg_orders_quality') }}),
     parts as (select (select count(*) from {{ ref('orders_clean') }})
                    + (select count(*) from {{ ref('orders_quarantine') }}) as n)
select 'reconcile_mismatch' as failure
from raw join parts on true
where raw.n != parts.n
```

> **Real-world variant:** in production you often skip the explicit two-model split and use a
> **post-hook** on the clean model that `INSERT`s the failing rows into a standing quarantine table.
> Here we use an explicit clean/quarantine pair because the data flow is easier to *see* and to test.

### Verified commands

```bash
cd dbt && source .env
dbt seed -s orders_quality_raw
dbt run  -s stg_orders_quality orders_clean orders_quarantine
dbt test -s assert_orders_reconcile
```

Result: **`orders_clean` = 2 rows** (2000, 2004); **`orders_quarantine` = 3 rows** (2001
`non_positive_amount`, 2002 `orphan_customer`, 2003 `invalid_status`); the **reconcile test PASSES**
(2 + 3 == 5 raw). The build is green and the clean mart is trustworthy — *with* bad data present.

## 6. Prove it

Trace every raw row to exactly one destination:

| `order_id` | Destination | `quarantine_reason` |
|-----------:|:------------|:--------------------|
| 2000 | `orders_clean` | — |
| 2001 | `orders_quarantine` | `non_positive_amount` |
| 2002 | `orders_quarantine` | `orphan_customer` |
| 2003 | `orders_quarantine` | `invalid_status` |
| 2004 | `orders_clean` | — |

Every row lands in exactly one table; **2 + 3 = 5** with none lost or duplicated. The reconcile test
passing is the machine-checked proof of that conservation. The clean mart is provably clean, and the
three bad rows are documented and queryable rather than blocking the run.

## 7. Takeaways & "in real production…"

- **Quarantine routine bad data; reserve hard failures for structural breakage.** A negative amount
  is a row to set aside, not a reason to take the warehouse down. Save ERROR-severity tests for
  invariants that mean the pipeline itself is broken (here: the reconcile test).
- **Always reconcile the split.** The one test that *must* fail is the one proving `clean +
  quarantine == source`. Without it, a subtle bug in either `WHERE` clause silently drops or
  double-counts rows — the exact failure quarantine is supposed to prevent.
- **Tag the reason.** A bare quarantine table is a junk drawer; `quarantine_reason` makes it a triage
  queue and lets you alert on *trends* (a sudden spike in `orphan_customer` is a real incident).
- **Mind layering.** Structural tests (`unique`, `not_null`) still belong at staging and *should*
  fail loudly. It's the **business-logic** rules that move into the clean/quarantine split — see
  [`_quality__models.yml`](../../dbt/models/marts/_quality__models.yml), where the raw model's
  `accepted_values` is `severity: warn` (flags, doesn't block) while the clean model's is ERROR
  (passes, because the quarantine guaranteed it).

## 8. Teardown

These models live in the shared [`dbt/`](../../dbt/) project; there's nothing module-specific to
tear down. `dbt build` rebuilds them from the seeds, and `make clean` clears all generated data
under `.tmp/`.
