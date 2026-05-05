# Claude Code — Project Context

## What This Repo Is

A Docker-based local development environment for learning and prototyping with Apache Spark 4.0.2, Iceberg, Delta Lake, Kafka Structured Streaming, and dbt-core. Everything runs locally via Docker Compose.

## Architecture (Critical to Understand)

One Docker container (`spark-connect`) runs a **Spark Thrift Server** (HiveServer2) with the **Spark Connect plugin** enabled in the same JVM. This means:

- **Notebooks** connect via Spark Connect gRPC at `localhost:15002`
- **dbt** connects via Thrift JDBC at `localhost:10000`
- **Both share the same SparkContext** → single Spark UI at `localhost:4040`

This is NOT a standard Spark Connect-only setup. The entrypoint runs `start-thriftserver.sh` with `spark.plugins=org.apache.spark.sql.connect.SparkConnectPlugin`.

## Key Technical Decisions

### JARs are pre-installed in Docker (not resolved at runtime)
- Iceberg, Delta, Kafka JARs are resolved via Ivy in a **multi-stage Docker build** and copied to `$SPARK_HOME/jars/`
- Reason: The Thrift Server has a classloader isolation bug where `spark.jars.packages` JARs aren't accessible from HiveServer2 execution threads
- `spark.jars.packages` is NOT used in spark-defaults.conf — JARs on the system classpath work

### Default catalog is `spark_catalog` (not `iceberg_catalog`)
- dbt creates tables in `spark_catalog` (Delta/Hive managed tables)
- Notebooks use `iceberg_catalog.my_database.xxx` explicitly — they never rely on the default catalog
- This avoids the Iceberg classloader issue with Thrift while keeping Iceberg fully usable from notebooks

### Iceberg namespaces are pre-created as directories
- The Hadoop-based Iceberg catalog stores namespaces as filesystem directories
- `docker-entrypoint.sh` creates `.tmp/local_iceberg_warehouse/{default,analytics,staging,marts,seeds}` on startup
- Without this, Thrift clients get `SCHEMA_NOT_FOUND` on connect (Iceberg doesn't auto-create `default`)

### All runtime data lives in `.tmp/`
- Warehouses: `.tmp/local_iceberg_warehouse/`, `.tmp/local_delta_warehouse/`
- Metastore: `.tmp/metastore/` (Derby, via `derby.system.home` JVM prop)
- Spark warehouse: `.tmp/spark-warehouse/` (via `spark.sql.warehouse.dir`)
- Event logs: `.tmp/spark-events/`
- Streaming checkpoints: `.tmp/checkpoint_*`
- `make clean` = `rm -rf .tmp`

## dbt Setup

### How users run dbt
```bash
cd dbt
source .env        # sets DBT_PROFILES_DIR=. and connection vars
dbt run -s model   # just works
dbt build          # seed + run + test
```

### Connection
- Method: `thrift` (PyHive)
- Host/port from env vars: `DBT_SPARK_HOST`, `DBT_SPARK_PORT`
- Schema: `analytics`
- No authentication (SASL default, no password)

### dbt-spark-qualify
- Local package at `./dbt-spark-qualify/`
- Monkeypatches `SparkConnectionManager.add_query()` to transpile QUALIFY clauses via sqlglot
- Installed via `[tool.uv.sources]` in pyproject.toml
- Uses a `.pth` file for automatic loading before dbt imports

### Schema naming
- `macros/generate_schema_name.sql` overrides dbt's default behavior
- Custom schemas are used directly (e.g., `staging`, `marts`) without prepending the target schema

## File Layout

```
├── Dockerfile              Multi-stage: deps (Ivy JAR resolution) → final image
├── docker-compose.yml      spark-connect, spark-history, kafka, kafka-ui
├── conf/
│   └── spark-defaults.conf Catalogs, extensions, memory, Thrift+Connect config
├── scripts/
│   └── docker-entrypoint.sh  Starts Thrift Server (connect mode) or History Server
├── app/
│   ├── utils/
│   │   ├── spark_session.py   Spark Connect session factory for notebooks
│   │   ├── producer.py        File-based event producer (JSON → streaming_input/)
│   │   └── sales_producer.py  Kafka event producer (→ sales-events topic)
│   ├── data/source/           Static CSVs (customers.csv, orders.csv)
│   └── notebooks/             Jupyter notebooks 01-04
├── dbt/
│   ├── dbt_project.yml        staging=view, marts=table
│   ├── profiles.yml           Thrift connection config
│   ├── seeds/customers.csv    20 customer records
│   ├── models/staging/        stg_customers (view)
│   ├── models/marts/          dim_customers (table, enriched)
│   └── macros/                generate_schema_name override
├── dbt-spark-qualify/         Local package: QUALIFY → CTE transpilation
├── pyproject.toml             uv-managed, Python >=3.13
└── .tmp/                      ALL generated data (gitignored)
```

## Docker Services

| Service | Image | Ports | Purpose |
|---------|-------|-------|---------|
| spark-connect | spark-dev:latest | 10000, 15002, 4040 | Unified Thrift+Connect server |
| spark-history | spark-dev:latest | 18080 | History Server (reads .tmp/spark-events) |
| kafka | apache/kafka:latest | 29092 | KRaft broker (no ZooKeeper) |
| kafka-ui | provectuslabs/kafka-ui | 8080 | Topic browser |

## Common Issues & Fixes

- **"SCHEMA_NOT_FOUND" on Thrift connect**: Iceberg namespaces not created. Check `docker-entrypoint.sh` creates dirs in `.tmp/local_iceberg_warehouse/`
- **NoClassDefFoundError with Iceberg/Delta**: JARs not on system classpath. Must be in `$SPARK_HOME/jars/`, not loaded via `spark.jars.packages`
- **dbt "thrift connection method requires additional dependencies"**: `dbt-spark[PyHive]` extra is missing from pyproject.toml
- **Slow first start**: Should no longer happen — JARs are baked into the Docker image. If Ivy runs at startup, something is wrong with spark-defaults.conf

## Dependency Versions (as of 2026-05-06)

- Spark: 4.0.2 (Scala 2.13, Java 17)
- Iceberg: 1.10.1
- Delta Lake: 4.0.0
- dbt-core: 1.11.0
- dbt-spark: 1.10.1
- Python: 3.13
- Package manager: uv
