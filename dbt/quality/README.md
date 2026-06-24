# `dbt/quality/` — dbt advanced & data quality (Phase 5) ✅ complete

The data-modeling and data-quality track. It lives **inside the dbt project** ([`dbt/`](../)) it
teaches, and **expands** that project well
beyond the two demo models and teaches **both** quality approaches and where each fits:

- **dbt native tests** — structural / in-pipeline assertions (unique, not-null, relationships,
  accepted-values), layered **staging (structural)** vs **marts (business-logic)**, plus singular
  and custom-generic tests, `severity: warn`, and the **quarantine** pattern.
- **dbt-expectations** — statistical / range / distribution tests that run inside `dbt build`/CI.
- **Great Expectations** — standalone profiling / distribution / validation, **decoupled** from the
  dbt run, against the Spark/Delta/Iceberg tables (Connect-safe via `toPandas`).

Each module follows **Break → Detect → Fix → Prove** (see
[`docs/CURRICULUM_BRIEF.md`](../../docs/CURRICULUM_BRIEF.md)). The dbt artifacts live in [`dbt/`](../)
(models / snapshots / macros / tests / seeds); the standalone Great Expectations lab lives in
[`great_expectations/`](great_expectations/).

> **Run the dbt side:** `cd dbt && source .env && dbt deps && dbt build`. Connection is Thrift→Spark
> (`make up` must be running). Models materialize in `spark_catalog` (Delta/Hive) — the modules use
> `file_format='delta'` where MERGE/incremental is needed (the Thrift+Iceberg classloader gotcha
> means notebooks, not dbt, write Iceberg directly).
>
> **Run Great Expectations:** from the repo root, `PYTHONPATH=$(pwd) uv run python dbt/quality/great_expectations/validate_table.py`.
>
> **Laptop-safe:** tiny seeds, all state in the Spark warehouse under `.tmp/`; `make clean` recovers.
> No new Docker services — Phase 5 runs on the base `make up` stack.

## Modules

`[ ]` not started · `[~]` in progress · `[x]` built & live-verified (`dbt build` / GE run)

| ID | Module | Where | Status |
|----|--------|-------|--------|
| `DBT-1` | [Materializations & cost](dbt1_materializations.md) — view / table / ephemeral / incremental tradeoffs | `dbt/` + writeup | `[x]` |
| `DBT-2` | [Incremental strategies](dbt2_incremental.md) — `merge` / `insert_overwrite` / `append`, `unique_key` | `dbt/models/marts/fct_orders.sql` | `[x]` |
| `DBT-3` | [Late-arriving data & lookback](dbt3_late_arriving.md) — event-time watermark drops late rows; a lookback recaptures them | `fct_orders` + `orders` seed | `[x]` |
| `DBT-4` | [Snapshots / SCD Type 2](dbt4_snapshots_scd2.md) — `dbt_valid_from/to`; check strategy | `dbt/snapshots/` | `[x]` |
| `DBT-5` | [Schema-change handling](dbt5_schema_change.md) — `on_schema_change`; add a column across runs | `fct_orders_evolving` | `[x]` |
| `DBT-6` | [Testing strategy & layering](dbt6_testing_strategy.md) — generic / singular / custom; `severity: warn` | `_quality__models.yml` + `macros/` + `tests/` | `[x]` |
| `DBT-7` | [Quarantine pattern](dbt7_quarantine.md) — route bad rows out instead of failing the build | `orders_clean` / `orders_quarantine` | `[x]` |
| `DBT-8` | [dbt-expectations + Great Expectations](dbt8_expectations_ge.md) — when to use which | `great_expectations/` + dbt tests | `[x]` |
| `DBT-9` | [Sources, freshness, contracts, exposures](dbt9_sources_contracts.md) — freshness SLAs, enforced contracts, lineage | `_sources.yml` / `dim_orders_contract` / `_exposures.yml` | `[x]` |
| `DBT-10` | [Macros, state & slim CI](dbt10_macros_slim_ci.md) — surrogate-key macro; `state:modified+` | `macros/` + `dim_orders_keyed` | `[x]` |

## Layout

```
dbt/quality/
├── README.md                      # this file (Phase 5 track index)
├── great_expectations/
│   └── validate_table.py          # DBT-8 standalone GE (Connect-safe via toPandas)
└── dbt1_materializations.md … dbt10_macros_slim_ci.md   # the 10 Break→Detect→Fix→Prove writeups
```

The dbt artifacts these modules add to the project ([`dbt/`](../)): seeds (`orders.csv`,
`orders_quality_raw.csv`), staging (`stg_orders`, `stg_orders_quality`, `snap_customers_src`),
marts (`fct_orders`, `fct_orders_evolving`, `orders_clean`, `orders_quarantine`,
`dim_orders_contract`, `dim_orders_keyed`, `high_value_orders`), `snapshots/customers_snapshot.sql`,
`macros/` (`surrogate_key`, `test_non_negative`), `tests/assert_orders_reconcile.sql`,
`_sources.yml`, `_exposures.yml`, `packages.yml` (metaplane/dbt_expectations).

## Suggested order

`DBT-1` (materializations) → `DBT-2` (incremental) → `DBT-3` (late data) → `DBT-4` (SCD2) →
`DBT-5` (schema change) → `DBT-6` (testing) → `DBT-7` (quarantine) → `DBT-8` (expectations + GE) →
`DBT-9` (sources/contracts) → `DBT-10` (macros/slim-CI). The first five are modeling-and-cost; the
last five are testing-and-quality. The whole expanded project is verified by one
`dbt build` (PASS=50, WARN=1 intentional, ERROR=0).

## How it connects to the rest of the curriculum

- **Lakehouse (Phase 2):** `merge` incrementals rewrite a partition (LAK-8); schema evolution mirrors
  LAK-6; the small-files cost of frequent incremental writes ties to LAK-2.
- **CDC (Phase 4):** the same dbt tests/quarantine/contracts guard the Iceberg tables fed by the
  Debezium→Spark `MERGE` pipeline (CDC-7).
