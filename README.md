# Spark Dev — Learning Repo

A Docker-based environment for learning **Apache Spark**, **Iceberg**, **Delta Lake**, and **Kafka Structured Streaming** — with a unified **Spark Connect Server** so every notebook shares one Spark UI.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  docker compose                                         │
│                                                         │
│  ┌──────────────────┐   ┌────────────────────────────┐  │
│  │  kafka            │   │  spark-connect             │  │
│  │  bitnami/kafka:3.9│◄──│  Spark 4.0.2 Connect Server│  │
│  │  :9092 (internal) │   │  :15002 (gRPC)             │  │
│  │  :29092 (external)│   │  :4040  (Spark UI)         │  │
│  └────────┬──────────┘   └────────────────────────────┘  │
│           │                                              │
│  ┌────────▼──────────┐   ┌────────────────────────────┐  │
│  │  kafka-ui          │   │  spark-history             │  │
│  │  :8080             │   │  :18080                    │  │
│  └───────────────────┘   └────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
         ▲                          ▲
    localhost:29092            sc://localhost:15002
    (producers on host)       (notebooks on host)
```

**Key design:** JupyterLab runs **locally on your machine** (not in Docker).  
Notebooks connect to the Spark Connect Server via `sc://localhost:15002`.  
All Spark jobs appear in **one unified Spark UI** at `http://localhost:4040`.

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

# 3. Start Docker services (Spark Connect + Kafka + History Server)
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
| Spark UI | http://localhost:4040 | Unified DAG view for all notebooks |
| History Server | http://localhost:18080 | Completed Spark applications |
| Kafka UI | http://localhost:8080 | Topic browser, message inspector |
| Kafka broker | http://localhost:29092 | Bootstrap server for producers |
| JupyterLab | http://localhost:8888 | Local notebook server |

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
make clean            # Remove generated data
make clean-all        # Remove data + Docker volumes
```

---

## Configuration

All configuration is in three places:

| File | Purpose |
|------|---------|
| `.env` | Environment variables (ports, Spark remote URL, Kafka address) |
| `conf/spark-defaults.conf` | Spark server config (catalogs, memory, packages) |
| `conf/log4j2.properties` | Logging levels |

### Spark Catalogs

| Catalog | Format | Warehouse path |
|---------|--------|----------------|
| `iceberg_catalog` (default) | Apache Iceberg | `.tmp/local_iceberg_warehouse` |
| `spark_catalog` | Delta Lake | `.tmp/local_delta_warehouse` |

---

## Project Structure

```
spark-dev/
├── docker-compose.yml          # Docker services
├── Dockerfile                  # Spark Connect Server image
├── Makefile                    # Dev workflow commands
├── .env                        # Environment variables
├── .env.example                # Template for .env
├── conf/
│   ├── spark-defaults.conf     # Spark config (catalogs, packages, memory)
│   └── log4j2.properties       # Logging config
├── scripts/
│   └── docker-entrypoint.sh    # Container entrypoint (connect/history modes)
├── start-local.sh              # Start local JupyterLab
├── app/
│   ├── utils/
│   │   ├── spark_session.py    # Spark session helper (Connect/local)
│   │   ├── producer.py         # File-based event producer
│   │   └── sales_producer.py   # Kafka sales event producer
│   ├── data/
│   │   └── source/             # Static reference data (CSV)
│   └── notebooks/              # Jupyter notebooks (01–04)
├── pyproject.toml              # Python dependencies
└── .tmp/                       # Generated data (gitignored)
```

---

## How Spark Connect Works

Instead of each notebook starting its own Spark instance:

```
Before:  Notebook 1 → local Spark (port 4040)
         Notebook 2 → local Spark (port 4041)
         Notebook 3 → local Spark (port 4042)

After:   Notebook 1 ─┐
         Notebook 2 ──┼── sc://localhost:15002 → Spark Connect Server (port 4040)
         Notebook 3 ─┘
```

All notebooks share one Spark engine. One Spark UI shows all DAGs.

---

## Troubleshooting

**Spark Connect not starting?**
```bash
docker compose logs spark-connect    # Check logs
make restart                         # Restart everything
```

**First startup is slow?**
The first `make up` downloads Spark JARs (Iceberg, Delta, Kafka). These are cached in a Docker volume (`spark-dev-ivy-cache`) for subsequent starts.

**Port conflict?**
Edit `.env` to change any port, then `make restart`.
