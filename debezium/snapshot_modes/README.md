# CDC-3 — Snapshot vs Streaming Phases

> **Break → Detect → Fix → Prove.** When a Debezium connector starts for the first time it runs in
> two distinct phases. First the **initial snapshot**: it reads every existing row of the captured
> table and emits one **`r` (read)** event per row — a consistent point-in-time picture of the table
> *as it already is*. Then it switches to **streaming**: it tails the WAL via the replication slot and
> emits **`c` / `u` / `d`** events for every change that happens *from then on*. The connector's
> **`snapshot.mode`** decides whether (and how) that first phase runs. Pick the wrong mode and you
> either **re-read a billion-row table** you didn't need to, or **silently miss all the data that was
> already there** before the connector came up.

- **Notebook:** [`cdc3_snapshot_modes.ipynb`](./cdc3_snapshot_modes.ipynb)
- **Toolkit used:** [`common.cdc_helpers`](../../common/cdc_helpers.py) — `seed_orders`,
  `debezium_pg_config` (the `snapshot_mode=...` knob), `register_connector`, `wait_for_connector`,
  `connector_status`, `topic_name`, `read_cdc_events`, `op_counts`, `pg_exec`, `teardown`. No Spark
  (`kafka-python` + the Connect REST API only).
- **Run against:** the CDC stack (`make cdc-up` → Postgres + Kafka Connect + Kafka). Producers/admin
  use the host listeners (`localhost:5432`, `localhost:8083`, `localhost:29092`); inspect the topics
  live in **kafka-ui** at http://localhost:8080.
- **Time:** ~10–12 min. **Laptop-safe:** two tiny (≤20-row) tables, bounded topic reads, short
  bounded `sleep`s, and **two** connectors/tables both torn down at the start and the end.

> **Read this first — honesty about what can be triggered.** Two of the three things this module
> teaches are **deterministic and observable** in one notebook run and we prove them outright:
> (1) `snapshot.mode=initial` produces **`r`=N then `c`** on one topic — the two phases, side by side;
> (2) `snapshot.mode=never` produces **`c` only** — existing rows are never re-emitted. The **third**
> — *interrupting a snapshot mid-way* — is **not** something we can force deterministically and
> laptop-safely: our table is so small the snapshot completes in well under a second, so there is no
> window to crash inside. So, exactly like the SPK-2/SPK-3 OOM modules, we **describe** that behavior
> precisely (with the correct mechanism and the resumable-snapshot fix) rather than fake it. The
> notebook still runs top-to-bottom.

---

## 1. The scenario

A team is standing up CDC on a `orders` table that already holds millions of historical rows. Two
engineers configure two connectors and get two very different results:

1. **Engineer A** leaves `snapshot.mode=initial` (the default). The connector reads the whole table
   once — a flood of **`r`** events — *then* starts streaming live changes. Downstream gets the full
   history **plus** every new change: a complete mirror. The cost is that the snapshot re-reads the
   entire table on first start (fine for millions of rows; a problem for billions).
2. **Engineer B** sets `snapshot.mode=never` because "we only care about changes from now on." The
   connector skips the snapshot entirely and streams only what happens **after** it starts. The
   existing rows are **never emitted** — and the downstream mirror is missing all the history nobody
   realized they needed.

Same table, same connector class, one config field — and one pipeline has the data while the other
silently doesn't. This module makes both outcomes concrete and contrasts them with `op_counts`.

## 2. Break it

We run the two modes against **two separate, freshly-seeded tables** so they don't interfere:

- **(A) `cdc3_orders`, `snapshot.mode=initial`.** Seed **N** rows, register the connector, wait for
  `RUNNING`, then read the topic: the snapshot phase has emitted **`r`=N**. *Then* we `INSERT` one row
  directly in Postgres (a change that happens *after* the connector is live) and read again: a single
  **`c`** has joined the same topic. One topic, two phases.
- **(B) `cdc3_orders_never`, `snapshot.mode=never`.** Seed **N** rows, register a *second* connector,
  wait for `RUNNING`. The N pre-existing rows produce **nothing** — `read_cdc_events` finds the topic
  empty (or absent). Only when we `INSERT` one row *after* `RUNNING` do we get exactly **one `c`**, and
  **zero `r`**.

Nothing here "fails" loudly — the failure of mode (B) is the *absence* of the N snapshot rows, which is
precisely the trap: a misconfigured `snapshot.mode` loses data **silently**.

## 3. Detect it

| Where | What you see |
|-------|--------------|
| `op_counts(read_cdc_events(topic))` on the **initial** topic, before any DML | `{'r': N}` — the snapshot phase |
| same, **after** a post-`RUNNING` insert | `{'r': N, 'c': 1}` — snapshot **and** streaming on one topic |
| `op_counts(...)` on the **never** topic, before any DML | `{}` — empty: no snapshot, the N existing rows were skipped |
| same, **after** a post-`RUNNING` insert | `{'c': 1}` — streaming only; still **no `r`** |
| `connector_status(name)` | both connectors `RUNNING` with a `RUNNING` task — the `never` connector is healthy, it simply had no snapshot to do |

The headline signal is the **presence or absence of `r` events**: `r` events exist **iff** a snapshot
ran. `c`/`u`/`d` always come from streaming the WAL. Reading the two `op_counts` dicts side by side is
the whole diagnosis — `initial` = `r`+`c`, `never` = `c` only.

## 4. Diagnose

- **Two phases, one slot, one topic.** On first start the connector takes a **consistent snapshot**
  (it reads the table at a single point in the WAL and labels every row `op="r"`), then switches to
  **streaming** from exactly that LSN forward, emitting `c`/`u`/`d` as it decodes the WAL. Both phases
  publish to the *same* `<prefix>.<schema>.<table>` topic, so a downstream consumer that processes `r`
  and `c`/`u`/`d` uniformly gets a seamless full picture.
- **`snapshot.mode` chooses the first phase.** `initial` snapshots once on first start then streams
  (and skips the snapshot on later restarts, because the connector has committed offsets). `never`
  skips the snapshot **always** and streams from the slot's current position — so any row that existed
  *before* the slot/connector was created is invisible to it.
- **Interrupting a snapshot (described, not triggered).** The default snapshot is **not incremental**:
  it has **no per-row checkpoint**. If the connector crashes part-way through the initial read, on
  restart it has no committed snapshot offset to resume from, so it **restarts the snapshot from
  scratch** — re-reading every row. On a huge table that's expensive and can repeat if the connector
  is flapping. Our table is sub-second to snapshot, so there's no deterministic window to crash inside;
  we therefore *describe* this rather than stage it (see the honesty note above). The fix is
  **incremental snapshots**: Debezium reads the table in chunks driven by a **signal table** (the
  `execute-snapshot` signal), committing progress per chunk so a restart **resumes** mid-table and runs
  concurrently with streaming instead of blocking it.

### `snapshot.mode` matrix (Debezium Postgres)

| Mode | First start | On restart (offsets exist) | Use when |
|------|-------------|----------------------------|----------|
| `initial` *(default)* | snapshot **then** stream | stream only (no re-snapshot) | you want full history **and** ongoing changes |
| `initial_only` | snapshot **then stop** (no streaming) | does nothing | a **one-shot backfill** of the current table |
| `never` | stream only — **no** snapshot | stream only | history is irrelevant; only changes from now on |
| `when_needed` | snapshot only if it can't resume from offsets/WAL | snapshot if offsets are gone, else stream | self-healing after WAL/offset loss |
| `no_data` / `schema_only` | capture **schema**, no row snapshot, then stream | stream | you need the table's schema for decoding but not its existing rows |

*(Mode names track the Debezium Postgres connector; older docs call `no_data` → `schema_only`. We
demonstrate `initial` and `never`; the rest are listed so you can pick deliberately.)*

## 5. Fix it / guidance

- **Pick `snapshot.mode` for your restart and backfill needs, not by default.** Want a complete mirror
  (history + live)? `initial`. Only a one-time copy of the current table? `initial_only`. Only changes
  going forward? `never` — but be sure you *truly* don't need the pre-existing rows, because they're
  gone for that connector.
- **Large tables → incremental snapshots.** For a table too big to re-read on a crash, use incremental
  snapshots (signal table + `execute-snapshot`): chunked, **resumable**, and concurrent with streaming.
  It removes the "crash mid-snapshot ⇒ start over" cliff and the "snapshot blocks all streaming"
  stall.
- **`initial_only` for backfills.** When you're seeding a downstream store once and a *separate*
  connector (or process) handles ongoing changes, `initial_only` snapshots and **stops** — no idle slot
  pinning WAL afterward (compare CDC-5).
- **Make downstream phase-agnostic.** Treat `r` and `c` identically as **upserts** at the sink (CDC-7)
  so the same code absorbs the snapshot and the stream without a special "initial load" path.

## 6. Prove it

The proof is the **side-by-side `op_counts`** the notebook prints:

```
initial (cdc3_orders)       : {'r': N}            # after RUNNING, before DML  → snapshot phase
initial (cdc3_orders)       : {'r': N, 'c': 1}    # after a post-RUNNING insert → snapshot + streaming
never   (cdc3_orders_never) : {}                  # after RUNNING, before DML  → no snapshot at all
never   (cdc3_orders_never) : {'c': 1}            # after a post-RUNNING insert → streaming only, zero r
```

`initial` carries the N snapshot rows; `never` does not — the **`r` count is the entire difference**,
and it's exactly the data Engineer B's pipeline lost. Both connectors are `RUNNING` throughout
(`connector_status`), so this is a configuration difference, not a failure.

## 7. Takeaways & "in real production…"

- **`r` = snapshot, `c`/`u`/`d` = streaming.** The event `op` tells you which phase produced a record;
  the *presence of `r`* tells you a snapshot ran at all. That single fact diagnoses most "where's my
  historical data?" CDC tickets.
- **`snapshot.mode` is a data-completeness decision.** The default `initial` is usually what you want
  (full mirror). `never` is a foot-gun unless you genuinely only care about go-forward changes —
  validate it against "do we need the rows that already exist?"
- **Non-incremental snapshots restart from scratch.** A connector that keeps crashing mid-snapshot on a
  large table will keep re-reading the whole table and never make progress. Reach for **incremental
  snapshots** (signal table) so first-load is chunked, resumable, and doesn't block streaming.
- **One-shot vs ongoing are different jobs.** Use `initial_only` to backfill and a streaming connector
  for the rest — don't leave a snapshot-only connector running (idle slot = retained WAL, CDC-5).
- **Design the sink to be phase-agnostic.** If `r` and `c`/`u`/`d` both become idempotent upserts
  (CDC-7), the snapshot→streaming transition is invisible downstream and a re-snapshot is harmless.

## 8. Teardown

The notebook calls `teardown(name, table)` for **both** connectors/tables at the start (clean slate for
re-runs) and again at the end — each deletes the connector, drops its replication slot, drops the
table, and deletes the Debezium topic. `make clean` clears any remaining local `.tmp/` state.
