"""AF-3 — Catchup & backfill (Break → Detect → Fix → Prove).

**Scenario.** A daily job goes live with a `start_date` in the past. What should Airflow do about
all the intervals that elapsed *before* the DAG existed? Two mechanisms cover this:

- **Catchup** (`catchup=True`): when the scheduler turns the DAG on, it automatically creates one
  run per *missed* interval between `start_date` and now — replaying history forward, one day at a
  time, until it's caught up.
- **Backfill** (`airflow dags backfill ... -s <start> -e <end>`): an *on-demand* replay of a date
  **range**, run by hand whenever you need to (re)materialize a span — e.g. after fixing a bug in
  the transform, or onboarding a new downstream that needs history.

Both lean on the AF-2 execution model: every replayed run gets the *stable* data interval for its
logical date, so the work is reproducible.

**Break (the trap this DAG is set up to AVOID).** `catchup=True` with an **old** `start_date` (say
`datetime(2024, 1, 1)`) tells the scheduler to instantiate ~hundreds of runs the instant the DAG is
unpaused — a thundering herd that floods the scheduler and can wedge a laptop. The naive instinct
"just set start_date back a year and turn on catchup" is the footgun.

**Detect.** You'd see the run count explode in the UI / `dags list-runs` the moment the DAG is
enabled, with the scheduler saturated launching backlog runs.

**Fix — two parts.**
1. *Bound the window.* Keep `start_date` **recent** (here: `2025-03-01`) so catchup is naturally
   small, and in real labs replay deliberately with `dags test <date>` (one run) or
   `dags backfill -s <start> -e <end>` (an explicit range) rather than unleashing the scheduler on
   an unbounded backlog. `dags test` doesn't touch the scheduler at all.
2. *Make each run idempotent* (ties to AF-1). `write_partition` writes
   `.tmp/af3_catchup/dt=<ds>/data.txt` in **overwrite** mode, keyed on `data_interval_start`. So a
   catchup replay and a manual backfill of the same date converge to the same file — replays never
   collide or double-write.

**Prove.** Run two different logical dates — each lands in its **own** `dt=<ds>` partition; re-run
either date — the file is byte-identical (overwrite, pure function of the interval). Distinct
partitions per interval + idempotent writes = a backfill you can trust.

Run it:
    cd airflow && AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags \\
      uv run airflow dags test af3_catchup_backfill 2025-03-02
    # writes .tmp/af3_catchup/dt=2025-03-02/data.txt ; re-run same date → identical file

Replay a bounded RANGE on demand (the production-safe way, not turning on catchup blindly):
    cd airflow && AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags \\
      uv run airflow dags backfill af3_catchup_backfill -s 2025-03-01 -e 2025-03-03
    # creates one run per day in [2025-03-01 .. 2025-03-03], each in its own dt= partition

In real production: enable `catchup` only with a tight, intentional `start_date`; replay history
with an explicit bounded `backfill` range; and keep every partitioned write idempotent (AF-1) so
catchup and backfill are always safe to repeat.
"""

from __future__ import annotations

import os
from datetime import datetime

from airflow.sdk import DAG, get_current_context, task

# Anchor outputs at the repo's .tmp/ (gitignored; `make clean` recovers). The DAG file lives at
# <repo>/airflow/dags/, so the repo root is two levels up.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(REPO, ".tmp", "af3_catchup")


with DAG(
    dag_id="af3_catchup_backfill",
    # RECENT, bounded start_date — see the docstring: an OLD start_date + catchup=True would flood
    # the scheduler with backlog runs. Recent keeps catchup naturally small and laptop-safe.
    start_date=datetime(2025, 3, 1),
    schedule="@daily",
    # catchup=True so the scheduler replays missed intervals from start_date — but because
    # start_date is recent the backlog is tiny; real labs replay with `dags test`/`dags backfill`.
    catchup=True,
    tags=["airflow-curriculum", "AF-3", "catchup", "backfill", "idempotency"],
    doc_md=__doc__,
):

    @task
    def write_partition():
        """Idempotent per-interval write: one partition keyed on data_interval_start, overwrite mode.

        Catchup and backfill both replay this for distinct logical dates; each lands in its own
        dt=<ds> partition, and re-running a date overwrites (never appends) — so replays converge
        to the same state instead of colliding (ties to AF-1).
        """
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        part_dir = os.path.join(OUT, f"dt={ds}")
        os.makedirs(part_dir, exist_ok=True)
        path = os.path.join(part_dir, "data.txt")
        # Overwrite ("w"), content a pure function of the interval → idempotent across replays.
        with open(path, "w") as f:
            f.write(f"partition dt={ds}\n")
            f.write(f"rows=3 metric=orders value={100 + (hash(ds) % 50)}\n")
        print(f"[af3] wrote partition {path} (overwrite → re-run/backfill is idempotent)")

    write_partition()
