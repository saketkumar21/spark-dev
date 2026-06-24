# CAP-3 — Observability (optional, opt-in)

> **Status: the metrics pillar is BUILT & VERIFIED; the rest is a documented design appendix.**
> An opt-in **`make monitoring-up`** profile (Prometheus + Grafana + `kafka-exporter` +
> `postgres-exporter`, plus Spark's `PrometheusServlet`) is wired in and was verified end-to-end —
> all 5 Prometheus targets scrape **UP** (prometheus, spark, spark-executors, kafka, postgres),
> with the **CDC-5** replication-slot signal and **KAF-1/2** consumer-lag/offsets live. It is **not**
> part of `make up` — observability adds ~1 GB and the curriculum's promise is a responsive laptop,
> so it's an explicit add-on. The heavier integrations below (Connect-JMX, Airflow OTel, dbt
> Elementary, OpenLineage/Marquez lineage) remain **described, not built** — start points to iterate
> on (genuinely trial-and-error per stack).

## Quick start (the built part)

```bash
make up            # base stack (Kafka + Spark with PrometheusServlet enabled)
make cdc-up        # so postgres-exporter has a database (the CDC-5 slot signal)
make monitoring-up # Prometheus :9090 + Grafana :3000 + exporters
```
- **Prometheus** http://localhost:9090 → Status → Targets (all 5 UP).
- **Grafana** http://localhost:3000 (admin/admin, anonymous read enabled; Prometheus datasource
  pre-provisioned). Import dashboards by ID: **Kafka 7589**, **Postgres 9628**, or build panels on
  `kafka_consumergroup_lag`, `pg_replication_slots_*`, Spark `metrics_*driver*`.
- `make monitoring-down` stops just these services. Config: [`conf/prometheus.yml`](../conf/prometheus.yml),
  [`conf/metrics.properties`](../conf/metrics.properties), [`conf/grafana/provisioning/`](../conf/grafana/provisioning/).
- Spark metrics need the server started with `spark.ui.prometheus.enabled` (already in
  [`conf/spark-defaults.conf`](../conf/spark-defaults.conf)); a pre-existing server needs `make restart`.

There is **no single framework** that covers Spark + Kafka + Debezium + Airflow + dbt. The proven,
vendor-neutral approach is two OSS pillars, each of which every one of these tools already integrates
with:

1. **Metrics & dashboards → Prometheus + Grafana** (the de-facto OSS metrics stack).
2. **Data lineage → OpenLineage + Marquez** (the LF AI & Data lineage standard + its reference UI).

Both run locally in Docker and are the closest thing to an "already tried-and-tested framework for
all these services."

---

## Pillar 1 — Metrics: Prometheus + Grafana

Each service exposes metrics; Prometheus scrapes them; Grafana dashboards visualize them. Per-service
exporters (all OSS, all standard):

| Service | How it exposes metrics (OSS) | Notable signals (tie-in) |
|---------|------------------------------|--------------------------|
| **Spark** | Built-in **`PrometheusServlet`** (Spark 3.0+): set `spark.ui.prometheus.enabled=true` and a `metrics.properties` `*.sink.prometheusServlet` — scrape `:4040/metrics/prometheus` + `/metrics/executors/prometheus`. (Older: Graphite/JMX sink.) For streaming, a `StreamingQueryListener`. | task time, shuffle, GC, executor memory, spill — the SPK-* signals |
| **Kafka broker** | **`jmx_exporter`** (Prometheus JMX exporter agent jar) on the broker JVM → `/metrics`. Mature Grafana dashboards exist (Strimzi/Confluent community). | under-replicated partitions, request latency, bytes in/out |
| **Kafka Connect / Debezium** | Connect JMX → `jmx_exporter`. Debezium publishes connector MBeans (`debezium.postgres:type=connector-metrics`): `MilliSecondsBehindSource`, `SnapshotCompleted`, `NumberOfEventsSeen`, queue sizes. | CDC lag, snapshot progress — the CDC-* signals |
| **Postgres** | **`postgres_exporter`** (prometheus-community). | **replication-slot retained WAL / lag** — exactly the CDC-5 pathology; connections; tx age |
| **Airflow** | Native **StatsD** metrics → **`statsd_exporter`** → Prometheus; or Airflow 3's **OpenTelemetry** metrics (`[metrics] otel_on=True`) → an OTel Collector → Prometheus. | DAG/task duration, failures, scheduler health, pool slots — AF-* signals |
| **dbt** | dbt has no live metrics — it emits **artifacts** (`run_results.json`, `manifest.json`) per run. Use **Elementary** (`elementary-data`, dbt-native: test results, freshness, anomaly detection + a report/dashboard) or **re_data**; or a tiny exporter that pushes `run_results` timings to Prometheus. | model run times, test pass/fail, freshness — DBT-* signals |

**How you'd add it (sketch — a `monitoring` compose profile, opt-in like `cdc`):**

```yaml
# docker-compose.yml — services behind  profiles: ["monitoring"]
prometheus:   { image: prom/prometheus,            ports: ["9090:9090"], volumes: ["./conf/prometheus.yml:/etc/prometheus/prometheus.yml"] }
grafana:      { image: grafana/grafana,            ports: ["3000:3000"] }
postgres-exporter: { image: quay.io/prometheuscommunity/postgres-exporter, profiles: ["monitoring","cdc"] }
# kafka / kafka-connect: add the jmx_exporter agent jar via KAFKA_OPTS=-javaagent:...
# spark: set spark.ui.prometheus.enabled=true + a prometheusServlet sink in conf/metrics.properties
# airflow: AIRFLOW__METRICS__STATSD_ON=true + a statsd_exporter container
```
Then `make monitoring-up` (a `--profile monitoring` target) and import community Grafana dashboards.
The single highest-value, lowest-effort piece for *this* repo is **`postgres_exporter`** — it turns
the CDC-5 "slot retains WAL → disk fills" lab into a live Grafana gauge with almost no wiring.

---

## Pillar 2 — Lineage: OpenLineage + Marquez

**OpenLineage** is an open spec for emitting run/dataset/job lineage events; **Marquez** is its
reference metadata server + web UI. Every tool in this repo has a first-class integration, so you get
**one lineage graph across Spark, Airflow, and dbt**:

| Tool | OpenLineage integration |
|------|-------------------------|
| **Airflow** | the native **`apache-airflow-providers-openlineage`** provider — emits events per task automatically |
| **Spark** | the **`openlineage-spark`** listener jar: `spark.extraListeners=io.openlineage.spark.agent.OpenLineageSparkListener` + a transport pointed at Marquez — dataset-level lineage for reads/writes/MERGE |
| **dbt** | **`dbt-ol`** (OpenLineage's dbt wrapper) or via **astronomer-cosmos** (already a dependency) — model/test lineage |

Wiring (sketch): add `marquez` + `marquez-web` + a small Postgres to a `lineage` compose profile, set
the OpenLineage transport (`OPENLINEAGE_URL=http://marquez:5000`) for each tool, and browse the graph
at the Marquez UI. This is the cleanest way to *see* CAP-1's two lineages (CDC→Iceberg and dbt/Delta)
as one DAG of datasets.

---

## What's built vs. what's next

- **Built & verified (this profile):** Prometheus + Grafana + `kafka-exporter` + `postgres-exporter`
  + Spark `PrometheusServlet`. Chosen because it's the smallest set that lights up the curriculum's
  flagship signals live — **CDC-5** slot/WAL (`postgres-exporter`), **KAF-1/2** lag+offsets
  (`kafka-exporter`, over the Kafka protocol — no fragile JMX-agent injection), **SPK-\*** JVM/exec
  (config-only). All 5 targets verified UP.
- **Next, if you want more (described above, not built):** Spark/Connect **`jmx_exporter`** for
  Debezium connector MBeans; **Airflow** OTel/StatsD; **dbt Elementary**; then **OpenLineage →
  Marquez** lineage (start with the Airflow provider — one package + an env var — since AF-10/CAP-1
  already orchestrate the real jobs).
- Everything stays **opt-in and offline**; never required to run a module.

## Optional, masked: New Relic (commercial alternative)

The inherited code referenced New Relic; the curriculum is **100% offline and never requires it**.
If a learner *wants* a hosted backend, NR (and any OTel vendor) ingests the **same** OpenTelemetry
metrics + OpenLineage events described above — point the OTel Collector / OpenLineage transport at the
vendor endpoint instead of Prometheus/Marquez. **No account IDs, license keys, or internal endpoints
are stored in this repo** (they were removed in F-7); a learner would supply their own via env vars.

---

### Verification status

The **built** metrics profile was verified live in this repo: `make monitoring-up` brought up
Prometheus, Grafana, and both exporters; Prometheus reported all five targets **UP**
(`prometheus`, `spark`, `spark-executors`, `kafka`, `postgres`); the Spark `/metrics/prometheus/`
endpoint served driver gauges; `kafka-exporter` exposed 600+ `kafka_*` series and `postgres-exporter`
500+ `pg_*` series including the `pg_replication_slots_*` (CDC-5) family. The **described-only**
extensions (Connect-JMX, Airflow OTel, dbt Elementary, OpenLineage/Marquez) are from established OSS
practice and were sanity-checked against current docs (Spark monitoring, kafka_exporter, Airflow
metrics) but not themselves wired up — confirm exact image tags / MBean names when you implement them.
