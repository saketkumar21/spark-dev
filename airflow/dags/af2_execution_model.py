"""AF-2 — The data-interval execution model (Break → Detect → Fix → Prove).

**Scenario.** Airflow does not run "now"; it runs **intervals**. A scheduled run for the window
`[data_interval_start, data_interval_end)` is responsible for processing *that* slice of time —
yesterday's data, last hour's data — and nothing else. The defining property: for a given logical
date the interval is **stable**. A retry, a manual clear-and-rerun, and a backfill of the same date
all see the *identical* window. This is what makes Airflow runs reproducible and backfillable.

**Break.** `window_from_now` derives its processing window from `datetime.now()` (start-of-today →
end-of-today, computed at execution time). Run it today, retry it tomorrow, backfill it next week →
each execution looks at a *different* window. The output is a function of when the task happened to
run, not of which date it represents. You can't reproduce it and you can't backfill it.

**Detect.** Print the window the task chose. The `now()`-based task's window tracks the wall clock
(re-run on a different day → different dates). The interval-based task's window is pinned to the
logical date no matter when it actually executes.

**Fix.** `window_from_interval` derives its window *purely* from
`get_current_context()["data_interval_start"]` / `["data_interval_end"]`. For
`af2_execution_model 2025-03-01` the window is always `[2025-03-01, 2025-03-02)` — on the first run,
on a retry, on a backfill months later. The output is a pure function of the interval.

**Prove.** Both tasks also print a live `datetime.now()`. Run this DAG twice for the same logical
date and compare the logs: the **interval** (start/end) is byte-for-byte identical across both runs,
while **now()** has drifted forward. Stable interval = reproducible run; drifting now() = the trap.

Run it:
    cd airflow && AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags \\
      uv run airflow dags test af2_execution_model 2025-03-01
    # The printed data_interval is [2025-03-01, 2025-03-02) regardless of today's wall-clock date.

In real production: never read now()/today() to decide *what* to process — derive the window from
the data interval. That single discipline is what makes retries safe, backfills (AF-3) correct, and
partitioned writes idempotent (AF-1).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from airflow.sdk import DAG, get_current_context, task

# Anchor any output at the repo's .tmp/ (gitignored; `make clean` recovers). The DAG file lives at
# <repo>/airflow/dags/, so the repo root is two levels up. This module writes nothing — it teaches
# by printing — but the anchor is here to match the house pattern.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


with DAG(
    dag_id="af2_execution_model",
    start_date=datetime(2025, 1, 1),
    schedule="@daily",
    catchup=False,
    tags=["airflow-curriculum", "AF-2", "execution-model", "data-interval"],
    doc_md=__doc__,
):

    @task
    def window_from_now():
        """BREAK: window derived from now() — drifts with the wall clock, can't reproduce/backfill."""
        now = datetime.now(timezone.utc)
        # Whatever day the task happens to execute becomes the "window" — wrong by design.
        win_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        win_end = win_start.replace(hour=23, minute=59, second=59)
        print(f"[now-based BROKEN] now()={now.isoformat()}")
        print(f"[now-based BROKEN] processing window = [{win_start.date()} .. {win_end.date()}]")
        print("[now-based BROKEN] re-run on a different day → different window → NOT reproducible")

    @task
    def window_from_interval():
        """FIX: window derived purely from the data interval — stable across run/retry/backfill."""
        ctx = get_current_context()
        di_start = ctx["data_interval_start"]  # datetime — start of THIS run's window (inclusive)
        di_end = ctx["data_interval_end"]      # datetime — end of THIS run's window (exclusive)
        now = datetime.now(timezone.utc)
        print(f"[interval-based FIXED] data_interval_start = {di_start.isoformat()}")
        print(f"[interval-based FIXED] data_interval_end   = {di_end.isoformat()}")
        print(f"[interval-based FIXED] live now()          = {now.isoformat()}")
        print(
            "[interval-based FIXED] across two runs of the same logical date the INTERVAL is "
            "identical while now() drifts → reproducible & backfillable"
        )

    window_from_now()
    window_from_interval()
