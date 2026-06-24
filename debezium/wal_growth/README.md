# CDC-5 — Replication slot & WAL growth ⚠️

**Break → Detect → Fix → Prove.** A logical replication slot is a **named cursor into the WAL**.
Postgres retains every WAL segment from the slot's `restart_lsn` forward **until a consumer reads
past it and advances the slot**. A healthy consumer (Debezium) advances it continuously, so WAL is
recycled and disk stays flat. But if the consumer stops while writes continue, the slot goes
**inactive** and the retained WAL grows without bound — **the database fills its own disk**. This is
one of the most common Postgres CDC outages, and it is silent until the disk is full.

This is the ⚠️ flagship pathology of the CDC track. It builds directly on the slot mechanics from
[CDC-1](../postgres_setup/) and the connector lifecycle from [CDC-2](../connector_bringup/).

**Prerequisite:** `make cdc-up` (Postgres + Kafka Connect). **Laptop-safe:** a few thousand small
rows generate only a few **MB** of WAL — bounded loops, never fills disk; manual slot **and** any
connector are dropped at teardown.

---

## The honest caveat (read this first)

The *real* operational scenario is a connector down for **hours** while production writes **GBs** of
WAL — the disk fills and Postgres halts. **We deliberately do not reproduce that**: it would risk the
learner's disk and violate the laptop-safety rule. Instead we demonstrate the **exact same mechanism
at tiny scale** — `retained_bytes` climbing monotonically for an inactive slot — which is the metric
you alert on in production. The numbers are MB, not GB; the failure mode is identical.

---

## Break — pin WAL with an inactive slot

The cleanest, fully deterministic reproduction (as in CDC-1) uses a **hand-made slot** so there's no
connector advancing it:

```python
cdc.pg_exec("SELECT pg_create_logical_replication_slot('cdc5_manual_slot', 'pgoutput')")
```

The slot is `active=False` from birth — nothing is consuming it. Now write rows in **bounded
batches** and watch the slot's `retained_bytes` climb after every batch. `restart_lsn` never moves
(no consumer), but `pg_current_wal_lsn()` does (we keep writing) — so the gap Postgres must retain
only grows.

## Detect — `pg_replication_slots`

`pg_replication_slots` is the source of truth, surfaced by `common.cdc_helpers.list_slots()`:

| Field | Meaning |
|-------|---------|
| `active` | Is a consumer attached? `False` ⇒ **nobody is advancing this slot.** |
| `restart_lsn` | The oldest WAL the slot still needs. Frozen while inactive. |
| `retained_bytes` | `pg_current_wal_lsn() - restart_lsn` — **the WAL this slot is pinning.** The headline metric. |

The smoking gun is `active=False` **and** `retained_bytes` rising on each measurement. The notebook
builds a small **before → after table** (rows written vs `retained_bytes`) — the quantitative
"Prove it".

## The realistic connector version (active → paused → resumed)

To show it as it actually happens in production, the notebook also drives a **real Debezium
connector** through its lifecycle via the Kafka Connect REST API:

1. **Register** the connector → slot `active=True`, `retained_bytes` stays ~flat as it consumes.
2. **Pause** it (`PUT /connectors/<name>/pause`) → the slot **stays** but goes **inactive**; this is
   the production incident (connector crashed / paused / lagging).
3. **Write more rows** → `retained_bytes` grows, exactly as with the manual slot.
4. **Resume** it (`PUT /connectors/<name>/resume`) → the connector catches up, advances the slot, and
   `retained_bytes` **recycles back down**. The cure in one call.

## Diagnose

> **Inactive slot + ongoing writes = retained WAL that Postgres cannot garbage-collect.**

Postgres keeps the WAL because, as far as it knows, a consumer still needs everything from
`restart_lsn` on. An *abandoned* slot (connector deleted but slot left behind, or a connector down
indefinitely) pins WAL **forever** → the disk fills → the primary stops accepting writes.

## Fix

- **Keep consumers healthy.** Resume/restart a paused or failed connector; a running Debezium task
  advances the slot and WAL recycles. (Demonstrated above — resume drops `retained_bytes`.)
- **Reap orphaned slots.** A slot with no owner must be dropped: `drop_slot(name)` /
  `pg_drop_replication_slot(name)`. The notebook shows `retained_bytes` falling to ~0 the moment the
  slot is gone — Postgres immediately recycles the WAL it was pinning.
- **Cap the blast radius with `max_slot_wal_keep_size` (PG 13+).** Set a ceiling so Postgres
  **invalidates a runaway slot** rather than running out of disk. The default in this repo's Postgres
  is `-1` (**disabled = unbounded** — the dangerous default); the notebook shows `SHOW
  max_slot_wal_keep_size` and explains the production setting. Trade-off: an invalidated slot forces
  its consumer to re-snapshot, so size it generously and alert *before* the cap.

## Prove

- Manual inactive slot: a **monotonically climbing** `retained_bytes` table vs rows written.
- Connector: `retained_bytes` flat (running) → climbing (paused) → recycled (resumed).
- `drop_slot` → `retained_bytes` for that slot disappears (WAL freed).

## Takeaways & "in real production…"

- **Alert on `pg_replication_slots`.** Page on `active = false` for any slot, and on
  `retained_bytes` / `pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)` crossing a threshold. This
  is the single most important Postgres-CDC alert.
- **One slot per consumer**, and **reap abandoned slots** — a leftover slot from a deleted connector
  is an outage waiting to happen (it's durable state on the *primary*, not on the consumer).
- **Set `max_slot_wal_keep_size`** as a guardrail so a stuck consumer can't take the database down.
- Ties back to **CDC-1** (what a slot *is*) and forward to monitoring / the incident simulator.

---

## Teardown

The notebook drops `cdc5_manual_slot`, tears down the `cdc5-orders` connector (which also drops its
slot), and drops `public.cdc5_orders`. `make clean` clears all generated data.
