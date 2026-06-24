"""AF-5 — Sensor modes: poke vs reschedule vs deferrable (Break → Detect → Fix → Prove).

**Scenario.** A pipeline often has to *wait* for something external before it can proceed — a file
to land, a partition to be ready, an upstream API to go green. Airflow expresses "wait until X" with
a **sensor**. The subtle, production-critical question is *how* it waits, because a sensor that waits
the wrong way can quietly starve your whole cluster of worker slots.

**The three ways a sensor waits (the slot economics):**
- **poke** (default): the sensor occupies a **worker slot for its entire lifetime**, waking every
  `poke_interval` to re-check. Cheap to reason about, fine for short waits — but a poke sensor that
  waits 6 hours holds a slot for 6 hours doing almost nothing.
- **reschedule** (`mode="reschedule"`): between checks the task **releases its worker slot** and goes
  back to scheduled; Airflow re-queues it each `poke_interval`. The slot is free while it waits, so
  many long sensors no longer pin the pool. Trade-off: slightly more scheduler bookkeeping and the
  re-check granularity is the poke interval.
- **deferrable / async**: the sensor hands its wait off to the **triggerer** process and frees the
  worker slot *entirely* — thousands of deferred sensors can wait on one triggerer. Best for long /
  high-fan-out waits, at the cost of running (and depending on) the triggerer.

**Break.** The naive instinct is "just use the default poke sensor everywhere." With a long wait and
many concurrent sensors that **exhausts the worker pool** — real work can't schedule because slots
are all held by sensors that are merely *waiting*. The classic sensor deadlock.

**Detect.** In the Airflow UI a poke sensor sits in **running** the whole time (slot held); a
reschedule sensor flips between **up_for_reschedule** and running (slot freed in between). Under
`dags test` both finish in one quick poke because the file is already present — so here we *describe*
the slot behavior in the logs and prove the modes coexist and both pass.

**Fix.** Pick the mode for the wait: short → **poke**; long → **reschedule** (or **deferrable** when
you also want to collapse many waiters onto the triggerer). `wait_poke` runs in poke mode and
`wait_reschedule` runs `mode="reschedule"`; both use the same cheap `python_callable` that returns
True as soon as the sentinel file `create_signal` wrote under `.tmp/af5/` exists. Tiny
`poke_interval=2` and `timeout=20` keep `dags test` to seconds.

**Prove.** `airflow dags test af5_sensor_modes 2025-03-01`: `create_signal` writes the file, then
both sensors succeed on their first poke. The poke sensor would have held a worker slot for the
whole wait; the reschedule sensor would have freed its slot between pokes — identical *result*,
very different *slot cost* under load.

**Honesty — deferrable sensors need the triggerer (not run by `dags test`).** A deferrable sensor
yields a `TriggerEvent` to the **triggerer** process; `airflow dags test` runs synchronously with no
scheduler **or triggerer**, so a truly-deferred wait can't be exercised here. We therefore describe
it. The async form of a time wait looks like:

    from airflow.providers.standard.sensors.time_delta import TimeDeltaSensorAsync
    waiter = TimeDeltaSensorAsync(task_id="wait_async", delta=timedelta(minutes=5))
    # Many PythonSensor-style sensors also accept `deferrable=True`, e.g.:
    #   FileSensor(task_id="wait_file", filepath="/data/_SUCCESS", deferrable=True)

Both immediately suspend the task and free the worker slot to the triggerer until the condition
fires — that is the whole point of deferral, and exactly why the triggerer must be running.

Run it:
    airflow dags test af5_sensor_modes 2025-03-01
    # create_signal writes .tmp/af5/_SUCCESS; wait_poke and wait_reschedule both pass on poke #1.

In real production: never default to long poke sensors at scale — use reschedule for long waits and
deferrable for long / high-fan-out waits to keep worker slots free; keep `poke_interval` sane; set a
`timeout` so a never-arriving dependency fails loudly instead of waiting forever.
"""

from __future__ import annotations

import os
from datetime import datetime

from airflow.sdk import DAG, task
from airflow.providers.standard.sensors.python import PythonSensor

# Anchor outputs at the repo's .tmp/ (gitignored; `make clean` recovers). The DAG file lives at
# <repo>/airflow/dags/, so the repo root is two levels up.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(REPO, ".tmp", "af5")
SIGNAL = os.path.join(OUT, "_SUCCESS")


def _signal_ready() -> bool:
    """Cheap, deterministic condition both sensors poll: does the sentinel file exist yet?"""
    ready = os.path.exists(SIGNAL)
    print(f"[sensor poke] checking {SIGNAL} -> {'present, condition met' if ready else 'absent, waiting'}")
    return ready


with DAG(
    dag_id="af5_sensor_modes",
    start_date=datetime(2025, 1, 1),
    schedule=None,
    catchup=False,
    tags=["airflow-curriculum", "AF-5", "sensors", "poke", "reschedule", "deferrable"],
    doc_md=__doc__,
):

    @task
    def create_signal():
        """Prior task that makes the awaited condition true, so both sensors pass on poke #1."""
        os.makedirs(OUT, exist_ok=True)
        with open(SIGNAL, "w") as f:
            f.write("ready\n")
        print(f"[create_signal] wrote {SIGNAL} — the thing the sensors are waiting for now exists")

    # POKE (default): holds a worker slot for its whole lifetime, re-checking every poke_interval.
    # Fine for short waits; at scale, many long poke sensors starve the worker pool.
    wait_poke = PythonSensor(
        task_id="wait_poke",
        python_callable=_signal_ready,
        mode="poke",
        poke_interval=2,
        timeout=20,
    )

    # RESCHEDULE: releases its worker slot between pokes (task goes up_for_reschedule), so the slot
    # is free while it waits. Preferred for long waits — same result, far cheaper under load.
    wait_reschedule = PythonSensor(
        task_id="wait_reschedule",
        python_callable=_signal_ready,
        mode="reschedule",
        poke_interval=2,
        timeout=20,
    )

    @task
    def proceed():
        """Runs only after both sensors clear — proves the wait resolved under `dags test`."""
        print("[proceed] both sensors cleared — poke held a slot throughout; reschedule freed it between pokes")

    # The signal must exist before the sensors poke; both then gate the downstream work.
    sig = create_signal()
    sig >> [wait_poke, wait_reschedule] >> proceed()
