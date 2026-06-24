"""AF-1 — Idempotency & deterministic tasks (Break → Detect → Fix → Prove).

**Scenario.** A daily job writes one partition of derived data. If the task is *not* idempotent,
re-running a date (a retry, a backfill, a manual clear-and-rerun) appends a second copy and the
partition is now double-counted. The fix is to make the write **deterministic and keyed on the
data interval**, so re-running a date *replaces* that date's partition instead of adding to it.

**Break.** `append_partition` opens the date's file in append mode (`"a"`). Run the same logical
date twice → the partition has 2× the rows. That's the classic non-idempotent task.

**Detect.** Compare the row count after one run vs two runs of the *same* `--logical-date`. A
non-idempotent task grows; an idempotent one is stable.

**Fix.** `overwrite_partition` writes the date's file in overwrite mode (`"w"`) and derives its
content *only* from `data_interval_start` (never `now()`), so the output is a pure function of the
interval. Re-runs and backfills converge to the same state — safe to replay.

**Prove.** Run this DAG twice for the same date; the overwrite partition's row count is identical
both times, while the append partition would have doubled. Each task prints its partition's row
count to the logs.

Run it:
    cd airflow && AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags \\
      uv run airflow dags test af1_idempotency 2025-03-01
    # run again with the SAME date → overwrite count stays the same (idempotent)

In real production: key every partitioned write on the data interval and use overwrite /
`MERGE`/`INSERT OVERWRITE` (see DBT-2), never blind append; that is what makes backfills (AF-3) safe.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from airflow.sdk import DAG, get_current_context, task

# Anchor outputs at the repo's .tmp/ (gitignored; `make clean` recovers). The DAG file lives at
# <repo>/airflow/dags/, so the repo root is two levels up.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(REPO, ".tmp", "af1_idempotency")


def _partition_path(kind: str, ds: str) -> str:
    d = os.path.join(OUT, kind, f"dt={ds}")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "data.jsonl")


def _rows_for_interval(ds: str) -> list[dict]:
    """Deterministic, pure function of the date — no now(), no randomness."""
    return [{"dt": ds, "metric": "orders", "value": 100 + (hash(ds) % 50)} for _ in range(3)]


with DAG(
    dag_id="af1_idempotency",
    start_date=datetime(2025, 1, 1),
    schedule="@daily",
    catchup=False,
    tags=["airflow-curriculum", "AF-1", "idempotency"],
):

    @task
    def append_partition():
        """BREAK: non-idempotent — appends, so a re-run double-writes the partition."""
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        path = _partition_path("append_broken", ds)
        with open(path, "a") as f:  # append → grows on every re-run
            for r in _rows_for_interval(ds):
                f.write(json.dumps(r) + "\n")
        n = sum(1 for _ in open(path))
        print(f"[append_broken] dt={ds} now has {n} rows (re-run → grows; NOT idempotent)")

    @task
    def overwrite_partition():
        """FIX: idempotent — overwrites the date's partition from a pure function of the interval."""
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        path = _partition_path("overwrite_fixed", ds)
        with open(path, "w") as f:  # overwrite → stable across re-runs
            for r in _rows_for_interval(ds):
                f.write(json.dumps(r) + "\n")
        n = sum(1 for _ in open(path))
        print(f"[overwrite_fixed] dt={ds} has {n} rows (re-run → same; idempotent)")

    append_partition()
    overwrite_partition()
