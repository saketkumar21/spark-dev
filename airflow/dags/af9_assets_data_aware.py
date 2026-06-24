"""AF-9 — Assets & data-aware scheduling (Break → Detect → Fix → Prove).

**Scenario.** Two pipelines have a real data dependency: a *consumer* must run after a *producer*
refreshes a dataset. The brittle way is to guess at the timing — schedule the consumer 30 minutes
after the producer and hope the producer finished. Airflow 3 replaces the guess with a **data-aware**
link: the producer declares it *updates* an **Asset**, and the consumer is scheduled **on that
Asset**. When the producer's asset-producing task succeeds, the scheduler fires the consumer — no
clock coupling, no `ExternalTaskSensor`, no slack-time padding.

**Break.** Time-coupled scheduling: ``consumer schedule="30 6 * * *"`` chasing a producer at 06:00.
If the producer runs long, is delayed, or is backfilled off-cycle, the consumer reads **stale or
missing** data. The dependency is implicit and timing-fragile. (We describe this trap; the DAGs below
implement the fix.)

**Detect.** Without assets you can't *see* the dependency — it lives in two unrelated cron strings
and in someone's head. With assets, the link is first-class: the Airflow UI shows an **Assets** graph
(producer → asset → consumer), and each consumer run records *which* asset event triggered it.

**Fix.** One shared asset, ``orders_asset = Asset("repo://af9/orders")``:
  - **Producer** (`af9_assets_producer`): a ``@task(outlets=[orders_asset])`` "produces" the dataset
    (writes a tiny marker under ``.tmp/af9/``). Succeeding marks the asset as **updated**.
  - **Consumer** (`af9_assets_consumer`): ``schedule=[orders_asset]`` — **no time schedule at all**.
    The scheduler runs it automatically *when the asset updates*, then it reads the marker.

**Prove (honest about what `dags test` does vs. the scheduler).**
``dags test`` executes a single DAG in-process; it is **not** the scheduler, so it will **not**
auto-fire the consumer off the producer's asset update. So we verify each DAG **standalone**:
  - ``dags test af9_assets_producer`` runs the producer and marks ``orders_asset`` updated (you'll
    see the asset/outlet event in the run); the marker file appears under ``.tmp/af9/``.
  - ``dags test af9_assets_consumer`` runs the consumer in isolation and reads the marker.
The producer→asset→consumer *triggering* is a **scheduler behavior**: under a live scheduler (or in
the UI's Assets view) the consumer's next run is created the moment the producer updates the asset.
That's the part you observe in the running system, not in a one-shot `dags test`.

Run it:
    cd airflow && AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags \\
      uv run airflow dags test af9_assets_producer 2025-03-01
    cd airflow && AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags \\
      uv run airflow dags test af9_assets_consumer 2025-03-01
    # Each runs clean standalone. The auto-trigger (producer's asset update -> consumer run) is what
    # the SCHEDULER does — visible in the UI Assets graph, not in a single-DAG `dags test`.

In real production: model cross-DAG data dependencies as Assets, not as cron offsets or sensors.
The consumer runs exactly when its inputs are fresh; you get a visible lineage graph; and multi-input
consumers (``schedule=[asset_a, asset_b]``) fire only once *all* their upstream datasets have updated.
"""

from __future__ import annotations

import os
from datetime import datetime

from airflow.sdk import DAG, Asset, get_current_context, task

# Anchor outputs at the repo's .tmp/ (gitignored; `make clean` recovers). The DAG file lives at
# <repo>/airflow/dags/, so the repo root is two levels up.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(REPO, ".tmp", "af9")

# The shared dataset both DAGs reference. The URI is just a stable identifier — it does not have to
# resolve to a real filesystem path; it's the handle the scheduler links producer -> consumer on.
orders_asset = Asset("repo://af9/orders")


def _marker_path(ds: str) -> str:
    os.makedirs(OUT, exist_ok=True)
    return os.path.join(OUT, f"orders_{ds}.marker")


# --- Producer DAG: declares it UPDATES orders_asset via the task's outlets -----------------------
with DAG(
    dag_id="af9_assets_producer",
    start_date=datetime(2025, 1, 1),
    schedule="@daily",
    catchup=False,
    tags=["airflow-curriculum", "AF-9", "assets", "data-aware", "producer"],
    doc_md=__doc__,
):

    @task(outlets=[orders_asset])
    def produce_orders() -> None:
        """Produce the dataset and mark orders_asset updated (outlets=[orders_asset])."""
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        path = _marker_path(ds)
        with open(path, "w") as f:
            f.write(f"orders produced for dt={ds}\n")
        print(f"[producer] wrote marker {path}")
        print(
            f"[producer] task succeeds with outlets=[{orders_asset.uri}] -> scheduler marks the "
            "asset UPDATED -> any DAG scheduled on this asset becomes eligible to run"
        )

    produce_orders()


# --- Consumer DAG: scheduled ON the asset (no clock) — scheduler fires it on asset update ---------
with DAG(
    dag_id="af9_assets_consumer",
    start_date=datetime(2025, 1, 1),
    schedule=[orders_asset],  # data-aware: run when orders_asset updates, not on a time schedule
    catchup=False,
    tags=["airflow-curriculum", "AF-9", "assets", "data-aware", "consumer"],
    doc_md=__doc__,
):

    @task
    def consume_orders() -> None:
        """Consume the dataset the producer refreshed. Under the scheduler this run is triggered by
        the producer's asset update; under `dags test` it just runs standalone and reads the marker."""
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        path = _marker_path(ds)
        if os.path.exists(path):
            print(f"[consumer] read marker {path}: {open(path).read().strip()!r}")
        else:
            # Standalone `dags test` (or a date the producer hasn't produced) — honest about it.
            print(
                f"[consumer] no marker at {path} yet — run af9_assets_producer for this date first. "
                "Under a live scheduler the consumer fires AFTER the producer updates the asset, so "
                "the marker is present by then."
            )
        print(f"[consumer] scheduled on asset {orders_asset.uri} (schedule=[orders_asset], no cron)")

    consume_orders()
