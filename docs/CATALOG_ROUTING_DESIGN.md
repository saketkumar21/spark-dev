# Explicit Multi-Catalog Routing (Delta · Iceberg · Hudi)

> **DECISION (adopted): the schema-string trick, no package.** A dbt model targets a catalog by putting
> it in the schema — `{{ config(schema='iceberg_catalog.marts', file_format='iceberg') }}` → dbt-spark
> renders `iceberg_catalog.marts.<t>`. The repo's `generate_schema_name` override already passes a custom
> schema through, so **no code is required**. Verified on stock dbt-spark: delta-schema model → provider
> delta in `spark_catalog`, iceberg-schema model → provider iceberg in `iceberg_catalog`. This is the
> user's real-world approach (`+schema: silver.schema` against AWS Glue).
>
> The `dbt-spark-catalog` `.pth` monkeypatch described below (relaxing `SparkRelation`'s
> `"Cannot set database in spark!"` guard so the proper `database` field works) was prototyped, verified,
> and then **removed as over-engineered** — kept here only as background/analysis. Prefer the trick.

Make this repo's Spark support **three table-format catalogs** where the user
**explicitly chooses the catalog by name** — never inferred from `file_format`. Two
usage paths:

1. **Raw SQL / notebooks** — write `CREATE TABLE <catalog>.<ns>.<t> USING <fmt> …`
   against the chosen catalog. Works today for Delta + Iceberg (verified below); Hudi
   needs a JAR + catalog (design at the end).
2. **dbt** — set the target catalog explicitly in model config
   (`{{ config(database='iceberg_catalog') }}`) or at project level, and the table is
   created in that catalog.

Catalogs are registered in `conf/spark-defaults.conf`:

| Catalog (name)    | Implementation                                          | Default? | Warehouse |
|-------------------|---------------------------------------------------------|----------|-----------|
| `spark_catalog`   | `org.apache.spark.sql.delta.catalog.DeltaCatalog`       | ✅ yes   | `.tmp/local_delta_warehouse` |
| `iceberg_catalog` | `org.apache.iceberg.spark.SparkCatalog` (type=hadoop)   | no       | `.tmp/local_iceberg_warehouse` |
| `hudi_catalog`    | `org.apache.spark.sql.hudi.catalog.HoodieCatalog` *(proposed)* | no | `.tmp/local_hudi_warehouse` |

> **Key principle:** catalog (where the table lives) and `file_format` (`USING delta`
> / `iceberg` / `hudi`, the DDL clause) are **orthogonal**. The user sets *both*:
> `{{ config(database='iceberg_catalog', file_format='iceberg') }}`. This work only
> makes the **catalog targeting** possible; it does not auto-pick a format.

---

## 1. Path 1 — Raw SQL / notebooks (already works for Delta + Iceberg)

No code change needed. Spark resolves a 3-level name against the named catalog:

```sql
-- Delta, explicit default catalog
CREATE OR REPLACE TABLE spark_catalog.analytics.t USING delta    AS SELECT 1 id;
-- Iceberg, explicit catalog
CREATE OR REPLACE TABLE iceberg_catalog.analytics.t USING iceberg AS SELECT 2 id;
```

**Verified** over Spark Connect (`common.spark_session.get_spark`) and over Thrift
(pyhive @ `localhost:10000`):

```
delta provider:   ['delta']    | iceberg provider: ['iceberg']
delta rows: [1]                | iceberg rows: [2]
SHOW TABLE EXTENDED … iceberg_catalog.tmp_catalogtest.zz_ice →
    Catalog: iceberg_catalog · Provider: iceberg ·
    Location: ./.tmp/local_iceberg_warehouse/…
```

Iceberg CTAS, `SHOW NAMESPACES IN iceberg_catalog`, `SHOW TABLES IN …`, and
`SHOW TABLE EXTENDED … LIKE '*'` all work over Thrift. (Pre-existing repo behavior —
notebooks already use `iceberg_catalog.…` explicitly.)

---

## 2. Path 2 — dbt (the real work)

### 2.1 Why stock dbt-spark can't do it

dbt-spark is hard-wired to a **single** (default) catalog. Three independent guards —
one at **parse time**, two at **runtime** — strip the catalog:

| # | Layer | Location | What it does |
|---|-------|----------|--------------|
| A | **parse** | `dbt/include/spark/macros/adapters.sql` → `spark__generate_database_name` | `{% do return(None) %}` — discards `config(database=…)`; the node's `database` becomes `None`. |
| B | runtime | `dbt/adapters/spark/relation.py` → `SparkRelation.__post_init__` | raises `DbtRuntimeError("Cannot set database in spark!")` if `database` is set. |
| C | runtime | same file → `SparkIncludePolicy.database = False` + `SparkRelation.render()` | the `database` level is excluded from rendering; `render()` even *raises* if both database and schema are included. |
| (D) | runtime | `dbt/adapters/spark/impl.py:273` `get_relation` | nulls `database` unless `get_default_include_policy().database` is True. |

dbt-core itself is catalog-capable: `BaseRelation.create_from` already threads
`relation_config.database` into the relation path, and `default__generate_database_name`
honors a custom name. **dbt-spark deliberately neutralizes it** at A/B/C. Relax all
three and explicit targeting "just works".

Proof of the blocker (stock, unpatched):

```
create/post_init RAISED: DbtRuntimeError: Cannot set database in spark!
default render (db==schema): analytics.x          # 2-level, no catalog
get_default_include_policy().database: False
```

### 2.2 The mechanism — two halves

Because the parse-time guard (A) is a **Jinja macro** and the runtime guards (B/C/D)
are **Python**, the fix is two small, backward-compatible pieces. Both are **no-ops
unless a model sets `database`**, so the existing project (and the parallel transpile
workstream) are unaffected.

#### Half 1 — Python `.pth` monkeypatch (runtime: B, C, D)

A standalone package `dbt/dbt-spark-catalog/` (mirrors `dbt/dbt-spark-transpile/`): a
`.pth` imports a module that patches dbt-spark at interpreter start-up, **guarded with
try/except** (non-dbt Python is unaffected) and `*args/**kwargs`-tolerant.

`dbt/dbt-spark-catalog/dbt_spark_catalog.py` (full source; also lives in-repo):

```python
"""dbt-spark-catalog — let a dbt-spark model target an explicit Spark catalog."""
import os

try:
    from dbt.adapters.spark import relation as _spark_relation
    from dbt.adapters.spark.relation import (
        SparkRelation, SparkIncludePolicy, SparkQuotePolicy,
    )
    from dbt.adapters.events.logging import AdapterLogger
except Exception:                      # non-dbt Python / version drift → disable
    SparkRelation = None

if SparkRelation is not None:
    _logger = AdapterLogger("SparkCatalog")

    _flag = os.environ.get("DBT_SPARK_CATALOG_ALWAYS_3LEVEL", "1").strip().lower()
    RENDER_DB_WHEN_EQUAL = _flag not in ("0", "false", "no", "off")

    # 1. Drop the "Cannot set database in spark!" guard.
    def _patched_post_init(self, *args, **kwargs):
        return None
    SparkRelation.__post_init__ = _patched_post_init

    # 2. Turn the database level ON. SparkIncludePolicy/QuotePolicy are dataclasses,
    #    so `SparkIncludePolicy.database = True` is ignored by the constructor; we must
    #    override the *default_factory* on SparkRelation's two policy fields. This
    #    fixes get_default_include_policy() (impl.get_relation relies on it).
    import dataclasses
    def _include_factory():
        return SparkIncludePolicy(database=True, schema=True, identifier=True)
    def _quote_factory():
        return SparkQuotePolicy(database=False, schema=False, identifier=False)
    for _f in dataclasses.fields(SparkRelation):
        if _f.name == "include_policy":
            _f.default_factory = _include_factory
        elif _f.name == "quote_policy":
            _f.default_factory = _quote_factory

    # 3. Replace render() so the catalog level is emitted and it never raises.
    #    dbt-spark's mashumaro deserializer bakes database=False into each *instance*
    #    policy, so we render straight from the path components (which already carry
    #    the catalog via BaseRelation.create_from) rather than the instance policy.
    def _patched_render(self, *args, **kwargs):
        db, sch, ident = self.database, self.schema, self.identifier
        from dbt.adapters.base.relation import ComponentName
        ip = getattr(self, "include_policy", None)
        def _level_on(level, default):
            if ip is None:
                return default
            try:
                v = ip.get_part(level)
            except Exception:
                return default
            return default if v is None else bool(v)
        parts = []
        if db is not None and (RENDER_DB_WHEN_EQUAL or db != sch):
            parts.append(db)
        if sch is not None and _level_on(ComponentName.Schema, True):
            parts.append(sch)
        if ident is not None and _level_on(ComponentName.Identifier, True):
            parts.append(ident)
        return ".".join(parts)
    SparkRelation.render = _patched_render
```

`dbt_spark_catalog.pth` → `import dbt_spark_catalog`.
`setup.py` → identical recipe to dbt-spark-transpile (`build_py` copies the `.pth`
into the wheel's purelib root so it installs into `site-packages` and auto-activates).

> **Design notes**
> - Patching the **`default_factory`** (not the dataclass class attr) is required —
>   field defaults are frozen into `__init__` at class-definition time.
> - The render is driven by the **path components**, not the instance `include_policy`,
>   because mashumaro's generated `from_dict` (used by `SparkRelation.create`) re-bakes
>   the original `database=False` default into every instance. Fighting that is
>   fragile; reading the path is robust and the path already carries the catalog.
> - `DBT_SPARK_CATALOG_ALWAYS_3LEVEL=0` reverts to classic 2-level rendering for
>   *un-targeted* relations (db == schema). Default `1` renders 3-level always, which
>   is harmless (`spark_catalog.analytics.t` ≡ `analytics.t` to Spark).

#### Half 2 — project macro override (parse: A)

`.pth`/site-packages can't reach into a dbt project's macro set, so the parse-time
guard is fixed the idiomatic dbt way — a project macro. `dbt/macros/generate_database_name.sql`:

```jinja
{% macro spark__generate_database_name(custom_database_name=none, node=none) -%}
    {%- if custom_database_name is none -%}
        {{ return(None) }}              {# stock behavior: use default catalog #}
    {%- else -%}
        {{ return(custom_database_name | trim) }}
    {%- endif -%}
{%- endmacro %}
```

This restores dbt-core's standard database semantics **only for the database name**:
set `database` → that catalog; unset → `None` (byte-for-byte stock dbt-spark). Models
that never set `database` are completely unaffected.

### 2.3 How a dbt user selects a catalog

**Per model** (overrides project default):

```sql
{{ config(
    materialized='table',
    database='iceberg_catalog',   -- the Spark catalog (a.k.a. "database" in dbt)
    schema='analytics',
    file_format='iceberg'         -- orthogonal: the USING clause
) }}
select ...
```

**Project / folder default** (`dbt_project.yml`):

```yaml
models:
  spark_dev:
    marts:
      +database: iceberg_catalog
      +file_format: iceberg
      +schema: marts
```

Recommended pairings:

| Target catalog    | `database`        | `file_format` |
|-------------------|-------------------|---------------|
| Delta (default)   | omit, or `spark_catalog` | `delta` (or omit) |
| Iceberg           | `iceberg_catalog` | `iceberg` |
| Hudi *(once installed)* | `hudi_catalog` | `hudi` |

> dbt-spark only emits `create or replace table` (vs plain `create table`) when
> `file_format ∈ {delta, iceberg}` — Iceberg/Delta support CREATE OR REPLACE; plain
> Hive does not. Hudi uses `create table` + `options(primaryKey=…)`; for Hudi prefer
> `materialized='incremental'` with a `unique_key`, or full-refresh tables.

### 2.4 Integration steps for the main thread (NOT done here — avoids lockfile collisions)

1. **Install the package** into the dbt venv so its `.pth` auto-activates the runtime
   patch. Mirror the transpile entry in `pyproject.toml`:
   ```toml
   [tool.uv.sources]
   dbt-spark-catalog = { path = "dbt/dbt-spark-catalog", editable = true }
   ```
   and add `dbt-spark-catalog` to dependencies, then `uv sync`. (The `.pth` install is
   what makes it load for `uv run dbt`; a plain editable install of the `.py` is not
   enough — the `build_py` shim copies the `.pth` into site-packages.)
2. **`dbt/macros/generate_database_name.sql`** is already in place (Half 2) — no action;
   it auto-loads with the project and is a no-op until a model sets `database`.
3. **No `dbt_project.yml` change required.** Optionally uncomment the `+file_format`
   hints already present, and add `+database:` to a subtree to make a whole folder
   target a catalog.
4. Optionally pin a supported dbt-core/dbt-spark range (these patches touch private
   seams: `SparkRelation`, mashumaro defaults, `spark__generate_database_name`).

---

## 3. Verification evidence

All runs against the live shared server, scoped to a scratch namespace
(`tmp_catalogtest`) and cleaned up; no existing model or `dbt_project.yml` touched, no
`uv sync`, no container restart.

### 3.1 Patched `SparkRelation` renders 3-level and does not raise (in-process)

```
get_default_include_policy().database: True
iceberg target render:      iceberg_catalog.tmp_catalogtest.zz_probe
spark_catalog target render: spark_catalog.analytics.zz_probe
hudi target render:          hudi_catalog.analytics.zz_probe
db==schema render:           analytics.analytics.x   (always_3level=1; harmless)
str()/repr()/hash() — no raise
ALL RENDER ASSERTS PASSED
```

Fail-open guard (dbt-spark import blocked) → module imports cleanly, `SparkRelation is None`.

### 3.2 End-to-end via the dbt adapter's transport (pyhive) — catalog routing + DDL

```
RENDERED: iceberg_catalog.tmp_catalogtest.zz_probe
DDL:      create or replace table iceberg_catalog.tmp_catalogtest.zz_probe using iceberg as select …
CTAS OK
SHOW TABLE EXTENDED → Catalog: iceberg_catalog · Provider: iceberg ·
                      Location: ./.tmp/local_iceberg_warehouse/…
SELECT * → [(1, 'a')]
```

### 3.3 Full real `dbt run` of a scratch model (highest fidelity)

Scratch model `models/marts/zz_catalog_probe_iceberg.sql` with
`{{ config(database='iceberg_catalog', schema='tmp_catalogtest', file_format='iceberg') }}`,
run with the patch active (injected for the subprocess via a temporary
`sitecustomize.py` on `PYTHONPATH`) + the macro override:

```
1 of 1 OK created sql table model iceberg_catalog.tmp_catalogtest.zz_catalog_probe_iceberg [OK in 0.21s]
Done. PASS=1 WARN=0 ERROR=0
```

- Compiled run SQL: `create or replace table iceberg_catalog.tmp_catalogtest.zz_catalog_probe_iceberg …` (**3-level**).
- Parsed manifest node: `config.database='iceberg_catalog'`, **and** `node.database='iceberg_catalog'`, `relation_name='iceberg_catalog.tmp_catalogtest.zz_catalog_probe_iceberg'` (without Half 2 the node's `database` was `None` and the relation 2-level — that confirmed the parse-time macro is the missing piece).
- Physical placement: `Catalog: iceberg_catalog`, `Provider: iceberg`, under `local_iceberg_warehouse`.

Scratch model, table, namespaces (both the iceberg one and a stray empty
`spark_catalog.tmp_catalogtest` dbt's `create_schema` left), temp `target/`, and the
temp `sitecustomize` were all removed. Server confirmed back to its original namespace
sets in both catalogs.

> **Note on the test harness:** `uv run dbt` does load a `PYTHONPATH`-provided
> `sitecustomize.py` (verified via sentinel files), so that was a valid way to activate
> the patch for a one-off run without installing it. In production the `.pth` (step 1
> above) is the real activation path.

### 3.4 Metadata compatibility (the ops dbt relies on), against `iceberg_catalog`

All green against a 3-level Iceberg relation:

| dbt method | Underlying SQL | Result |
|------------|----------------|--------|
| `list_relations_without_caching` | `SHOW TABLE EXTENDED IN iceberg_catalog.tmp_catalogtest LIKE '*'` | returns the table + an `information` blob carrying `Catalog/Provider/Schema` |
| `get_columns_in_relation` → `parse_describe_extended` | `DESCRIBE EXTENDED iceberg_catalog.tmp_catalogtest.zz_probe` | columns `id:int, name:string` parsed correctly; Iceberg metadata columns (`_spec_id`, `_file`, …) appear and are handled |
| relation existence | `SHOW TABLES IN … LIKE 'zz_probe'` | found |

`impl.get_relation` no longer nulls `database` because
`get_default_include_policy().database` is now `True` (Half 1, default_factory patch).

---

## 4. Cross-catalog `ref()` behavior

- `ref()` to a model in a **different** catalog renders the **fully-qualified 3-level
  source** (`other_catalog.schema.model`). Spark resolves it as long as both catalogs
  are registered in `spark-defaults.conf` — so a *read* across catalogs in a SELECT is
  fine (e.g. an Iceberg model selecting from a Delta source).
- The patch changes **targeting/rendering only**, not Spark's execution semantics.
  Spark can read across catalogs in one query; whether a single
  `CREATE TABLE a.b.c AS SELECT … FROM x.y.z` that spans two *different* catalog
  implementations executes depends on Spark/connector support and is unchanged here.
- **Recommendation:** keep a model and the sources it selects from in the **same
  catalog** where practical. When you do cross catalogs, prefer Iceberg/Delta as the
  *reader* side (mature `DataSourceV2` read paths). Within one catalog, `ref()` is
  unaffected and works exactly as today.
- `dim_*`/`agg_*` style marts that `ref()` staging models: if you move marts to
  `iceberg_catalog` but leave staging in `spark_catalog` (Delta), the `ref()` renders
  `spark_catalog.staging.stg_x` and the Iceberg-targeted mart reads it via Spark's
  multi-catalog resolution. Validate per-model; simplest is to move a whole lineage to
  one catalog via a folder-level `+database`.

---

## 5. Hudi enablement — design + exact diffs (NOT applied; needs image rebuild + restart)

Hudi is **not installed** (only `delta` + `iceberg` JARs are in `$SPARK_HOME/jars`, and
there is no `hudi_catalog`). Adding it requires a **Docker image rebuild + container
restart** — deliberately **not done** here (a parallel workstream shares the live
server). Below are the concrete diffs to apply.

### 5.1 Confirmed Maven coordinates (Spark 4.0, Scala 2.13)

Verified against Maven Central (`repo1.maven.org/maven2/org/apache/hudi/`):

- Artifact: **`org.apache.hudi:hudi-spark4.0-bundle_2.13`**
  (note `hudi-spark4.0-bundle_2.13` — there is also a `hudi-spark4.1-bundle_2.13`; pick
  the one matching Spark 4.0).
- Latest version: **`1.2.0`** (available: `1.1.0`, `1.1.1`, `1.2.0`; metadata updated
  2026-05-23).
- → coordinate: **`org.apache.hudi:hudi-spark4.0-bundle_2.13:1.2.0`**

The bundle is self-contained (shades Avro/Parquet/etc.), so it slots into the existing
Ivy stage exactly like the Iceberg/Delta runtimes.

### 5.2 `Dockerfile` — add the bundle to the Ivy resolution stage

```diff
 ARG SPARK_PACKAGES="\
 org.apache.iceberg:iceberg-spark-runtime-4.0_2.13:1.10.1,\
 io.delta:delta-spark_2.13:4.0.0,\
-org.apache.spark:spark-sql-kafka-0-10_2.13:4.0.2"
+org.apache.spark:spark-sql-kafka-0-10_2.13:4.0.2,\
+org.apache.hudi:hudi-spark4.0-bundle_2.13:1.2.0"
```

(No other Dockerfile change — stage 2 already copies all resolved jars from
`/tmp/ivy/jars/` onto the system classpath, which is the classloader-safe path the
Thrift Server needs.)

### 5.3 `conf/spark-defaults.conf` — extension, serializer, catalog, warehouse

```diff
 spark.sql.extensions                    org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions,\
-                                        io.delta.sql.DeltaSparkSessionExtension
+                                        io.delta.sql.DeltaSparkSessionExtension,\
+                                        org.apache.spark.sql.hudi.HoodieSparkSessionExtension
+
+# Hudi requires Kryo (it registers custom serializers). Harmless for Delta/Iceberg.
+spark.serializer                        org.apache.spark.serializer.KryoSerializer
+
+# Hudi catalog (Spark SQL catalog plugin). Managed/Hive-style — not a Hadoop FS catalog.
+spark.sql.catalog.hudi_catalog                      org.apache.spark.sql.hudi.catalog.HoodieCatalog
+spark.sql.catalog.hudi_catalog.type                 hadoop
+spark.sql.catalog.hudi_catalog.warehouse            ./.tmp/local_hudi_warehouse
```

> Caveats:
> - `HoodieCatalog` is a `CatalogExtension`-style plugin. The `type=hadoop`/`warehouse`
>   keys are the conventional way to point it at a local dir; if `HoodieCatalog` rejects
>   `type`, drop that key and keep just `.warehouse` (confirm against the 1.2.0 docs on
>   first boot). The catalog name `hudi_catalog` is what dbt's `database='hudi_catalog'`
>   targets.
> - `spark.serializer=KryoSerializer` is a global change. It's the Hudi-recommended
>   setting and is safe for Delta/Iceberg, but it *is* a behavioral change for the whole
>   server — call it out when rebuilding.
> - Adding `HoodieSparkSessionExtension` alongside the Iceberg + Delta extensions is
>   supported (extensions compose); order is not significant for these three.

### 5.4 `scripts/docker-entrypoint.sh` + `Dockerfile` mkdir — warehouse + namespaces

Iceberg's Hadoop catalog needs namespace **directories** pre-created (that's why the
entrypoint `mkdir`s them). Hudi's `HoodieCatalog` creates namespaces lazily via
`CREATE NAMESPACE`/table writes, so directory pre-creation is *probably* unnecessary —
but to match the Iceberg pattern and avoid a first-run `SCHEMA_NOT_FOUND` over Thrift,
create the warehouse dir (and optionally the same namespace dirs):

```diff
 # docker-entrypoint.sh
 mkdir -p .tmp/spark-events .tmp/local_iceberg_warehouse .tmp/local_delta_warehouse \
-         .tmp/metastore .tmp/spark-warehouse logs
+         .tmp/local_hudi_warehouse .tmp/metastore .tmp/spark-warehouse logs

 mkdir -p .tmp/local_iceberg_warehouse/{default,analytics,staging,marts,seeds}
+# Hudi (HoodieCatalog) — create the warehouse root; namespaces are created lazily on
+# first CREATE NAMESPACE / write. Pre-create dirs only if Thrift connect reports
+# SCHEMA_NOT_FOUND for hudi_catalog.default on a fresh boot:
+# mkdir -p .tmp/local_hudi_warehouse/{default,analytics,staging,marts,seeds}
```

```diff
 # Dockerfile
 RUN mkdir -p .tmp/spark-events .tmp/local_iceberg_warehouse .tmp/local_delta_warehouse \
-             .tmp/metastore .tmp/spark-warehouse \
+             .tmp/local_hudi_warehouse .tmp/metastore .tmp/spark-warehouse \
              logs /opt/spark/logs
```

### 5.5 Apply order (for whoever does the rebuild)

1. Apply the 3 diffs above.
2. Rebuild the image (resolves the Hudi bundle via Ivy): `make build` /
   `docker compose build spark-connect`.
3. Restart: `make down && make up` (or `up-constrained`).
4. Smoke test over Thrift/Connect:
   ```sql
   CREATE NAMESPACE IF NOT EXISTS hudi_catalog.analytics;
   CREATE TABLE hudi_catalog.analytics.t USING hudi
     TBLPROPERTIES (primaryKey='id') AS SELECT 1 id, 'a' name;
   SELECT * FROM hudi_catalog.analytics.t;          -- expect (1,'a')
   SHOW TABLE EXTENDED IN hudi_catalog.analytics LIKE 't';  -- Provider: hudi
   ```
5. dbt smoke test: a model with `{{ config(database='hudi_catalog', file_format='hudi',
   unique_key='id', materialized='incremental') }}` (Hudi needs a primary/record key;
   the spark `options_clause` macro auto-maps `unique_key`→`primaryKey`).
6. `make clean` recovers all generated data (incl. `.tmp/local_hudi_warehouse`).

> **`make clean`** already does `rm -rf .tmp`, so the new Hudi warehouse is covered.

---

## 6. File inventory (this feature)

| Path | Role | Status |
|------|------|--------|
| `dbt/dbt-spark-catalog/dbt_spark_catalog.py` | Runtime monkeypatch (B/C/D) | ✅ created, tested |
| `dbt/dbt-spark-catalog/dbt_spark_catalog.pth` | Auto-activates the patch | ✅ created |
| `dbt/dbt-spark-catalog/setup.py` | Wheel build (copies `.pth` to purelib) | ✅ created |
| `dbt/macros/generate_database_name.sql` | Parse-time fix (A), no-op when unused | ✅ created, tested |
| `docs/CATALOG_ROUTING_DESIGN.md` | This document | ✅ |

**Not touched** (per constraints): `pyproject.toml`, `uv.lock`, `dbt/dbt_project.yml`,
`dbt/dbt-spark-transpile/*`, `dbt/models/marts/poc_dialect_demo.sql`. The scratch probe
model and all temp test artifacts were deleted; nothing was committed; the Docker image
was not rebuilt and the container was not restarted.
