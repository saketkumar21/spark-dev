# DBT-6 — Testing strategy & layering

> **Break → Detect → Fix → Prove.** Deliberately bad rows enter the pipeline. The question
> isn't "do we have tests?" — it's **which kind of test, at which layer, at what severity**.
> This module wires up all three dbt test kinds across two layers and shows a build that
> surfaces a real problem (a WARN) without blocking CI, while hard business rules stay at
> ERROR — exactly the calibration that keeps a pipeline both safe and shippable.

- **Artifacts:**
  [`dbt/models/marts/_quality__models.yml`](../models/marts/_quality__models.yml) (tests),
  [`dbt/macros/test_non_negative.sql`](../macros/test_non_negative.sql) (custom generic test),
  [`dbt/tests/assert_orders_reconcile.sql`](../tests/assert_orders_reconcile.sql) (singular test),
  seed [`dbt/seeds/orders_quality_raw.csv`](../seeds/orders_quality_raw.csv) (the bad rows).
- **Extends:** the [`dbt/`](README.md) project. **Run via:**
  `cd dbt && source .env && dbt <cmd>` — Thrift JDBC → the unified Spark server, Delta tables.
- **Time:** ~5 min. **Laptop-safe:** 5 raw rows, all under `.tmp/`; `make clean` resets.

---

## 1. The scenario — bad data on purpose

`orders_quality_raw.csv` is seeded with rows engineered to trip one rule each:

| `order_id` | Problem | Which rule it breaks |
|-----------:|---------|----------------------|
| 2001 | `amount = -9.99` | non-negative amount |
| 2002 | `customer_id = C999` | no such customer (orphan FK) |
| 2003 | `status = 'shipped'` | not an accepted status value |
| 2000, 2004 | clean | — |

`stg_orders_quality` is a thin typed **view** over that seed (it still contains all 5 bad-and-
good rows). [DBT-7](README.md) then splits it into `orders_clean` (passes every rule) and
`orders_quarantine` (the 3 offenders, tagged with a reason). This module **tests** all of
those — and the layering is the whole point.

## 2. The three kinds of dbt test

dbt has exactly three test shapes, and a mature project uses all three:

- **Generic (built-in)** — reusable, parameterized assertions you attach to a column in YAML:
  `unique`, `not_null`, `accepted_values`, `relationships`. The bread and butter.
- **Singular** — a one-off `.sql` file in `tests/` that returns **failing rows**; if it
  returns any row, the test fails. For assertions that don't fit a column (cross-table
  invariants, reconciliations). Here, `assert_orders_reconcile`:

  ```sql
  -- every raw row is either clean or quarantined (none lost / duplicated)
  with raw as (select count(*) as n from {{ ref('stg_orders_quality') }}),
       parts as (select (select count(*) from {{ ref('orders_clean') }})
                      + (select count(*) from {{ ref('orders_quarantine') }}) as n)
  select 'reconcile_mismatch' as failure
  from raw join parts on true
  where raw.n != parts.n
  ```

- **Custom generic** — your *own* reusable test, defined as a `{% test %}` macro and then
  usable in YAML like any built-in. Here, `non_negative`:

  ```sql
  {% test non_negative(model, column_name) %}
  select * from {{ model }} where {{ column_name }} < 0   -- failing rows
  {% endtest %}
  ```

  Once defined, `data_tests: [non_negative]` on any numeric column just works.

## 3. The layering — structural at staging, business-logic at marts

Where a test lives matters as much as what it asserts:

- **Staging layer = structural truth.** Is the grain right? Keys unique and non-null, types
  correct, raw enum domain understood. On `stg_orders_quality`: `order_id` is `unique` +
  `not_null`, and an `accepted_values` on `status` — but at **`severity: warn`**, because
  staging *expects* raw mess and we want it flagged, not fatal.
- **Marts layer = business rules.** The validated `orders_clean` mart must satisfy the rules
  the business actually depends on, at full strictness: `non_negative` amount (custom generic),
  `accepted_values` on `status` at **default ERROR**, a `relationships` FK to `stg_customers`,
  and a `dbt_expectations.expect_column_values_to_be_between` range (0–100000) on amount.

The same `accepted_values` rule appears at **both** layers but at **different severities** —
that contrast is the lesson. Raw data warns; the clean mart must not contain a violation at
all.

## 4. severity: `warn` vs `error`

Every test has a severity:

- **`error`** (default) — a failure makes `dbt test` / `dbt build` exit non-zero. In CI this
  **blocks the pipeline**. Use it for invariants downstream consumers rely on.
- **`warn`** — a failure is reported (counted as a WARN, printed) but the command still
  **succeeds**. The issue is visible without halting delivery. Use it for known-noisy raw data,
  soft expectations, or a new rule you're rolling out before enforcing.

```yaml
- name: status
  data_tests:
    - accepted_values:
        values: ['completed', 'refunded', 'pending']
        config: { severity: warn }   # on stg_orders_quality — flags 'shipped', build continues
```

The art is calibration: too much `error` and the build is red over data you can't control; too
much `warn` and real regressions slip through. Structural keys → error; raw-domain drift →
warn; business contracts → error.

## 5. Run it — VERIFIED

```bash
dbt test -s stg_orders_quality orders_clean orders_quarantine assert_orders_reconcile
```

→ **10 PASS, 1 WARN, 0 ERROR.** Breakdown:

- The **1 WARN** is the `severity: warn` `accepted_values` on `stg_orders_quality.status`: it
  finds the `'shipped'` row (order 2003) and **warns** — the build does not fail. This is the
  headline: a real data problem surfaced, pipeline still green.
- Every **`orders_clean`** business test **PASSES** at ERROR severity — the custom `non_negative`,
  the `relationships` FK to `stg_customers`, the default-severity `accepted_values`, and the
  `dbt_expectations` range test. They pass because the [DBT-7](README.md) quarantine already removed
  the three bad rows (−9.99, C999, 'shipped') before they reached the clean mart. Quarantine at
  the model layer is *why* the strict tests can stay strict and still go green.
- The `not_null` on `orders_quarantine.quarantine_reason` passes (every quarantined row has a
  reason).
- The singular **`assert_orders_reconcile`** PASSES: `clean (2) + quarantine (3) == raw (5)` →
  no rows lost or duplicated by the split.

## 6. Diagnose

> **A test result is `pass` / `warn` / `error` (+ `skip`), and the kind × layer × severity
> matrix is your quality contract.** dbt compiles each test to a SELECT of failing rows; zero
> rows = pass, ≥1 row = warn-or-error per the configured severity.

- The WARN proves severity works: `'shipped'` violates `accepted_values`, but at `warn` the
  command still exits 0. Flip it to `error` and the *same* row turns the build red.
- The clean-mart ERROR tests passing proves **layering + the quarantine pattern**: bad rows
  were routed out upstream, so strict business assertions on the mart have nothing to fail on.
- The singular test proves an invariant no column-level test could: the clean/quarantine split
  is **conservative** (the counts reconcile to the raw input).

## 7. Fix it — the calibration that keeps a build both safe and shippable

- **Push structural tests down to staging** — unique/not-null on keys, types, enum domain. Fail
  fast where data enters, before marts compound the error.
- **Put business-logic tests at marts** — accepted values, ranges, FK relationships,
  dbt-expectations. These encode what consumers rely on; keep them at ERROR.
- **Use a custom generic** when the same business rule repeats across models (`non_negative`)
  instead of copy-pasting singular SQL.
- **Use a singular test** for cross-table / cross-grain invariants a column test can't express
  (reconciliation, "no row in A without a match in B by composite key").
- **Calibrate severity deliberately** — `warn` for raw-domain noise and not-yet-enforced rules,
  `error` for contracts. Pair `warn` with the [DBT-7](README.md) quarantine so warned rows are
  *routed*, not merely logged.

## 8. Prove it

| Test | Kind | Layer | Severity | Result |
|------|------|-------|----------|--------|
| `order_id` unique / not_null (stg) | generic | staging | error | PASS |
| `status` accepted_values (stg) | generic | staging | **warn** | **WARN** (finds `'shipped'`) |
| `order_id` unique / not_null (clean) | generic | marts | error | PASS |
| `amount` `non_negative` | **custom generic** | marts | error | PASS |
| `amount` range 0–100000 | generic (dbt-expectations) | marts | error | PASS |
| `status` accepted_values (clean) | generic | marts | error | PASS |
| `customer_id` relationships → stg_customers | generic | marts | error | PASS |
| `quarantine_reason` not_null | generic | marts | error | PASS |
| `assert_orders_reconcile` | **singular** | cross-table | error | PASS |

**Totals: 10 PASS, 1 WARN, 0 ERROR.** The one WARN is the deliberate raw-data violation
surfaced without blocking; everything strict passes because quarantine cleaned the marts first.

## 9. Takeaways & "in real production…"

- **Three test kinds, used together** — generic for the common column assertions, custom
  generic for *reusable* business rules, singular for invariants that span tables.
- **Layer by purpose** — structural at staging (grain/keys/types), business-logic at marts
  (values/ranges/relationships). A bug caught at staging is cheaper than one caught in a mart.
- **Severity is a CI policy, not a detail** — `warn` surfaces, `error` blocks. Reserve `error`
  for consumer contracts; let raw-domain noise `warn` and route it via the quarantine pattern.
- **Tests + quarantine compose** — [DBT-7](README.md) routes bad rows out so the marts' strict ERROR
  tests stay green; the staging WARN keeps the problem visible. That pairing is what lets a
  pipeline be both **safe** and **always shippable**. Statistical / drift checks beyond
  fixed-domain assertions are **DBT-8** (dbt-expectations + Great Expectations).

## 10. Teardown

These tests are read-only over Delta tables built by `dbt build`. Re-run `dbt build` to rebuild
the seed → staging → clean/quarantine chain; `make clean` removes everything under `.tmp/`.
