"""
Spark session factory for spark-dev notebooks.

Usage in notebooks:
    from common.spark_session import spark, display_df

    # spark is ready — Connect, eager eval, sparksql-magic all configured
    spark.sql("SELECT * FROM iceberg_catalog.my_database.orders_iceberg")

    # Scrollable display
    display_df(spark.table("iceberg_catalog.my_database.orders_iceberg"))

Recovering after a driver death (e.g. the OOM modules SPK-2 / SPK-3):
    from common.spark_session import reconnect
    spark = reconnect()      # rebuild the session after NO_ACTIVE_SESSION

A Spark Connect client caches its session; if the server's driver JVM dies and
restarts, the cached handle raises ``[NO_ACTIVE_SESSION]`` and re-importing this
module returns the same stale object (Python caches modules). ``reconnect()`` /
``get_spark()`` rebuild a fresh session without a kernel restart.
"""

import os
from pyspark.sql import SparkSession
from IPython.display import display, HTML


def _tune(session: SparkSession) -> SparkSession:
    """Apply client-side display settings (must be set client-side for Spark Connect)."""
    session.conf.set("spark.sql.repl.eagerEval.enabled", True)
    session.conf.set("spark.sql.repl.eagerEval.maxNumRows", 20)
    session.conf.set("spark.sql.repl.eagerEval.truncate", 50)
    # Spark Connect doesn't set _instantiatedSession; some tools expect it
    SparkSession._instantiatedSession = session
    return session


def _create_spark(force_new: bool = False) -> SparkSession:
    """Create and configure a SparkSession for notebooks.

    Args:
        force_new: for a Connect remote, use ``.create()`` (a brand-new server-side
            session) instead of ``.getOrCreate()`` — needed when the previous session
            died and its cached handle is stale.
    """
    remote = os.environ.get("SPARK_REMOTE", "sc://localhost:15002")

    if remote:
        builder = SparkSession.builder.remote(remote)
        session = builder.create() if force_new else builder.getOrCreate()
    else:
        session = (
            SparkSession.builder
            .appName("Spark-Developer")
            .master("local[*]")
            .getOrCreate()
        )

    return _tune(session)


def reconnect() -> SparkSession:
    """Rebuild the module-level ``spark`` after the driver/session died.

    Use after a session-killing failure (e.g. a driver OOM in SPK-3) instead of
    restarting the Jupyter kernel:

        from common.spark_session import reconnect
        spark = reconnect()      # rebind in your notebook, then carry on
    """
    global spark
    spark = _create_spark(force_new=True)
    print("Reconnected: fresh Spark session created.")
    return spark


def get_spark(probe: bool = True) -> SparkSession:
    """Return a healthy session, transparently reconnecting if the current one is dead.

    With ``probe=True`` (default) it runs a trivial op and rebuilds the session if that
    raises (e.g. ``[NO_ACTIVE_SESSION]`` after a driver OOM). Use this instead of the
    bare module-level ``spark`` in code that might run after a session-killing failure.
    """
    global spark
    if probe:
        try:
            spark.range(1).count()
        except Exception:  # noqa: BLE001 — stale/dead session → rebuild
            return reconnect()
    return spark


def display_df(df, limit=100, height="400px"):
    """Scrollable HTML table display for Spark DataFrames."""
    html = df.limit(limit).toPandas().to_html(index=False)
    display(HTML(f"<div style='max-height:{height}; overflow:auto'>{html}</div>"))


# Pre-built session — just import and use
spark = _create_spark()
