# Spark Dev — Learning Repo

A Docker-based environment for **Apache Spark**, **Iceberg**, **Delta Lake**, **Kafka Structured Streaming**, and **dbt** — with a unified Spark server so notebooks and dbt jobs share one Spark UI.

---

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│  docker compose                                               │
│                                                               │
│  ┌──────────────────┐   ┌──────────────────────────────────┐  │
│  │  kafka            │   │  spark-connect                   │  │
│  │  apache/kafka     │◄──│  Spark 4.0.2 Unified Server     │  │
│  │  :9092 (internal) │   │  :10000 (Thrift JDBC — dbt)     │  │
│  │  :29092 (external)│   │  :15002 (Connect gRPC — notebooks)│ │
│  └────────┬──────────┘   │  :4040  (Spark UI)              │  │
│           │              └──────────────────────────────────┘  │
│  ┌────────▼──────────┐   ┌──────────────────────────────────┐  │
│  │  kafka-ui          │   │  spark-history                   │  │
│  │  :8080             │   │  :18080                          │  │
│  └───────────────────┘   └──────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
         ▲                          ▲                ▲
    localhost:29092         sc://localhost:15002   jdbc:hive2://localhost:10000
    (producers)            (notebooks)            (dbt)
```

**Key design:**
- Notebooks connect via **Spark Connect** (`sc://localhost:15002`)
- dbt connects via **Thrift Server** (`jdbc:hive2://localhost:10000`)
- Both share the **same SparkContext** — all jobs appear in one Spark UI at `http://localhost:4040`

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [uv](https://docs.astral.sh/uv/) (Python package manager)

---

## Quick Start

```bash
# 1. Clone and enter
git clone <repo-url> spark-dev && cd spark-dev

# 2. Install Python dependencies locally
uv sync

# 3. Start Docker services (Spark + Kafka + History Server)
make up

# 4. Start JupyterLab locally
make jupyter

# 5. Open http://localhost:8888 and run notebooks
```

---

## Services & Ports

| Service | URL | Description |
|---------|-----|-------------|
| Spark Connect | `sc://localhost:15002` | gRPC endpoint for notebooks |
| Spark Thrift | `jdbc:hive2://localhost:10000` | JDBC endpoint for dbt |
| Spark UI | http://localhost:4040 | Unified DAG view for all jobs |
| History Server | http://localhost:18080 | Completed Spark applications |
| Kafka UI | http://localhost:8080 | Topic browser, message inspector |
| Kafka broker | http://localhost:29092 | Bootstrap server for producers |
| JupyterLab | http://localhost:8888 | Local notebook server |

---

## dbt

dbt-core is integrated via the Spark Thrift Server. All dbt jobs appear in the same Spark UI alongside notebook jobs.

### Usage

```bash
cd dbt
source .env
dbt run -s stg_customers       # run a single model
dbt build                      # seed + run + test (full pipeline)
dbt test -s dim_customers      # test a specific model
```

### Project Structure

```
dbt/
├── dbt_project.yml            # Project config
├── profiles.yml               # Connection config (Thrift → localhost:10000)
├── .env                       # Source this for direct dbt usage
├── seeds/
│   └── customers.csv          # Raw customer data
├── models/
│   ├── staging/
│   │   ├── stg_customers.sql  # Cleaned + typed customer data
│   │   └── _staging__models.yml
│   └── marts/
│       ├── dim_customers.sql  # Customer dimension (regions, tiers, tenure)
│       └── _marts__models.yml
└── macros/
    └── generate_schema_name.sql
```

### Models

| Model | Layer | Materialized | Description |
|-------|-------|--------------|-------------|
| `stg_customers` | staging | view | Cleaned customer data with typed dates and tenure |
| `dim_customers` | marts | table | Enriched with region, tier rank, tenure segment |

---

## Notebooks

Run notebooks in order: **01 → 02 → 04 → 03**.

| File | Producer | Description |
|------|----------|-------------|
| `01_setup_tables` | — | Load CSV into Iceberg, Delta Lake, Parquet |
| `02_streaming_to_iceberg` | `make producer` | File-based Structured Streaming → Iceberg |
| `03_query_iceberg` | — | Time travel, snapshots, cross-table analysis |
| `04_sales_streaming_to_iceberg` | `make sales-producer` | Kafka stream + customer enrichment → Iceberg |

---

## Make Targets

```bash
make help             # Show all commands
make up               # Start Docker services
make down             # Stop Docker services
make restart          # Restart everything
make logs             # Tail service logs
make status           # Show service status
make jupyter          # Start local JupyterLab
make producer         # Start file-based event producer
make sales-producer   # Start Kafka sales event producer
make dbt-build        # Run full dbt pipeline (seed + run + test)
make dbt-debug        # Verify dbt connection
make clean            # Remove generated data
make clean-all        # Remove data + Docker volumes
```

---

## Configuration

| File | Purpose |
|------|---------|
| `.env` | Ports, Spark remote URL, Kafka address, dbt connection vars |
| `conf/spark-defaults.conf` | Spark server config (catalogs, memory, extensions) |
| `conf/log4j2.properties` | Logging levels |
| `dbt/profiles.yml` | dbt connection config (uses env vars from `dbt/.env`) |

### Spark Catalogs

| Catalog | Format | Default | Warehouse path |
|---------|--------|---------|----------------|
| `spark_catalog` | Delta Lake / Hive | Yes (dbt) | `.tmp/local_delta_warehouse` |
| `iceberg_catalog` | Apache Iceberg | Notebooks use explicitly | `.tmp/local_iceberg_warehouse` |

---

## Project Structure

```
spark-dev/
├── docker-compose.yml          # Docker services
├── Dockerfile                  # Spark Unified Server image
├── Makefile                    # Dev workflow commands
├── .env                        # Environment variables
├── .env.example                # Template for .env
├── conf/
│   ├── spark-defaults.conf     # Spark config (catalogs, extensions, memory)
│   └── log4j2.properties       # Logging config
├── scripts/
│   └── docker-entrypoint.sh    # Container entrypoint (Thrift+Connect / history)
├── app/
│   ├── utils/
│   │   ├── spark_session.py    # Spark session helper (Connect/local)
│   │   ├── producer.py         # File-based event producer
│   │   └── sales_producer.py   # Kafka sales event producer
│   ├── data/
│   │   └── source/             # Static reference data (CSV)
│   └── notebooks/              # Jupyter notebooks (01–04)
├── dbt/                        # dbt project (models, seeds, tests)
├── pyproject.toml              # Python dependencies
└── .tmp/                       # Generated data (gitignored)
```

---

## How the Unified Server Works

The Docker container runs **one Spark application** that exposes two interfaces:

```
dbt ─────────── Thrift (JDBC :10000) ──┐
                                        ├── Same SparkContext → Spark UI :4040
Notebooks ───── Connect (gRPC :15002) ─┘
```

This is achieved by starting the Spark **Thrift Server** (`HiveServer2`) with the **Spark Connect plugin** enabled in the same JVM. JARs (Iceberg, Delta, Kafka) are pre-installed in the Docker image for fast startup and classloader compatibility.

---

## Troubleshooting

**Spark not starting?**
```bash
docker compose logs spark-connect    # Check logs
make restart                         # Restart everything
```

**dbt can't connect?**
```bash
# Verify the Thrift port is open
nc -z localhost 10000 && echo OK

# Check dbt config
cd dbt && source .env && dbt debug
```

**Port conflict?**
Edit `.env` to change any port, then `make restart`.
