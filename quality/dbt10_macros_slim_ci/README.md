# DBT-10 (Deep) — Macros, state & slim CI

> **Deep dive — patterns, not Break → Fix.** The earlier modules build models; this one is about the
> two engineering features that keep a *growing* dbt project maintainable and its CI fast: **Jinja
> macros** (DRY, deterministic SQL you write once and reuse) and **state comparison** (the
> `manifest.json` diff that powers **slim CI** and **deferral** — rebuild only what changed instead
> of the whole warehouse on every PR). There's no pathology to break here; each step is a short demo
> and the command output *is* the artifact.

This track expands the dbt project in [`dbt/`](../../dbt/). Run dbt with:

```bash
cd dbt && source .env && dbt <cmd>
```

- **Connection:** Thrift → the unified Spark server (`make up`), catalog `spark_catalog`
  (Delta / Hive managed tables). Spark UI at http://localhost:4040 (not central — this module is
  about Jinja and CI selectors, not Stages).
- **Artifacts:** [`macros/surrogate_key.sql`](../../dbt/macros/surrogate_key.sql) (a hand-rolled
  surrogate-key macro), [`models/marts/dim_orders_keyed.sql`](../../dbt/models/marts/dim_orders_keyed.sql)
  (uses it), [`macros/test_non_negative.sql`](../../dbt/macros/test_non_negative.sql) (a
  macro-as-test).
- **Laptop-safe:** the `orders` seed is ~15 rows; the only files written are `dim_orders_keyed` and a
  copy of `manifest.json` in `/tmp`.

---

## 1. Macros — write the SQL once

A dbt macro is a Jinja function that **emits SQL**. The point is DRY: a transformation used in ten
models lives in one macro, and fixing it fixes all ten.
[`surrogate_key.sql`](../../dbt/macros/surrogate_key.sql) builds a stable hash key from a list of
business columns — no extra package needed:

```jinja
{% macro surrogate_key(cols) -%}
md5(concat_ws('|'{% for c in cols %}, coalesce(cast({{ c }} as string), '_null_'){% endfor %}))
{%- endmacro %}
```

Two properties make this a *good* macro, and they're the general lesson:

- **Deterministic** — same inputs always produce the same hash. `md5` is pure; given the same column
  values you get the same key on every run, in every environment. That's what lets a surrogate key be
  a stable join target across rebuilds.
- **Idempotent / null-safe** — every column is `cast(... as string)` and wrapped in
  `coalesce(..., '_null_')`, and the parts are joined with an explicit `'|'` delimiter. So a `NULL`
  hashes distinctly from the literal string `'_null_'`, and `('a', NULL)` can't collide with
  `('a|', '')`. Hand-rolling the null handling is exactly what packaged helpers like
  `dbt_utils.generate_surrogate_key` do for you; writing it once here shows what's underneath.

[`dim_orders_keyed.sql`](../../dbt/models/marts/dim_orders_keyed.sql) just calls it:

```sql
{{ config(materialized='view') }}
select
    {{ surrogate_key(['order_id', 'customer_id']) }} as order_key,
    order_id, customer_id, amount, status
from {{ ref('fct_orders') }}
```

```bash
cd dbt && source .env
dbt run -s dim_orders_keyed
```

**Verified: builds**, and `order_key` is a deterministic `md5` of `(order_id, customer_id)` — re-run
it and the same order gets the same key every time. Inspect the compiled SQL under
`target/compiled/spark_dev/models/marts/dim_orders_keyed.sql` to see the macro expanded into the
literal `md5(concat_ws('|', coalesce(cast(order_id as string), '_null_'), ...))`.

### Macro-as-test

The same mechanism powers **custom generic tests**. [`test_non_negative.sql`](../../dbt/macros/test_non_negative.sql)
is a macro that returns the **failing rows** (a generic test passes when its query returns zero):

```jinja
{% test non_negative(model, column_name) %}
select * from {{ model }} where {{ column_name }} < 0
{% endtest %}
```

Reference it like any built-in test (`data_tests: [non_negative]` on a numeric column) and dbt runs
it as part of `dbt test`/`dbt build`. A test is just a macro that selects the rows that shouldn't
exist — which is why "write a macro" and "write a test" are the same skill.

## 2. State & slim CI — rebuild only what changed

dbt writes a **`target/manifest.json`** on every invocation: a full graph of your project — every
model, its compiled SQL, its dependencies, and a checksum. **State comparison** diffs the *current*
project against a **saved** manifest and exposes the difference as a node selector. That diff is the
entire basis of **slim CI**: on a PR, you don't rebuild the warehouse — you rebuild only the models
the PR *changed*, plus everything downstream of them.

The selector is **`state:modified+`**, read as two parts:

- **`state:modified`** — nodes whose definition differs from the saved manifest (SQL changed, config
  changed, etc.).
- **`+`** (trailing) — **and all their descendants**. You must rebuild downstream models because a
  change to `fct_orders` can change everything that reads it.

`--state <dir>` points dbt at the saved manifest to compare against.

### Verified slim-CI walkthrough

```bash
cd dbt && source .env

# 1. Establish a baseline ("production") manifest.
dbt compile                              # writes target/manifest.json
mkdir -p /tmp/dbt_state
cp target/manifest.json /tmp/dbt_state/  # the saved "prod" state to diff against

# 2. Modify ONE model (e.g. edit marts/high_value_orders.sql).

# 3. Ask dbt what changed — and what depends on it.
dbt ls --select 'state:modified+' --state /tmp/dbt_state --resource-type model
```

**Verified:** the command lists **only the modified model and its downstream** — e.g.
`spark_dev.marts.high_value_orders` — **not** the dozen untouched models in the project. Swap `ls`
for `build` and that is the exact command a slim-CI job runs on every pull request:

```bash
dbt build --select 'state:modified+' --state /tmp/dbt_state
```

On this 15-row project the savings are theoretical; on a 500-model warehouse, `state:modified+` is
the difference between a 3-minute PR check and a 90-minute one.

### Deferral — run a changed model against prod's upstreams

The companion feature is **`--defer`** (with `--state`). When you're iterating on one model, you
don't want to first rebuild all of *its* upstreams in your dev schema. `--defer` tells dbt: for any
`ref()` to a model you **haven't** built yourself, **resolve it to the version in the saved
(prod) state** instead. So you build only your changed model and it reads everything else straight
from production:

```bash
dbt run --select dim_orders_keyed --defer --state /tmp/dbt_state
```

`dim_orders_keyed`'s `ref('fct_orders')` resolves to **prod's** `fct_orders` — you never had to
build it locally. Slim CI and deferral are the same `--state` manifest used two ways: **slim CI**
asks *"what changed?"*, **deferral** asks *"where do unbuilt upstreams come from?"*

## 3. Prove it

| Technique | Command | Outcome |
|-----------|---------|---------|
| **Reusable macro** | `dbt run -s dim_orders_keyed` | builds; `order_key` = deterministic `md5(concat_ws('|', …))` of `(order_id, customer_id)` — same inputs → same key every run |
| **Macro-as-test** | reference `non_negative` in `data_tests:`, then `dbt test` | passes when no rows are negative; the macro *is* the failing-row query |
| **Slim CI (state)** | `dbt ls --select 'state:modified+' --state /tmp/dbt_state --resource-type model` | lists **only** the changed model + its descendants (e.g. `spark_dev.marts.high_value_orders`) — the selector a CI job runs |
| **Deferral** | `dbt run -s dim_orders_keyed --defer --state /tmp/dbt_state` | builds only the changed model; unbuilt `ref()`s resolve to the **saved (prod) state** |

## 4. Takeaways & "in real production…"

- **Macros are the DRY layer of a dbt project.** Hoist repeated SQL (surrogate keys, audit columns,
  date spines) into one deterministic, null-safe macro; `dbt_utils.generate_surrogate_key` is the
  packaged version of the one here. A deterministic key is what makes joins survive rebuilds.
- **A test is just a macro that selects bad rows** — custom generic tests (`non_negative`) and
  reusable transforms are the same Jinja skill.
- **Slim CI is non-negotiable past a few dozen models.** Save the prod `manifest.json` as a CI
  artifact, then run `dbt build -s 'state:modified+' --state <saved>` on PRs so you rebuild **only
  changed models and their descendants**, not the whole warehouse. `modified+` = *changed nodes
  **plus** everything downstream* — the `+` is mandatory because a change ripples to consumers.
- **`--defer` lets devs iterate cheaply** — build the one model you're editing and read every
  unchanged upstream from prod's state, instead of rebuilding the world in a dev schema.
- The connective tissue is `target/manifest.json`: dbt's self-describing graph. Once you treat it as
  durable state to diff against, slim CI and deferral both fall out of the same `--state` flag.

## 5. Teardown

`dim_orders_keyed` lives in the shared [`dbt/`](../../dbt/) project (`dbt build`/`--full-refresh`
recreates it from the seeds); the macros create nothing. Remove the saved-state copy with
`rm -rf /tmp/dbt_state`. `make clean` clears all generated data under `.tmp/`.
