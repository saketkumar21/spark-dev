# DBT-8 — dbt-expectations & Great Expectations

> **Break → Detect → Fix → Prove.** `unique` / `not_null` / `relationships` cover *structural*
> correctness, but they say nothing about whether a column's **values make sense** — is the amount in
> a plausible range, is the mean where you expect, does the id match a format? Two tools cover that
> gap from opposite ends. **dbt-expectations** expresses statistical/range/distribution assertions as
> dbt tests that run **inside `dbt build` / CI**, versioned alongside the models. **Great
> Expectations (GE)** runs profiling, distribution and validation **decoupled from the dbt run** —
> against any source, on its own schedule, producing data docs. This module builds one of each and,
> more importantly, teaches **when to reach for which.**

This track expands the dbt project in [`dbt/`](README.md). Run dbt with:

```bash
cd dbt && source .env        # sets DBT_PROFILES_DIR + Thrift connection vars
dbt <cmd>                     # Thrift JDBC → Spark; marts are Delta tables
```

The standalone GE script runs **from the repo root** (not from `dbt/`).

- **Connection:** Thrift → the unified Spark server (`make up`), catalog `spark_catalog`
  (Delta / Hive managed tables). Spark UI at http://localhost:4040.
- **Artifacts:**
  - [`dbt/packages.yml`](../packages.yml) — `metaplane/dbt_expectations` `>=0.10.0, <0.11.0`.
  - [`dbt/models/marts/_quality__models.yml`](../models/marts/_quality__models.yml) — an
    `expect_column_values_to_be_between` test on `orders_clean.amount` (min 0, max 100000).
  - [`dbt/quality/great_expectations/validate_table.py`](great_expectations/validate_table.py) —
    standalone GE validation of a Spark/Delta/Iceberg table over Spark Connect.
- **Laptop-safe:** validates `orders_clean` (the 2-row clean mart from [DBT-7](dbt7_quarantine.md));
  no volume, no extra infra.

---

## 1. The scenario

`orders_clean` already passes its structural tests — `order_id` is unique and not-null, every
`customer_id` resolves. But "structurally valid" is not "sensible." Suppose a unit bug starts writing
`amount` in cents (so `5000` instead of `50.00`), or a feed flips a sign, or a new currency pushes
values into the millions. None of that trips `unique`/`not_null`/`relationships` — the rows are
perfectly well-formed; they're just **wrong in value**. You need *value-level* assertions, and you
need to decide where they live: **in the build** (fail CI, block bad data) or **alongside the build**
(profile, monitor, document, alert).

## 2. Break it — structural tests are blind to value drift

A range bug sails straight through the structural suite:

```sql
-- orders_clean still passes unique/not_null/relationships with this row:
--   order_id = 2099 (unique ✓), customer_id = C001 (resolves ✓), amount = 5000000.00
-- ...yet 5,000,000 is almost certainly a units or currency bug.
```

The gap is **distributional**: no structural test knows that `amount` should sit in `[0, 100000]`,
that its mean should be in a sane band, or that `status` should match a fixed set. Without a
value-level check, the bad row flows into every downstream aggregate and quietly skews it.

## 3. Detect it — two complementary tools

### dbt-expectations — value tests inside the build

`metaplane/dbt_expectations` ships a large library of statistical/distribution tests usable exactly
like any other dbt test. On `orders_clean.amount`:

```yaml
# dbt/models/marts/_quality__models.yml
- name: orders_clean
  columns:
    - name: amount
      data_tests:
        - dbt_expectations.expect_column_values_to_be_between:
            min_value: 0
            max_value: 100000
```

Other tests from the same family you'd reach for: `expect_column_mean_to_be_between`,
`expect_column_values_to_match_regex`, `expect_column_value_lengths_to_be_between`,
`expect_column_values_to_be_in_set`. They run inside `dbt build`, are versioned with the model, and
fail CI when a value drifts out of band — the bad `5000000` row above would turn the build red.

### Great Expectations — validation decoupled from the run

[`validate_table.py`](great_expectations/validate_table.py) reads a table and validates it with an
**ephemeral** GE context and an `ExpectationSuite` — no dbt run, no persistent GE project required:

```python
TABLE = sys.argv[1] if len(sys.argv) > 1 else "spark_catalog.marts.orders_clean"
pdf = spark.table(TABLE).toPandas()                 # Connect-safe for small teaching tables

context = gx.get_context()                            # ephemeral
batch = (context.data_sources.add_pandas("spark_extract")
         .add_dataframe_asset(name="asset")
         .add_batch_definition_whole_dataframe("batch")
         .get_batch(batch_parameters={"dataframe": pdf}))

suite = gx.ExpectationSuite(name="orders_quality")
for exp in [
    gx.expectations.ExpectColumnValuesToNotBeNull(column="order_id"),
    gx.expectations.ExpectColumnValuesToBeUnique(column="order_id"),
    gx.expectations.ExpectColumnValuesToBeBetween(column="amount", min_value=0, max_value=100000),
    gx.expectations.ExpectColumnValuesToBeInSet(column="status",
                                                value_set=["completed", "refunded", "pending"]),
]:
    suite.add_expectation(exp)

result = batch.validate(suite)
sys.exit(0 if result.success else 1)                 # non-zero on failure → CI-friendly
```

> **Honesty note — GE over Spark Connect.** GE's **Spark execution engine does not work over Spark
> Connect** (it reaches for a classic `SparkSession`/RDD internals that Connect doesn't expose). The
> Connect-safe pattern this script uses is to **read the table to pandas with `toPandas()`** and
> validate the pandas frame. That is fine for the small teaching tables here; for a production-sized
> table you would instead point GE at the data via a native Spark session, a warehouse connection, or
> file-based assets — *not* `toPandas()` on a billion rows. The script also prints a benign gRPC
> "FD from fork parent" warning on some platforms — harmless, ignore it.

## 4. Diagnose

> **dbt-expectations and Great Expectations are not competitors — they sit at different points in the
> lifecycle. dbt-expectations is for assertions you want **in the build, versioned with the model, and
> enforced in CI**. Great Expectations is for profiling, distribution/drift analysis, data docs and
> validation **decoupled from the transform** — run against any source on its own cadence.**

| Question | Reach for |
|:---------|:----------|
| "Block this in CI, keep it next to the model" | **dbt-expectations** (a dbt test) |
| "Profile / monitor / document / drift, independent of dbt" | **Great Expectations** (checkpoints, data docs) |
| "Validate a source dbt doesn't even own" | **Great Expectations** |
| "Range/regex/distribution as part of `dbt build`" | **dbt-expectations** |

## 5. Fix it — put each check where it belongs

- **In the build:** keep value-level invariants you want enforced on every run as **dbt-expectations**
  tests in the model's YAML. They run with `dbt build`, fail CI on drift, and travel with the model in
  version control. `expect_column_values_to_be_between` on `amount` is the worked example.
- **Alongside the build:** run **Great Expectations** for profiling, drift detection, data docs, and
  validating sources outside the dbt graph — on its own schedule (a checkpoint in CI, an Airflow task,
  an ad-hoc audit). The standalone script is the worked example, and its non-zero exit on failure
  makes it drop straight into a CI gate.

### Verified commands

**dbt-expectations (in the build):**

```bash
cd dbt && source .env
dbt test -s orders_clean
```

The `dbt_expectations.expect_column_values_to_be_between` test on `amount` **PASSES** (the clean mart
holds 50.00 and 75.00 — both inside `[0, 100000]`).

**Standalone GE (decoupled), from the repo root:**

```bash
PYTHONPATH=$(pwd) uv run python dbt/quality/great_expectations/validate_table.py
# optional: pass a fully-qualified table name, e.g.
#   ... validate_table.py iceberg_catalog.default.lak2_events
```

Defaults to `spark_catalog.marts.orders_clean`: reads **2 rows**, runs **4 expectations**
(`order_id` not-null, `order_id` unique, `amount` between 0..100000, `status` in set) → **all 4 PASS,
`success=True`**, exit 0.

## 6. Prove it

| Tool | Where it runs | Example check | Result |
|:-----|:--------------|:--------------|:-------|
| **dbt-expectations** | inside `dbt build` / CI, versioned with the model | `expect_column_values_to_be_between(amount, 0, 100000)` | **PASS** (`dbt test -s orders_clean`) |
| **Great Expectations** | standalone, decoupled from dbt (any source, any schedule) | suite of 4: not-null + unique `order_id`, `amount` 0..100000, `status` in set | **4/4 PASS, `success=True`** (exit 0) |

The dbt test passing proves the in-build, versioned path works; the GE script reporting `success=True`
across all four expectations (and exiting 0) proves the decoupled, CI-friendly path works against a
live Delta table read over Spark Connect.

## 7. Takeaways & "in real production…"

- **Structural tests are necessary but not sufficient.** `unique`/`not_null`/`relationships` never
  catch value drift — a perfectly well-formed row can still be wrong. Add **value-level** assertions.
- **Put the assertion where it has to act.** Want it enforced on every build and reviewed in PRs →
  **dbt-expectations** (it's just a dbt test). Want profiling, drift, data docs, or validation of a
  source dbt doesn't own → **Great Expectations**, on its own cadence.
- **GE does not run over Spark Connect's Spark engine.** The Connect-safe move for *small* tables is
  `toPandas()` then validate the frame; for large tables use a native Spark session, a warehouse
  connection, or file-based assets instead — never `toPandas()` a huge table into the driver.
- **Make the standalone check CI-friendly.** The script exits non-zero on failure, so it slots into a
  CI gate or an Airflow task as cleanly as a dbt test does — the difference is *when* and *against
  what* it runs, not whether it can gate a deploy.
- Closes the Phase-5 data-quality arc: [DBT-7](dbt7_quarantine.md) keeps bad rows from blocking the
  pipeline; DBT-8 keeps *sensible-but-wrong* values from slipping through unseen.

## 8. Teardown

Nothing module-specific to tear down: the dbt tests run against the shared [`dbt/`](README.md)
models, and the GE script builds only an **ephemeral** context (no files written). `dbt build`
rebuilds the models from the seeds; `make clean` clears all generated data under `.tmp/`.
