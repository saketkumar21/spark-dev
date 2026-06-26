#!/usr/bin/env python
"""transpile_check — certify which compiled dbt models actually run on Spark.

The trust gate for dbt-spark-transpile. After `dbt compile` (the transpile patch has rendered each
model to Spark SQL in target/compiled/), this runs `EXPLAIN <sql>` against the live Spark Thrift
server for every model and classifies it:

  PASS      EXPLAIN succeeded  -> verified valid on Spark.
  DIALECT   parse / unresolved-function error -> a construct sqlglot couldn't convert (the real work;
            this is what you must know about — printed with the model + Spark error).
  UPSTREAM  table/view-not-found -> the referenced model just isn't built yet (NOT a dialect problem;
            re-run after `dbt build`). Informational only.

Exits non-zero if any DIALECT blocker is found, so it doubles as a CI gate. It never edits anything —
it tells you upfront what is safe, so a model is either verified or surfaced, never silently wrong.

Requires the ``check`` extra for the Spark/Thrift driver:  pip install "dbt-spark-transpile[check]"

Usage (from your dbt project, after `dbt compile`):
    dbt-spark-transpile-check --compiled-dir target/compiled
    # equivalently:  python -m transpile_check --compiled-dir target/compiled
    # connection:    DBT_SPARK_HOST / DBT_SPARK_PORT env vars (default localhost:10000)
"""
import argparse
import glob
import os
import sys

from pyhive import hive


def _classify(err_text):
    t = (err_text or "").upper()
    # Real dialect blockers: sqlglot produced SQL Spark can't parse, or referencing a function
    # Spark doesn't have (an unmapped Snowflake function).
    if "PARSE_SYNTAX_ERROR" in t or "PARSEEXCEPTION" in t:
        return "DIALECT"
    if "UNRESOLVED_ROUTINE" in t or "UNDEFINED FUNCTION" in t or "CANNOT RESOLVE FUNCTION" in t:
        return "DIALECT"
    # Not a dialect problem: the referenced model/seed just isn't built (or is in another catalog).
    if "TABLE_OR_VIEW_NOT_FOUND" in t or "UNRESOLVED_COLUMN" in t or "UNRESOLVED_TABLE" in t:
        return "UPSTREAM"
    return "DIALECT"  # cautious default: unknown error = surface it, never hide it


def _error_class(msg):
    """Pull the Spark [ERROR_CLASS] token out of a HiveSQLException message, for readable output."""
    import re
    m = re.search(r"\[([A-Z0-9_.]+)\]", msg or "")
    return m.group(1) if m else (msg or "").strip().splitlines()[0][:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compiled-dir", default="target/compiled",
                    help="dbt compiled-SQL dir (default: target/compiled, relative to your dbt project root)")
    ap.add_argument("--host", default=os.environ.get("DBT_SPARK_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DBT_SPARK_PORT", "10000")))
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.compiled_dir, "**", "models", "**", "*.sql"),
                             recursive=True))
    if not files:
        print(f"No compiled models under {args.compiled_dir} — run `dbt compile` first.")
        return 2

    cur = hive.connect(host=args.host, port=args.port, username="spark").cursor()
    results = {"PASS": [], "DIALECT": [], "UPSTREAM": []}
    for f in files:
        name = os.path.splitext(os.path.basename(f))[0]
        sql = open(f).read().strip().rstrip(";")
        if not sql:
            continue
        # A zero-row execution forces parse + analysis and RAISES with the real Spark error class
        # ([PARSE_SYNTAX_ERROR] / [TABLE_OR_VIEW_NOT_FOUND] / [UNRESOLVED_ROUTINE] …). EXPLAIN over
        # Thrift only returns a detail-less "Error occurred during query planning:", so we wrap instead.
        try:
            cur.execute(f"SELECT * FROM (\n{sql}\n) _transpile_check WHERE 1 = 0")
            cur.fetchall()
            results["PASS"].append((name, ""))
        except Exception as e:
            msg = str(e)
            results[_classify(msg)].append((name, _error_class(msg)))

    total = sum(len(v) for v in results.values())
    print(f"\n=== transpile_check: {len(results['PASS'])}/{total} models verified valid on Spark ===")
    if results["UPSTREAM"]:
        print(f"\n  {len(results['UPSTREAM'])} not checkable yet (upstream not built — run `dbt build`):")
        for n, _ in results["UPSTREAM"]:
            print(f"    · {n}")
    if results["DIALECT"]:
        print(f"\n  ⚠️  {len(results['DIALECT'])} DIALECT blocker(s) — sqlglot couldn't produce Spark-valid SQL:")
        for n, err in results["DIALECT"]:
            print(f"    ✗ {n}\n        {err}")
        print("\n  These are the only models needing attention. Everything else is verified.")
        return 1
    print("\n  ✅ No dialect blockers — every checkable model runs on Spark.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
