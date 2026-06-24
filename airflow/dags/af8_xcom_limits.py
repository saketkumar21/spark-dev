"""AF-8 — XCom for metadata, not payloads (Break → Detect → Fix → Prove).

**Scenario.** Tasks hand off to each other. The question is *what* you hand off. XCom return values
are stored as serialized rows in Airflow's **metadata database** and pulled back out by the consumer.
That makes XCom perfect for small control-plane metadata (a row count, a status, a *path*) and a trap
for actual data. Push a big blob — a multi-MB list, a serialized DataFrame — through XCom and you
bloat the metadata DB, slow every scheduler query that touches that table, and eventually hit the
backend's value-size limit. The rule: **pass references (URIs/paths), not payloads.**

**Break.** `make_big_blob` would `return` the whole dataset (here a list of N dicts) so the next task
receives the bytes *via the metadata DB*. We DESCRIBE this antipattern and only return the blob's
**size** (never the megabytes themselves) so the DAG stays laptop-safe — but the docstring and logs
spell out exactly why returning the real list is the wrong move and what breaks.

**Detect.** XCom values live in the `xcom` table of the metadata DB. A healthy pipeline keeps those
values tiny (bytes-to-kilobytes of metadata). If they balloon, you see it as a growing `xcom` table,
slower scheduler/`dags`-list queries, and — past the backend limit (default-backend values are capped
by the DB column / serialization) — outright failures on push. Watch the size each task reports.

**Fix.** Two good patterns, both in this DAG:
  1. `summarize` returns a small **dict of metadata** — `{"rows": N, "path": "<uri>", "status": "ok"}`
     — and `report_metadata` consumes it directly. Kilobytes at most; exactly what XCom is for.
  2. `write_dataset` WRITES the "large" dataset to a file under ``.tmp/af8/`` and returns only the
     **path/URI**; `read_dataset` receives that string and reads the file itself. The data never
     touches the metadata DB — only a short reference does.

**Prove.** ``dags test`` shows the small dict flowing across tasks via XCom and the path-passing
handoff working end to end. Each task prints the *size* of what it would have pushed vs. what it
actually pushed, so the contrast (payload vs. reference) is visible in the logs.

Run it:
    cd airflow && AIRFLOW_HOME=$PWD/.airflow_home AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags \\
      uv run airflow dags test af8_xcom_limits 2025-03-01
    # Logs show: a small metadata dict consumed downstream, and a dataset handed off by PATH (not value).

In real production: push references, not payloads. Keep XCom for small metadata and write big
intermediates to object storage / a table (S3, Iceberg, Delta), passing only the URI. When handoffs
genuinely must carry more, configure a **custom XCom backend** (e.g. an S3/GCS backend) so values are
offloaded out of the metadata DB rather than stuffed into it.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from airflow.sdk import DAG, get_current_context, task

# Anchor outputs at the repo's .tmp/ (gitignored; `make clean` recovers). The DAG file lives at
# <repo>/airflow/dags/, so the repo root is two levels up.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(REPO, ".tmp", "af8")

# "Large" only by intent — kept tiny so the demo is laptop-safe. In real life this would be the
# multi-MB / multi-GB intermediate you must NOT route through XCom.
N_ROWS = 1000


def _dataset(ds: str) -> list[dict]:
    """Deterministic, pure function of the date — the 'large' dataset we hand off."""
    return [{"dt": ds, "id": i, "value": 100 + (i % 50)} for i in range(N_ROWS)]


with DAG(
    dag_id="af8_xcom_limits",
    start_date=datetime(2025, 1, 1),
    schedule=None,
    catchup=False,
    tags=["airflow-curriculum", "AF-8", "xcom", "metadata-vs-payload"],
    doc_md=__doc__,
):

    @task
    def make_big_blob() -> int:
        """BREAK (described): returning the whole dataset routes the bytes through the metadata DB.

        We do NOT actually push the list — that's the antipattern. We build it, measure how big the
        XCom row *would* be, and return only that size so the lesson is visible without the bloat.
        """
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        blob = _dataset(ds)
        would_push_bytes = len(json.dumps(blob).encode())  # what XCom would store in the metadata DB
        print(
            f"[BROKEN/described] returning this list via XCom would push ~{would_push_bytes:,} bytes "
            f"({len(blob):,} rows) into the metadata DB's `xcom` table"
        )
        print(
            "[BROKEN/described] that bloats the DB, slows scheduler queries, and can exceed the "
            "value-size limit — so we DO NOT do it. Return a reference instead (see write_dataset)."
        )
        return would_push_bytes  # tiny int, not the payload

    @task
    def summarize() -> dict:
        """FIX #1: return a SMALL metadata dict — exactly what XCom is designed to carry."""
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        rows = _dataset(ds)
        meta = {"rows": len(rows), "path": f"repo://af8/dt={ds}", "status": "ok"}
        print(f"[GOOD/metadata] pushing small dict via XCom: {meta} (~{sys.getsizeof(meta)} bytes)")
        return meta

    @task
    def report_metadata(meta: dict) -> None:
        """FIX #1 (consumer): receive the metadata dict straight from XCom and act on it."""
        print(f"[GOOD/metadata] consumed via XCom -> rows={meta['rows']} path={meta['path']} status={meta['status']}")

    @task
    def write_dataset() -> str:
        """FIX #2: write the 'large' dataset to a file and return ONLY its path (a reference)."""
        ds = get_current_context()["data_interval_start"].strftime("%Y-%m-%d")
        d = os.path.join(OUT, f"dt={ds}")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "data.jsonl")
        with open(path, "w") as f:
            for r in _dataset(ds):
                f.write(json.dumps(r) + "\n")
        size = os.path.getsize(path)
        print(
            f"[GOOD/reference] wrote {N_ROWS:,} rows (~{size:,} bytes on disk) and return only the "
            f"PATH via XCom ({len(path)} bytes) — the data never touches the metadata DB"
        )
        return path  # short string reference, not the bytes

    @task
    def read_dataset(path: str) -> None:
        """FIX #2 (consumer): receive the path via XCom and read the data from disk itself."""
        n = sum(1 for _ in open(path))
        print(f"[GOOD/reference] consumed PATH via XCom -> read {n:,} rows from {path}")

    # Wiring: XCom flows by passing one task's return value into the next.
    make_big_blob()                       # described antipattern (returns size only)
    report_metadata(summarize())          # metadata dict over XCom
    read_dataset(write_dataset())         # path/URI over XCom; data stays on disk
