# `airflow/` — Orchestration challenges (Phase 6) ✅ complete

Generic, **fully local** teaching DAGs that orchestrate *this repo's own* Spark/dbt/GE jobs — no
cloud, no internal infra. Each DAG demonstrates one production orchestration concept following
**Break → Detect → Fix → Prove** (see [`docs/CURRICULUM_BRIEF.md`](../docs/CURRICULUM_BRIEF.md)),
written in their module docstring + the DAG's `doc_md`.

Airflow **3.1.7** runs locally in its own `uv` venv (isolated from the main project). The DAGs use
the Airflow-3 SDK (`airflow.sdk`: `DAG`, `@task`, `get_current_context`, `Asset`) and the standard
provider (`airflow.providers.standard.operators.bash` / `.operators.python` / `.sensors.python`).

> **Run the whole thing (UI):** `make airflow-up` → http://localhost:5000 (login `airflow`/`airflow`).
> DAGs live in `airflow/dags/`.
>
> **Run/verify one DAG headlessly** (how every module here was verified — synchronous, no scheduler):
> ```bash
> cd airflow
> AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags AIRFLOW__CORE__LOAD_EXAMPLES=false \
>   uv run airflow dags test <dag_id> 2025-03-01
> ```
>
> **Laptop-safe:** tiny/deterministic tasks; any output goes under the repo's `.tmp/` (`make clean`
> recovers). `AF-10` shells into the repo's `uv` project to run real `dbt build`/`test` + a Great
> Expectations gate against the running Spark stack (so `make up` must be running for AF-10).

## Modules (DAGs in `airflow/dags/`)

`[ ]` not started · `[~]` in progress · `[x]` built & verified (`airflow dags test`)

| ID | DAG (`dag_id`) | Concept | Status |
|----|----------------|---------|--------|
| `AF-1` | `af1_idempotency` | Idempotency & deterministic tasks — re-run/backfill safely (overwrite a partition keyed on the data interval, never append) | `[x]` |
| `AF-2` | `af2_execution_model` | The data-interval execution model; why `now()` is an antipattern; stable interval across retries/backfills | `[x]` |
| `AF-3` | `af3_catchup_backfill` | `catchup`, replaying history, `airflow dags backfill` over a date range without collisions | `[x]` |
| `AF-4` | `af4_retries_sla` | Retries, `retry_delay`, exponential backoff; SLA → Airflow-3 deadline alerting | `[x]` |
| `AF-5` | `af5_sensor_modes` | Sensor poke vs reschedule vs deferrable/async; freeing worker slots | `[x]` |
| `AF-6` | `af6_trigger_rules_branching` | Trigger rules (all_success/all_done/none_failed…), branching, short-circuit | `[x]` |
| `AF-7` | `af7_dynamic_mapping` | Dynamic task mapping over a list of tables/configs; TaskGroups | `[x]` |
| `AF-8` | `af8_xcom_limits` | XCom for small metadata vs passing URIs for large data; what NOT to push | `[x]` |
| `AF-9` | `af9_assets_data_aware` | Airflow 3 Assets — a consumer DAG runs when a producer updates an asset (data-aware scheduling) | `[x]` |
| `AF-10` | `af10_dbt_spark_e2e` | Orchestrate the real repo: dbt build → test → Great Expectations gate → cleanup; Cosmos vs BashOperator; the top-level-code antipattern | `[x]` |

## Layout

```
airflow/
├── README.md                 # this file (Phase 6 track index)
├── pyproject.toml            # Airflow 3.1.7 + providers + astronomer-cosmos (separate uv venv)
├── passwords.json            # local UI auth (airflow/airflow)
└── dags/
    ├── example_dag.py        # trivial smoke DAG
    └── af1_idempotency.py … af10_dbt_spark_e2e.py   # the 10 teaching DAGs
```

## Suggested order

`AF-1` (idempotency) → `AF-2` (execution model) → `AF-3` (catchup/backfill) — the data-interval
foundation; then `AF-4` (retries/SLA) → `AF-5` (sensors) → `AF-6` (trigger rules/branching) →
`AF-7` (dynamic mapping) → `AF-8` (XCom) → `AF-9` (assets) — the orchestration toolkit; finishing
with `AF-10` (the dbt+Spark+GE end-to-end), which ties Phases 2–5 together under one DAG and is the
bridge to **Phase 7's capstone**.

## How it connects to the rest of the curriculum

- **AF-10** runs the **Phase 5** dbt models + the Great Expectations gate (`dbt/quality/`) on the
  **Phase 1–2** Spark/Iceberg/Delta stack — the same models the **Phase 4** CDC pipeline feeds.
- **AF-1/2/3** (idempotency, data-interval, backfill) are exactly what make the incremental and
  late-arriving patterns from **DBT-2/DBT-3** safe to replay.
