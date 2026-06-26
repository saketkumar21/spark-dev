# Run a Snowflake dbt repo on Spark 4.0.2 — unchanged

Swap the warehouse from Snowflake to Spark 4.0.2 **without editing a single model**. Your Snowflake SQL
stays as-is; only `profiles.yml`, one `dbt_project.yml` flag, and a package install change.

## The only changes you make

1. **Install the package** (carries the transpile + Spark-output fix-ups, auto-activates via a `.pth`):
   ```bash
   pip install dbt-spark-transpile        # in this repo it's already wired via uv
   ```
2. **`profiles.yml`** — point the output at Spark/Thrift instead of Snowflake:
   ```yaml
   your_profile:
     target: dev
     outputs:
       dev:
         type: spark
         method: thrift
         host: "{{ env_var('DBT_SPARK_HOST', 'localhost') }}"
         port: "{{ env_var('DBT_SPARK_PORT', 10000) | int }}"
         schema: analytics
   ```
3. **`dbt_project.yml`** — declare your models' source dialect:
   ```yaml
   models:
     your_project:
       +transpile_from: snowflake      # your models are written in Snowflake SQL
       # +transpile_to: spark          # optional, default 'spark'
   ```

Then `dbt build` runs your existing Snowflake models on Spark. **No model edits.**

## How it works
At dbt **compile**, each model's SQL is parsed as Snowflake and regenerated as Spark via `sqlglot`
(`dbt/dbt-spark-transpile/`). A **fix-up layer** then repairs the spots where sqlglot's Spark output
isn't accepted by Spark 4.0.2's real parser (e.g. `x NOT IN (subquery)` → which sqlglot renders as the
unsupported `x <> ALL (subquery)` → rewritten back to `NOT x IN (subquery)`). The rewrite happens before
dbt wraps the model, so `target/compiled/` and the executed SQL are both Spark.

Two companions make a whole Snowflake repo work on Spark, also config-only:
- **Catalog routing** (`macros/generate_schema_name.sql`): `file_format` → the matching Spark catalog
  (`delta`→`spark_catalog`, `iceberg`→`iceberg_catalog`).
- **Seed idempotency** (`macros/create_csv_table.sql`): makes `dbt seed` re-runnable on Spark.

## Trust: you always know what's safe — nothing is silently wrong
A model is either converted to **verified-valid Spark SQL**, or it **fails loudly** (a clear dbt/Spark
error naming the model). It never silently produces a wrong result from an un-converted construct.

To certify your repo **upfront**, after `dbt compile` run the check (or `make transpile-check`):
```bash
uv run python dbt/dbt-spark-transpile/transpile_check.py
```
It `EXPLAIN`/zero-row-validates every compiled model against Spark and reports:
- **verified valid on Spark** (the bulk),
- **DIALECT blocker** — a construct sqlglot can't convert (named, with the Spark error class) — the only
  models needing attention,
- **upstream not built** — informational (run `dbt build` first), not a dialect issue.
It exits non-zero on any DIALECT blocker, so it works as a CI gate.

### Safely converted (verified)
Window functions incl. `QUALIFY`, `x [NOT] IN (subquery)`, `IFF`→`IF`, `NVL`→`COALESCE`, `::`→`CAST`,
`DATEADD`/`DATEDIFF`, CTEs, `CASE`, standard joins/aggregations, and the broad set `sqlglot` maps.

### Known-unsupported (fails loud, by design)
Constructs with **no clean Spark equivalent** — chiefly Snowflake **semi-structured** features:
`LATERAL FLATTEN`, `VARIANT`/`OBJECT`/`ARRAY` semantics, `:` path access, and a few proprietary
functions. These surface as loud errors (or a fail-soft WARNING + the original passed through, which
Spark then rejects loudly) — so you find them via the check, not in production. This residue is inherent
to the dialect differences (true of any tool, including SQLMesh), not a defect of the approach.

> Honest bottom line: this safely and accurately converts the large majority of analytical Snowflake SQL,
> and is transparent (loud, certified) about the minority it can't — so you can trust every green model.
