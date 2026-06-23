"""
iceberg_meta (Phase 2) — table-health metrics for the lakehouse track.

`common.metrics_diff` captures *query* performance; this captures *table* health — the
**data-file**, **snapshot**, and **manifest** counts that the lakehouse pathologies drive up
(small files, snapshot growth, manifest explosion) and that maintenance drives back down
(`rewrite_data_files`, `expire_snapshots`, `rewrite_manifests`). It's the "Prove it" for LAK-*.

Connect-safe: reads Iceberg metadata tables via ``spark.sql`` only.

Usage:
    from common.iceberg_meta import table_health, compare_health

    before = table_health(spark, "iceberg_catalog.lake.orders", "before compaction")
    spark.sql("CALL iceberg_catalog.system.rewrite_data_files(table => 'lake.orders')")
    after  = table_health(spark, "iceberg_catalog.lake.orders", "after compaction")
    compare_health([before, after])

For Delta tables use ``DESCRIBE DETAIL <t>`` (numFiles, sizeInBytes) / ``DESCRIBE HISTORY <t>``;
this module is Iceberg-specific.
"""

from __future__ import annotations


def _human(n) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def table_health(spark, table: str, label: str | None = None) -> dict:
    """Snapshot of an Iceberg table's physical health (current snapshot).

    Reads ``<table>.files`` / ``.snapshots`` / ``.manifests``. Returns a dict with
    data-file count, total + average data-file size, and snapshot / manifest counts.
    """
    f = spark.sql(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(file_size_in_bytes), 0) AS sz FROM {table}.files"
    ).first()
    data_files = int(f["n"])
    total = int(f["sz"])
    snapshots = int(spark.sql(f"SELECT COUNT(*) AS n FROM {table}.snapshots").first()["n"])
    manifests = int(spark.sql(f"SELECT COUNT(*) AS n FROM {table}.manifests").first()["n"])
    return {
        "label": label or table,
        "data_files": data_files,
        "total_bytes": total,
        "avg_file_bytes": (total // data_files) if data_files else 0,
        "snapshots": snapshots,
        "manifests": manifests,
    }


_ROWS = [
    ("Data files", "data_files", "int"),
    ("Total size", "total_bytes", "bytes"),
    ("Avg file size", "avg_file_bytes", "bytes"),
    ("Snapshots", "snapshots", "int"),
    ("Manifests", "manifests", "int"),
]


def compare_health(results: list[dict]) -> None:
    """Print a before/after table of :func:`table_health` results (metrics as rows).

    For small-files / snapshot / manifest modules, watch **Data files**, **Snapshots**, and
    **Manifests** fall (and **Avg file size** rise) after maintenance.
    """
    if not results:
        print("compare_health(): pass a list of table_health() dicts.")
        return
    labels = [r.get("label", f"run {i}") for i, r in enumerate(results)]
    name_w = max(len(n) for n, _, _ in _ROWS)
    col_w = [max(len(l), 12) for l in labels]

    def fmt(v, kind):
        if v is None:
            return "—"
        return _human(v) if kind == "bytes" else f"{v:,}"

    def row(cells):
        return f"| {cells[0].ljust(name_w)} | " + " | ".join(
            c.rjust(col_w[i]) for i, c in enumerate(cells[1:])
        ) + " |"

    print(row(["Metric", *labels]))
    print(f"| {'-' * name_w} | " + " | ".join("-" * col_w[i] for i in range(len(labels))) + " |")
    for name, key, kind in _ROWS:
        print(row([name] + [fmt(r.get(key), kind) for r in results]))
