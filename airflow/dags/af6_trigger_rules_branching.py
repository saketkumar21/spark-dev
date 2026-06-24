"""AF-6 — Branching, trigger rules & short-circuit (Break → Detect → Fix → Prove).

**Scenario.** Real pipelines fork: "if the load is a full refresh do X, else do an incremental Y",
"if there's no new data, stop early". Airflow models this with **branching** (`@task.branch` picks
which downstream path to follow; the unchosen path is *skipped*) and **short-circuiting**
(`@task.short_circuit` returning `False` skips everything downstream). The catch: a task that fans
**back in** after a branch — a "join" — uses the default `trigger_rule="all_success"`, which requires
*every* direct upstream to have succeeded. But one upstream was deliberately **skipped**, and under
`all_success` a skipped upstream makes the join skip too. The whole tail of your DAG silently
vanishes. The fix is to give the join the right trigger rule.

**Break.** `pick_branch` (a `@task.branch`) chooses exactly one of `run_full_refresh` /
`run_incremental` based on a value derived *deterministically* from `data_interval_start` (even row
count → full refresh, odd → incremental). The other branch is **skipped**. A naive join wired with
the default `all_success` would see one SUCCESS and one SKIPPED upstream and **skip itself** — the
report never runs.

**Detect.** In `dags test` logs you'll see exactly one of the two branch tasks run and the other marked
`skipped`. If the join had `all_success` it would also show `skipped` (and you'd wrongly conclude the
pipeline "passed" when it actually did nothing).

**Fix.** `join_after_branch` sets `trigger_rule="none_failed_min_one_success"`: it runs as long as no
upstream *failed* and at least one *succeeded* — a skipped sibling is fine. That's the correct rule
for a post-branch join. (Contrast the default `all_success`: ALL upstreams must succeed, so a single
skipped branch sibling would skip the join.)

**Prove.** The logs show: one branch SUCCESS + one branch SKIPPED, and `join_after_branch` still
**succeeds** and prints which branch won. Separately, `gate` (a `@task.short_circuit`) returns `False`
on odd intervals → its downstream `after_gate` is **skipped**; on even intervals it returns `True` →
`after_gate` runs. Branching chooses *which* path; short-circuit chooses *whether* to continue.

Run it:
    cd airflow && AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags \\
      uv run airflow dags test af6_trigger_rules_branching 2025-03-01
    # 2025-03-01 → odd row count → 'run_incremental' runs, 'run_full_refresh' is skipped,
    # the join still succeeds, and the short-circuit gate skips 'after_gate'.

In real production: after any `@task.branch`, give the join a skip-tolerant trigger rule
(`none_failed_min_one_success` / `none_failed`), never the default `all_success` — otherwise one
skipped branch silently kills the rest of the DAG. Use `@task.short_circuit` for "no new data → stop"
gates so empty intervals exit cleanly instead of running no-op work.
"""

from __future__ import annotations

import os
from datetime import datetime

from airflow.sdk import DAG, get_current_context, task

# Anchor any output at the repo's .tmp/ (gitignored; `make clean` recovers). The DAG file lives at
# <repo>/airflow/dags/, so the repo root is two levels up. This module teaches by printing/branching
# and writes nothing, but the anchor is here to match the house pattern.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _row_count_for_interval(ds: str) -> int:
    """Deterministic synthetic 'row count' for this interval — pure function of the date, no now()."""
    return 100 + (sum(ord(c) for c in ds) % 7)  # stable per logical date; parity drives the branch


with DAG(
    dag_id="af6_trigger_rules_branching",
    start_date=datetime(2025, 1, 1),
    schedule=None,
    catchup=False,
    tags=["airflow-curriculum", "AF-6", "branching", "trigger-rules", "short-circuit"],
    doc_md=__doc__,
):

    @task.branch
    def pick_branch() -> str:
        """Choose ONE downstream path; the unchosen task is skipped. Returns the task_id to follow."""
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        rows = _row_count_for_interval(ds)
        chosen = "run_full_refresh" if rows % 2 == 0 else "run_incremental"
        print(f"[branch] dt={ds} synthetic rows={rows} ({'even' if rows % 2 == 0 else 'odd'}) "
              f"→ following '{chosen}', the other branch will be SKIPPED")
        return chosen

    @task
    def run_full_refresh():
        """One arm of the fork (runs only on even intervals)."""
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        print(f"[full_refresh] dt={ds} → rebuilt everything from scratch")
        return "full_refresh"

    @task
    def run_incremental():
        """Other arm of the fork (runs only on odd intervals)."""
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        print(f"[incremental] dt={ds} → merged only this interval's new rows")
        return "incremental"

    @task(trigger_rule="none_failed_min_one_success")
    def join_after_branch():
        """FIX: skip-tolerant join. Runs if nothing FAILED and >=1 upstream SUCCEEDED — so a skipped
        branch sibling is fine. The default all_success would SKIP here (one upstream is skipped)."""
        print("[join] reached the post-branch join via trigger_rule='none_failed_min_one_success'")
        print("[join] with the DEFAULT 'all_success' this task would be SKIPPED, because exactly one "
              "of its two upstream branches is always skipped → the rest of the DAG would vanish")

    @task.short_circuit
    def gate() -> bool:
        """Short-circuit: return False → skip everything downstream (the 'no new data, stop' pattern)."""
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        proceed = _row_count_for_interval(ds) % 2 == 0  # even interval → proceed, odd → short-circuit
        print(f"[gate] dt={ds} returning {proceed} "
              f"→ downstream 'after_gate' will {'RUN' if proceed else 'be SKIPPED'}")
        return proceed

    @task
    def after_gate():
        """Runs only when the short-circuit gate returned True."""
        print("[after_gate] gate was open (True) → continuing work")

    # Branch fan-out → skip-tolerant join.
    branch = pick_branch()
    full = run_full_refresh()
    incr = run_incremental()
    branch >> [full, incr] >> join_after_branch()

    # Independent short-circuit chain.
    gate() >> after_gate()
