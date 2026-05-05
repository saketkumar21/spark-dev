import os
import re
import sqlglot
from dbt.adapters.spark.connections import SparkConnectionManager

original_add_query = SparkConnectionManager.add_query

PATCHED_RUN_DIR = os.path.join(os.getcwd(), "target", "patched_run")


def _write_patched_sql(sql):
    """Write patched SQL mirroring dbt's target/run/ folder structure, formatted for readability."""
    match = re.search(r'"node_id":\s*"model\.([^"]+)"', sql)
    if match:
        node_id = match.group(1)
        # e.g. spark_dev.agg_customers -> spark_dev/models/marts/agg_customers.sql
        # Derive path from the run/ folder by finding the matching file
        parts = node_id.split(".")
        project = parts[0] if len(parts) > 1 else "unknown"
        model_name = parts[-1] if parts else "unknown"

        # Search target/run/ for the matching file to replicate its path
        run_dir = os.path.join(os.getcwd(), "target", "run")
        rel_path = None
        if os.path.isdir(run_dir):
            for root, _, files in os.walk(run_dir):
                if model_name + ".sql" in files:
                    rel_path = os.path.relpath(
                        os.path.join(root, model_name + ".sql"), run_dir
                    )
                    break

        if rel_path:
            filepath = os.path.join(PATCHED_RUN_DIR, rel_path)
        else:
            filepath = os.path.join(PATCHED_RUN_DIR, project, model_name + ".sql")
    else:
        filepath = os.path.join(PATCHED_RUN_DIR, "unknown.sql")

    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    # Format SQL for readability
    formatted = sqlglot.transpile(sql, read="spark", write="spark", pretty=True)[0]
    with open(filepath, "w") as f:
        f.write(formatted + "\n")


def patched_add_query(self, sql, auto_begin=True, bindings=None, abridge_sql_log=False):
    if "QUALIFY" in sql.upper():
        try:
            sql = sqlglot.transpile(sql, read="snowflake", write="spark")[0]
            _write_patched_sql(sql)
        except Exception:
            pass  # Silent fallback
    return original_add_query(self, sql, auto_begin, bindings, abridge_sql_log)

SparkConnectionManager.add_query = patched_add_query