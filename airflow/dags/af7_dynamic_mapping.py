"""AF-7 — Dynamic task mapping & TaskGroups (Break → Detect → Fix → Prove).

**Scenario.** You need to process "all the tables that exist today" — but *how many* and *which ones*
isn't known when you write the DAG; it's only known when an upstream task runs (a metastore query, a
directory listing, a config file). Hard-coding one task per table is the trap: the list drifts, you
either miss new tables or carry dead tasks. Airflow's answer is **dynamic task mapping** — a single
mapped task `.expand()`s over a list produced *at runtime* into one task instance per element, each
with its own **map-index**. Grouping the related steps in a **TaskGroup** keeps the graph readable.

**Break (the anti-pattern this replaces).** Writing N separate `process_orders`, `process_customers`,
… tasks bakes the table list into the *DAG structure*. Add a table → edit the DAG; the producer can't
influence the shape. The structure is frozen at parse time, but the work is only known at run time.

**Detect.** `list_tables` returns `["orders","customers","events","payments"]` and `process.expand(...)`
fans out. In `dags test` logs / the Grid view you'll see **4 mapped instances** of `process`
(`map_index=0..3`), one per table — created at runtime, not written in the file. Change the producer's
list and the instance count follows automatically; the source code is untouched.

**Fix / pattern.** `list_tables` (producer) → `process.expand(table=<list>)` (one mapped instance per
table, each doing cheap deterministic work and writing a tiny marker under `.tmp/af7/`) → `summarize`
(reduce: receives **all** mapped outputs as a single list and sums them). The producer + mapped task
live inside a `@task_group("ingest")` so the per-table work is one collapsible unit in the UI.

**Prove.** Each mapped instance prints its `map_index`, its table, and its computed `rows`, and drops
`.tmp/af7/<table>.json`. `summarize` prints the per-table rows it collected and the grand total — proof
that the reduce step sees the whole expanded set as a list. Mapping count == len(producer list), decided
at runtime; the DAG file never names the individual tables as tasks.

Run it:
    cd airflow && AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags \\
      uv run airflow dags test af7_dynamic_mapping 2025-03-01
    # → 4 mapped 'ingest.process' instances (map_index 0..3) + one 'summarize' with the total.

In real production: derive the fan-out list from the real world (metastore/listing/config) and
`.expand()` over it instead of hard-coding per-entity tasks — the DAG then adapts as entities come and
go. Keep the mapped step's work small and independent (it runs once per element), and use a reduce task
for cross-element aggregation. Wrap multi-step per-entity logic in a TaskGroup for a navigable graph.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from airflow.sdk import DAG, get_current_context, task, task_group

# Anchor outputs at the repo's .tmp/ (gitignored; `make clean` recovers). The DAG file lives at
# <repo>/airflow/dags/, so the repo root is two levels up.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(REPO, ".tmp", "af7")


with DAG(
    dag_id="af7_dynamic_mapping",
    start_date=datetime(2025, 1, 1),
    schedule=None,
    catchup=False,
    tags=["airflow-curriculum", "AF-7", "dynamic-mapping", "task-group"],
    doc_md=__doc__,
):

    @task_group(group_id="ingest")
    def ingest():
        """Per-table ingest as one collapsible unit: produce the list, then map over it at runtime."""

        @task
        def list_tables() -> list[str]:
            """Producer: the fan-out list, decided at RUNTIME (here static; in prod a metastore/listing)."""
            ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
            tables = ["orders", "customers", "events", "payments"]
            print(f"[list_tables] dt={ds} discovered {len(tables)} tables → {tables} "
                  f"(process will expand into {len(tables)} mapped instances)")
            return tables

        @task
        def process(table: str) -> dict:
            """Mapped task: ONE instance per table (its own map_index). Cheap, deterministic work."""
            ctx = get_current_context()
            mi = getattr(ctx.get("ti"), "map_index", -1)  # which mapped instance this is (0..N-1)
            rows = len(table) * 10  # deterministic stand-in for "rows processed"
            os.makedirs(OUT, exist_ok=True)
            marker = os.path.join(OUT, f"{table}.json")
            with open(marker, "w") as f:  # overwrite → idempotent across re-runs (see AF-1)
                json.dump({"table": table, "rows": rows}, f)
            print(f"[process map_index={mi}] table={table} rows={rows} → wrote {marker}")
            return {"table": table, "rows": rows}

        # .expand() turns the producer's list into one mapped 'process' instance per element.
        return process.expand(table=list_tables())

    @task
    def summarize(results: list[dict]):
        """Reduce: receives ALL mapped outputs as a single list and aggregates across them."""
        per_table = {r["table"]: r["rows"] for r in results}
        total = sum(r["rows"] for r in results)
        print(f"[summarize] collected {len(results)} mapped results: {per_table}")
        print(f"[summarize] grand total rows across all mapped tables = {total}")

    # ingest() returns the mapped 'process' output; summarize collects the full list.
    summarize(ingest())
