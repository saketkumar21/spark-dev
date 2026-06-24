# `debezium/` — Change Data Capture track (Phase 4) ✅ complete

A self-contained CDC pipeline — **Postgres → Debezium (Kafka Connect) → Kafka → Spark → Iceberg
MERGE** — and the production failure modes that come with it. Deployed as **Kafka Connect + the
Debezium Postgres connector** (mirrors real production). Each module follows
**Break → Detect → Fix → Prove** (see [`docs/CURRICULUM_BRIEF.md`](../docs/CURRICULUM_BRIEF.md))
and reuses [`common/cdc_helpers.py`](../common/cdc_helpers.py) (Postgres DML/seed, the Debezium
connector lifecycle over the Connect REST API, replication-slot inspection, and a teardown that
resets offsets + drops the slot/topic) plus [`common/kafka_helpers.py`](../common/kafka_helpers.py).

> **Start the track:** `make cdc-up` brings up the two **opt-in** services (Postgres + Kafka
> Connect) on top of the base stack; `make up` does **not** start them. Then `make jupyter` and open
> a module. `make cdc-down` stops just the CDC services.
>
> **Laptop note (8 GB):** CDC adds Postgres + Kafka Connect (~1.3 GB). Stop optional services while
> running it: `docker compose stop spark-history kafka-ui`. This dev stack targets headroom on
> 16 GB+; all generated data stays in `.tmp/` and `make clean` recovers.
>
> **Connect-safe / laptop-safe:** modules drive Postgres + Connect from the notebook via
> `kafka-python` / `psycopg2` / the Connect REST API and read CDC topics with Spark over
> `kafka:9092`; tiny tables, bounded reads, teardown at start **and** end (idempotent re-runs).

## Modules

`[ ]` not started · `[~]` in progress · `[x]` built & live-tested (headless `nbconvert`)

| ID | Module | Status |
|----|--------|--------|
| `CDC-1` | [Local Postgres & logical replication](postgres_setup/) — `wal_level=logical`, publication, replication slot; WAL the slot pins | `[x]` |
| `CDC-2` | [Debezium connector bring-up](connector_bringup/) — register via the Connect REST API; initial snapshot then streaming | `[x]` |
| `CDC-3` | [Snapshot vs streaming phases](snapshot_modes/) — `snapshot.mode` (`initial` vs `never`); the restart-from-scratch caveat | `[x]` |
| `CDC-4` | [The CDC event envelope](event_envelope/) — `before`/`after`/`op`/`ts_ms`/`source`; flatten with `ExtractNewRecordState` | `[x]` |
| `CDC-5` | [Replication slot & WAL growth ⚠️](wal_growth/) — inactive slot retains WAL → disk grows; detect via `pg_replication_slots`; `max_slot_wal_keep_size` | `[x]` |
| `CDC-6` | [Tombstones, deletes & replica identity](deletes_tombstones/) — `op=d` + tombstone; `REPLICA IDENTITY FULL` for the full `before` | `[x]` |
| `CDC-7` | [CDC → Spark → Iceberg upsert pipeline](cdc_to_iceberg/) — `MERGE` c/u/d into an Iceberg mirror; idempotent by LSN | `[x]` |
| `CDC-8` | [CDC schema evolution](schema_evolution/) — `ALTER TABLE` upstream; no DDL events; evolve the Iceberg sink (LAK-6) | `[x]` |
| `CDC-9` | [Deep failure-mode tour](failure_modes/) — pause/resume/restart & offset recovery; ordering; effectively-once reasoning | `[x]` |

## Layout

```
debezium/
├── README.md             # this file (Phase 4 track index)
├── postgres_setup/       # CDC-1
├── connector_bringup/    # CDC-2
├── snapshot_modes/       # CDC-3
├── event_envelope/       # CDC-4
├── wal_growth/           # CDC-5
├── deletes_tombstones/   # CDC-6
├── cdc_to_iceberg/       # CDC-7
├── schema_evolution/     # CDC-8
└── failure_modes/        # CDC-9
```

Each `debezium/<topic>/` holds a `README.md` (the Break→Detect→Fix→Prove writeup) and a runnable
`cdc<N>_<topic>.ipynb`. All built and **live-verified** end-to-end against the
Postgres + Kafka Connect + Spark + Iceberg stack.

## Suggested order

`CDC-1` (logical replication) → `CDC-2` (connector bring-up) → `CDC-3` (snapshot modes) →
`CDC-4` (envelope) → `CDC-5` (WAL growth ⚠️) → `CDC-6` (deletes/replica identity) →
`CDC-7` (Spark→Iceberg MERGE) → `CDC-8` (schema evolution) → `CDC-9` (failure-mode tour).
CDC-1/2 build the pipeline; CDC-5 is the flagship pathology; CDC-7 is the integration payoff
(and, with STR-2 / KAF-5, the bridge to **Phase 7's capstone**).

## How it connects to the rest of the curriculum

- **Kafka track (Phase 3):** Debezium *is* a Kafka producer — per-key ordering (KAF-1), consumer
  lag (KAF-2), delivery semantics (KAF-5), and Spark checkpoints (STR-2) all apply to the CDC stream.
- **Lakehouse track (Phase 2):** the CDC sink is an Iceberg table — `MERGE` (LAK-8), schema
  evolution (LAK-6), and small-file/maintenance debt (LAK-2/3) all show up downstream.
