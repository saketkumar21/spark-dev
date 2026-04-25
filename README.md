# Spark Dev — Learning Repo

A self-contained Docker environment for learning **Apache Spark**, **Iceberg**, **Delta Lake**, and **Kafka Structured Streaming**.  
Clone → start → open Jupyter → learn.

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (includes Docker Compose)
- That's it — no Python, Java, or Kafka installation required

---

## Quick Start

```bash
# 1. Clone the repo
git clone <repo-url> spark-dev && cd spark-dev

# 2. Build and start all services (first run downloads ~2 GB of images/jars)
docker compose up --build

# 3. Open JupyterLab in your browser
open http://localhost:8888
```

To stop everything:
```bash
docker compose down
```

---

## Services & Ports

| Service | URL | Description |
|---------|-----|-------------|
| JupyterLab | http://localhost:8888 | Notebooks (no token/password) |
| Spark UI | http://localhost:4040 | Active SparkSession metrics |
| Spark UI (2nd app) | http://localhost:4041 | When two sessions are running |
| Kafka UI | http://localhost:8080 | Topic browser, message inspector |
| Kafka broker | localhost:29092 | Bootstrap server for producers |

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  docker compose                                 │
│                                                 │
│  ┌───────────────┐    ┌──────────────────────┐  │
│  │  kafka        │    │  jupyter-spark       │  │
│  │  bitnami/     │◄───│  Spark 4.0.2 +       │  │
│  │  kafka:3.9    │    │  JupyterLab 4        │  │
│  │  :9092 (int.) │    │  :8888  :4040  18080 │  │
│  │  :29092 (ext.)│    └──────────────────────┘  │
│  └───────┬───────┘                              │
│          │                                      │
│  ┌───────▼───────┐                              │
│  │  kafka-ui     │                              │
│  │  :8080        │                              │
│  └───────────────┘                              │
└─────────────────────────────────────────────────┘
         ▲
    localhost:29092  ←  producers running on host
```

**Two Kafka listeners:**
- `kafka:9092` — used by Spark notebooks inside the Docker network
- `localhost:29092` — used by producer scripts running on your laptop

---

## Notebooks

| File | Producer | Transport | Topic |
|------|----------|-----------|-------|
| [01_setup_tables.ipynb](01_setup_tables.ipynb) | — | — | Load CSV into Iceberg, Delta Lake, Parquet |
| [02_streaming_to_iceberg.ipynb](02_streaming_to_iceberg.ipynb) | `producer.py` | **File** (`./data/streaming_input/`) | File-based Structured Streaming → Iceberg |
| [03_query_iceberg.ipynb](03_query_iceberg.ipynb) | — | — | Time travel, snapshots, schema evolution |
| [04_sales_streaming_to_iceberg.ipynb](04_sales_streaming_to_iceberg.ipynb) | `sales_producer.py` | **Kafka** (`sales-events`) | Kafka stream-static join (customer enrichment) → Iceberg |

Run notebooks in order: **01 → 02 → 04 → 03**.

---

## Running the Producers

### `producer.py` — file-based (notebook 02)

Writes 2-row JSONL files to `./data/streaming_input/` every 10 s.  
No Kafka required.

```bash
uv run python producer.py
```

### `sales_producer.py` — Kafka-based (notebook 04)

Publishes sale events to Kafka topic `sales-events` every 10 s.  
Requires Kafka running first (see [start-kafka.sh](start-kafka.sh) or `docker compose up`).

```bash
# host machine (Kafka on localhost:9092 after start-kafka.sh)
uv run python sales_producer.py

# or override broker address
KAFKA_BOOTSTRAP_SERVERS=localhost:9092 uv run python sales_producer.py

# inside Docker (broker reachable as kafka:9092)
KAFKA_BOOTSTRAP_SERVERS=kafka:9092 uv run python sales_producer.py
```

---

## Spark Catalogs

| Catalog | Format | Warehouse path |
|---------|--------|----------------|
| `iceberg_catalog` | Apache Iceberg | `.tmp/local_iceberg_warehouse` |
| `spark_catalog` | Delta Lake | `.tmp/local_delta_warehouse` |

`iceberg_catalog` is the default. Example:
```sql
-- Iceberg (default catalog)
SELECT * FROM my_database.user_events;

-- Delta (explicit catalog)
SELECT * FROM spark_catalog.default.orders_delta;
```

---

## Local Development (without Docker)

If you prefer running Spark locally:

```bash
uv sync
./start.sh      # starts Spark History Server + JupyterLab
```

Set `KAFKA_BOOTSTRAP_SERVERS=localhost:29092` in a `.env` file if Kafka is running locally.

---

## Project Structure

```
spark-dev/
├── docker-compose.yml          # Kafka + Kafka UI + JupyterLab services
├── DockerFile                  # Custom Spark + JupyterLab image
├── spark_conf/
│   └── spark-defaults.conf     # Iceberg, Delta, Kafka packages + catalog config
├── data/
│   └── source/
│       └── customers.csv       # Static reference data (20 customers)
├── producer.py                 # Kafka producer → user-events
├── sales_producer.py           # Kafka producer → sales-events
├── 01_setup_tables.ipynb
├── 02_streaming_to_iceberg.ipynb
├── 03_query_iceberg.ipynb
└── 04_sales_streaming_to_iceberg.ipynb
```
