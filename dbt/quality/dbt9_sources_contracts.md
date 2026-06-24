# DBT-9 — Sources, freshness, contracts & exposures

> **Break → Detect → Fix → Prove.** A dbt project doesn't end at its own models — it has an
> **upstream** (the raw feeds it reads) and a **downstream** (the dashboards and ML jobs that read
> *it*). Three under-used dbt features make both edges first-class: **source freshness** alerts you
> when an upstream feed stalls, **model contracts** fail the build the moment a model's output schema
> drifts, and **exposures** put downstream consumers on the lineage graph so a breaking change is
> visible *before* you ship it. This module wires up one of each and breaks the freshness SLA on
> purpose to show the alert fire.

This track expands the dbt project in [`dbt/`](README.md). Run dbt with:

```bash
cd dbt && source .env && dbt <cmd>
```

- **Connection:** Thrift → the unified Spark server (`make up`), catalog `spark_catalog`
  (Delta / Hive managed tables). Spark UI at http://localhost:4040 (not central here — this module
  is about metadata and CI gates, not Stages).
- **Artifacts:** [`models/_sources.yml`](../models/_sources.yml) (a source with a freshness
  SLA), [`models/marts/dim_orders_contract.sql`](../models/marts/dim_orders_contract.sql) +
  [`models/marts/_contract__models.yml`](../models/marts/_contract__models.yml) (an enforced
  contract), [`models/_exposures.yml`](../models/_exposures.yml) (a dashboard exposure).
- **Laptop-safe:** the `orders` seed is ~15 rows; nothing here generates volume.

---

## 1. The three features

| Feature | Declares | dbt checks it with | What it protects |
|---------|----------|--------------------|------------------|
| **Source freshness** | a raw table + a `loaded_at_field` + warn/error age thresholds | `dbt source freshness` | the **upstream** edge — catches a stalled feed before stale data flows downstream |
| **Model contract** | the model's **output schema** (column name + `data_type`) | `dbt build` / `dbt run` (at build time) | the model itself — a breaking schema change **fails fast** instead of silently shipping |
| **Exposure** | a downstream consumer (dashboard, ML, app) + what it `depends_on` | `dbt docs` / `dbt build -s +exposure:…` | the **downstream** edge — puts consumers on the lineage DAG so impact is visible |

## 2. Source freshness — the upstream early-warning

[`_sources.yml`](../models/_sources.yml) declares the seeded `orders` as a **source**, the
column dbt should read the landing time from, and how old that landing time may get:

```yaml
sources:
  - name: raw_landing
    schema: seeds
    tables:
      - name: orders
        loaded_at_field: loaded_at
        freshness:
          warn_after:  {count: 12, period: hour}
          error_after: {count: 24, period: hour}
```

`dbt source freshness` runs `select max(loaded_at) from seeds.orders`, computes **how long ago**
that was, and compares the age to the thresholds: under 12 h → **pass**, 12–24 h → **warn**, over
24 h → **error**. That's a **service-level agreement on data arrival** — "this feed must have
produced a row in the last day, or page me." It is the single cheapest way to learn an upstream
producer died *before* your users notice the numbers stopped moving.

## 3. Break it — a stalled feed trips the SLA

The seed is a **fixed CSV**: its newest `loaded_at` is **2024-03-04**, and it never advances. So as
wall-clock time marches on, the source gets older and older relative to *now* — exactly what a feed
that **stopped producing** looks like. Run it:

```bash
cd dbt && source .env
dbt source freshness
```

**Verified result: `ERROR STALE` for `raw_landing.orders`.** `max(loaded_at)` is 2024-03-04, which
is far more than 24 h ago, so the age blows straight past `error_after` and dbt reports the source
as stale (non-zero exit — exactly what makes a CI/scheduler step go red). This is the demo: a
freshness breach you would alert on. In production the same check passes every day on a live feed
and only errors when the producer actually stalls.

## 4. Model contracts — fail fast on schema drift

[`dim_orders_contract.sql`](../models/marts/dim_orders_contract.sql) turns the contract on in
config and the companion
[`_contract__models.yml`](../models/marts/_contract__models.yml) **declares the promised
output schema**:

```sql
{{ config(materialized='table', file_format='delta', contract={'enforced': true}) }}
select
    cast(order_id as bigint)  as order_id,
    cast(amount   as double)  as amount,
    cast(status   as string)  as status
from {{ ref('fct_orders') }}
```

```yaml
models:
  - name: dim_orders_contract
    config: {contract: {enforced: true}}
    columns:
      - {name: order_id, data_type: bigint}
      - {name: amount,   data_type: double}
      - {name: status,   data_type: string}
```

With `contract: {enforced: true}`, dbt compares the model's **actual** projected columns against the
**declared** ones at build time. Build it:

```bash
cd dbt && source .env
dbt run -s dim_orders_contract
```

**Verified: builds OK** — the three `cast(...) as ...` columns match the declared names and types
exactly. The value is in the failure mode: rename `status` to `order_status`, drop a column, or
change a `cast` from `double` to `int`, and the build fails with

> *This model has an enforced contract that failed.*

A breaking change to a model's public schema is caught **in CI on the PR**, not in a downstream job
the next morning. The contract is the API boundary of the model.

### Honest caveat — contracts on Spark 4 + Delta + Thrift

dbt contracts can also declare **column-level constraints** (`not_null`, `primary_key`, `check`, …).
On this setup, **name + data-type enforcement works** (verified above), but the constraint DDL dbt
emits for `not_null`/`check` is **rejected by Spark** through the Thrift path — Spark's support for
enforced column constraints is limited and engine-dependent. So this module **enforces names and
types** (which is the bulk of contract value) and **describes constraints** as a feature with
limited Spark support rather than demonstrating them green. On a warehouse like Snowflake/BigQuery
the constraints enforce too; that's a platform difference, not a dbt one.

## 5. Exposures — put the downstream on the map

[`_exposures.yml`](../models/_exposures.yml) declares an `orders_dashboard` that depends on
two marts:

```yaml
exposures:
  - name: orders_dashboard
    type: dashboard
    depends_on:
      - ref('fct_orders')
      - ref('dim_customers')
    owner: {name: Data Team, email: data@example.com}
```

An exposure is **metadata, not a model** — dbt builds nothing for it. What it buys you:

- It appears as a **leaf node in the lineage DAG** in `dbt docs`, so the dashboard's dependency on
  `fct_orders` / `dim_customers` is visible to anyone reading the graph.
- It makes **impact analysis** a selector: `dbt build -s +exposure:orders_dashboard` builds
  exactly the models that feed that dashboard — the answer to "what do I need to run to refresh
  this report?" and "what breaks if I change `fct_orders`?"
- It records an **owner**, so a freshness/contract failure upstream has a name attached.

Generate the docs to see it on the graph:

```bash
cd dbt && source .env
dbt docs generate     # exposure now shows as a downstream node in the lineage
```

## 6. Prove it

| Feature | Command | Signal |
|---------|---------|--------|
| **Source freshness** | `dbt source freshness` | **`ERROR STALE`** for `raw_landing.orders` (max `loaded_at` 2024-03-04 ≫ 24 h `error_after`) |
| **Contract holds** | `dbt run -s dim_orders_contract` | **builds OK** — projected columns match declared name + type |
| **Contract drift** | (rename/retype/drop a column, re-run) | **build fails** — *"This model has an enforced contract that failed."* |
| **Exposure** | `dbt docs generate` | `orders_dashboard` appears as a **downstream leaf** in the lineage DAG |

## 7. Takeaways & "in real production…"

- **Freshness is your upstream smoke alarm.** Schedule `dbt source freshness` and page on
  `error`/`warn`; it catches a dead producer hours before the dashboards visibly flatline. Size
  `warn_after`/`error_after` to each feed's real cadence (hourly feed → tight; daily batch → loose).
- **Contracts make a model's schema a CI gate.** Enforce them on the models other teams/dashboards
  consume so a breaking rename/retype fails on the PR, not in production. Names + types are the
  portable core; treat column constraints as best-effort on engines (like Spark) with partial
  support.
- **Exposures close the lineage loop.** Without them the DAG stops at your marts and you're blind to
  who consumes them; with them, impact analysis (`+exposure:…`) and ownership are one selector away.
- Together these three guard the **edges** of the project — upstream arrival, the model's own
  contract, and downstream blast radius — which is exactly where unowned breakage hides.

## 8. Teardown

These artifacts live in the shared [`dbt/`](README.md) project; `dim_orders_contract` is the only
table built and `dbt build`/`--full-refresh` recreates it from the seeds. Sources and exposures
create nothing. `make clean` clears all generated data under `.tmp/`.
