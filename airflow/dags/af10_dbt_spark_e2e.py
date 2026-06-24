"""AF-10 — End-to-end: Spark/dbt build → test → quality gate → cleanup (Break → Detect → Fix → Prove).

**Scenario.** The capstone orchestration: a single DAG drives *this repo's own* pipeline —
build the dbt models on Spark, run the dbt tests, run a standalone Great Expectations gate, then
clean up — all locally, no cloud. It shows how Airflow invokes work that lives in a *different*
environment (the repo's `uv` project), and the **Cosmos vs BashOperator** trade-off.

**The orchestration (linear, each a real command against the running stack):**
1. `dbt_build`     — `dbt build` the staging + marts models (the Spark/Delta transform).
2. `dbt_test`      — `dbt test` the marts (in-pipeline structural/business assertions).
3. `ge_quality_gate` — standalone Great Expectations on `orders_clean` (statistical gate; fails the
   DAG on a breach because the script exits non-zero — see DBT-8).
4. `cleanup`       — drop scratch state (here: a no-op marker; real jobs vacuum temp tables).

**BashOperator vs Cosmos (the DBT-10/AF-10 trade-off).** This DAG uses `BashOperator` to shell into
the repo's `uv` project — simple and robust, but each `dbt` invocation is **one opaque Airflow task**
(coarse retries, no per-model lineage in the Airflow UI). **astronomer-cosmos** (installed here)
renders a dbt project as a `TaskGroup` with **one Airflow task per model/test**, giving fine-grained
retries, selective re-runs, and dbt lineage in the Airflow graph — at the cost of a heavier
integration. Sketch:

    from cosmos import DbtTaskGroup, ProjectConfig, ProfileConfig, ExecutionConfig
    orders = DbtTaskGroup(
        project_config=ProjectConfig(REPO + "/dbt"),
        profile_config=ProfileConfig(profile_name="spark_dev", target_name="dev",
                                     profiles_yml_filepath=REPO + "/dbt/profiles.yml"),
        execution_config=ExecutionConfig(dbt_executable_path="uv run dbt"),
    )

**Antipattern this DAG avoids — top-level code.** Everything that touches the DB / does heavy work
runs *inside tasks*. Module-level DB calls, network I/O, or heavy imports run on **every scheduler
parse** (every few seconds), hammering connections and slowing the whole scheduler. Keep the top
level to cheap imports + DAG structure only.

Run it (the Spark stack must be up — `make up`):
    cd airflow && AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags \\
      uv run airflow dags test af10_dbt_spark_e2e 2025-03-01

In real production: gate promotion on tests + expectations; orchestrate dbt with Cosmos for lineage;
alert on the quality gate; keep extracts idempotent (AF-1) so the whole DAG is safely re-runnable.
"""

from __future__ import annotations

import os
from datetime import datetime

from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator

# Repo root is two levels up from <repo>/airflow/dags/. Tasks shell into the repo's uv project,
# which is independent of Airflow's own venv.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DBT = f"cd {REPO}/dbt && set -a && . ./.env && set +a && uv run dbt"

with DAG(
    dag_id="af10_dbt_spark_e2e",
    start_date=datetime(2025, 1, 1),
    schedule=None,
    catchup=False,
    tags=["airflow-curriculum", "AF-10", "dbt", "spark", "e2e"],
):
    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command=f"{DBT} build -s stg_orders fct_orders stg_orders_quality orders_clean orders_quarantine",
    )
    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=f"{DBT} test -s orders_clean orders_quarantine",
    )
    ge_quality_gate = BashOperator(
        task_id="ge_quality_gate",
        bash_command=(
            f"cd {REPO} && PYTHONPATH={REPO} uv run python "
            f"dbt/quality/great_expectations/validate_table.py spark_catalog.marts.orders_clean"
        ),
    )
    cleanup = BashOperator(
        task_id="cleanup",
        bash_command="echo 'pipeline complete — real jobs would VACUUM/expire scratch tables here'",
    )

    dbt_build >> dbt_test >> ge_quality_gate >> cleanup
