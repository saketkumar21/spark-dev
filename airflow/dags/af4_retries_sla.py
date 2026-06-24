"""AF-4 — Retries, exponential backoff & deadline alerting (Break → Detect → Fix → Prove).

**Scenario.** Tasks fail for transient reasons — a flaky network call, a momentarily busy
warehouse, a brief lock. The pipeline shouldn't page a human for a blip it could have shrugged off
on a second attempt. Airflow's answer is **automatic retries with backoff**: re-run the task a few
times, spacing the attempts further apart so a struggling downstream gets room to recover, and only
*then* escalate. The companion concern is **lateness**: a task that "succeeds" four hours late can be
as damaging as one that fails, so you also want to alert when a run blows past its deadline.

**Break.** `flaky_extract` raises on its first two attempts (it inspects
`get_current_context()["ti"].try_number` and fails while it is `<= 2`). With **no** retry policy that
single transient blip would fail the task and the whole DAG — a human gets paged for something that
would have worked on the next try.

**Detect.** Watch the attempt counter in the logs. Each attempt prints
`try_number = 1 / 2 / 3`; attempts 1 and 2 raise, and the `on_retry_callback` fires between them
("ALERT: retrying"). Without retries you would see attempt 1 fail and stop there.

**Fix.** `default_args` gives every task a retry policy:
`retries=3, retry_delay=1s, retry_exponential_backoff=True, max_retry_delay=10s`. Now the two
transient failures are absorbed automatically and the **3rd attempt succeeds** — no page, no manual
clear-and-rerun. Exponential backoff means the gap roughly *doubles* each time (≈1s → 2s → 4s …,
capped at `max_retry_delay`), so a flapping dependency isn't hammered on a tight loop.

**Prove.** `airflow dags test af4_retries_sla 2025-03-01` runs synchronously and the logs show
attempt 1 FAIL → attempt 2 FAIL → attempt 3 SUCCESS, with the retry callback firing twice. The task
ends green despite two failures — the retry policy did its job, and the whole DAG run is `success`.
The **terminal case** — a task that never recovers, exhausts all 3 retries, and fires
`on_failure_callback` once ("ALERT: task failed after all retries") — is *described* here rather than
wired live, so this lab stays green; add an always-raising task to watch it end `failed`.

**Honesty — SLA is gone in Airflow 3; use Deadline Alerting.** The old `sla=timedelta(...)` task
param and the `sla_miss_callback` were **removed** in Airflow 3. The replacement is **Deadline
Alerting**, configured on the DAG via a `DeadlineAlert` with a reference point, an offset, and a
callback that fires when the deadline is crossed:

    from airflow.sdk.definitions.deadline import DeadlineAlert, DeadlineReference
    # ... on the DAG:
    deadline=DeadlineAlert(
        reference=DeadlineReference.DAGRUN_LOGICAL_DATE,  # anchor (or DAGRUN_QUEUED_AT)
        interval=timedelta(minutes=30),                  # alert if not done within 30m
        callback=alert_on_deadline,                      # your notifier
    )

We **describe** it here rather than wiring it live because the deadline is evaluated by the
scheduler against real elapsed wall-clock time — `dags test` runs synchronously in seconds and never
crosses a meaningful deadline, so a live deadline wouldn't actually fire under the test harness.
The retry/backoff behavior below, by contrast, *is* fully exercised by `dags test`.

Run it:
    airflow dags test af4_retries_sla 2025-03-01
    # logs: flaky_extract attempt 1 FAIL → 2 FAIL → 3 SUCCESS; on_retry_callback fires twice.

In real production: set conservative retries with exponential backoff on transient-failure-prone
tasks; keep them idempotent (AF-1) so a retry can't double-write; reserve alerts for *terminal*
failure and *deadline* misses, not every blip — alert fatigue is how real incidents get ignored.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.sdk import DAG, get_current_context, task

# Anchor any output at the repo's .tmp/ (gitignored; `make clean` recovers). The DAG file lives at
# <repo>/airflow/dags/, so the repo root is two levels up. This module writes nothing — it teaches
# by printing — but the anchor is here to match the house pattern.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def alert_on_retry(context) -> None:
    """Fires between attempts (on_retry_callback). In prod this would notify Slack/PagerDuty."""
    ti = context["ti"]
    print(
        f"[ALERT: retrying] task={ti.task_id} just failed attempt {ti.try_number} "
        f"— Airflow will back off and try again"
    )


def alert_on_failure(context) -> None:
    """Fires only after retries are exhausted (on_failure_callback) — the terminal escalation."""
    ti = context["ti"]
    print(
        f"[ALERT: task failed after all retries] task={ti.task_id} exhausted its retry budget "
        f"on attempt {ti.try_number} — a human should look"
    )


# Every task in this DAG inherits this retry policy. Exponential backoff roughly doubles the gap
# each attempt (≈1s → 2s → 4s …) and is capped at max_retry_delay so a flapping dependency isn't
# pounded on a tight loop. Delays are tiny here so `dags test` finishes in seconds.
default_args = {
    "retries": 3,
    "retry_delay": timedelta(seconds=1),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(seconds=10),
    "on_retry_callback": alert_on_retry,
    "on_failure_callback": alert_on_failure,
}


with DAG(
    dag_id="af4_retries_sla",
    start_date=datetime(2025, 1, 1),
    schedule=None,
    catchup=False,
    default_args=default_args,
    tags=["airflow-curriculum", "AF-4", "retries", "backoff", "deadline-alerting"],
    doc_md=__doc__,
    # Honesty: Airflow 3 removed `sla=`/`sla_miss_callback`. Deadline Alerting is the replacement —
    # see the module docstring for a `DeadlineAlert(...)` snippet. It is evaluated against real
    # elapsed time by the scheduler, so it is described (not wired live) because `dags test` runs
    # synchronously in seconds and would never cross the deadline.
):

    @task
    def flaky_extract():
        """FIX in action: transient failure on attempts 1-2, success on 3 — retries absorb it."""
        ti = get_current_context()["ti"]
        attempt = ti.try_number  # 1 on the first run, 2 on the first retry, 3 on the second retry
        print(f"[flaky_extract] try_number = {attempt}")
        if attempt <= 2:
            # Simulate a transient blip (flaky network / busy warehouse). Raising here triggers a
            # retry; on_retry_callback fires, then Airflow waits (exponential backoff) and re-runs.
            raise RuntimeError(
                f"transient failure on attempt {attempt} (will be retried with backoff)"
            )
        print(f"[flaky_extract] attempt {attempt} SUCCEEDED — two transient failures absorbed by retries")

    @task
    def downstream_load():
        """Proves the happy path: only runs because flaky_extract eventually went green."""
        print("[downstream_load] running — upstream recovered via retries, so the DAG continues")

    # Happy path: the flaky extract recovers on attempt 3, so the load runs and the DAG goes green.
    # (Terminal escalation — a task that exhausts its retry budget and fires on_failure_callback —
    # is *described* in the docstring rather than wired live, so this lab stays green under
    # `dags test`. To see it, add a task that always raises; it ends `failed` after 3 retries and
    # `alert_on_failure` fires once.)
    flaky_extract() >> downstream_load()
