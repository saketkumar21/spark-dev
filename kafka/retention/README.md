# KAF-4 — Retention & Compaction

> **Break → Detect → Fix → Prove.** A Kafka topic does not keep messages forever. A topic's
> **`cleanup.policy`** decides what happens to old data: **`delete`** (the default) drops records
> once they age past **`retention.ms`** (or the log exceeds **`retention.bytes`**); **`compact`**
> keeps only the **latest value per key** and garbage-collects the older ones. Get retention wrong
> and an offline consumer comes back to find the offset it wanted **no longer exists**
> (`OffsetOutOfRange`); use compaction where you should use delete (or vice-versa) and you either
> lose history you needed or keep a changelog that never shrinks.

- **Notebook:** [`kaf4_retention.ipynb`](./kaf4_retention.ipynb)
- **Toolkit used:** `common.kafka_helpers` (`ensure_topic`, `produce_events`, `topic_end_offsets`,
  `delete_topic`, `BOOTSTRAP`) + raw `kafka-python` (`KafkaConsumer` for the offline-consumer
  scenario, `KafkaAdminClient.describe_configs` for the proof).
- **Run against:** the Kafka broker (`make up`) — producers/admin use the host listener
  `localhost:29092`; inspect topics & their configs live in **kafka-ui** at
  http://localhost:8080 → topic → **Settings**.
- **Time:** ~10–12 min. **Laptop-safe:** small bounded produce batches, only short bounded
  `sleep`s (never an unbounded wait on the broker), both topics deleted at teardown.

> **Read this first — honesty about timing.** Retention deletion and log compaction are **not**
> instant, on-demand operations. They run on the **broker's own background schedule** (the log
> cleaner / log retention threads, gated by `segment.ms`, `min.cleanable.dirty.ratio`,
> `log.retention.check.interval.ms`, etc.). You cannot force them to a guaranteed completion from a
> client in a notebook. So this module is precise about the split:
> **what is reliably observable here** (the *configs* are applied — provable via `describe_configs`;
> compaction's *semantics* — latest-value-per-key — which we make concrete by producing many
> updates for a few keys and reading them back) **vs. what is broker-scheduled** (the *physical*
> deletion / collapse, which "will" happen but on the broker's clock — we describe the mechanism and
> what you'd observe in production, and poll briefly without ever blocking the notebook).

---

## 1. The scenario

A platform team runs two very different topics:

1. **`kaf4_events`** — a high-volume event log (clickstream / orders). It uses the default
   `cleanup.policy=delete`: keep recent data for replay, then drop it so the disk doesn't fill. An
   analytics consumer normally tails it in near-real-time. One day that consumer is **down for
   longer than the topic's retention window**. When it restarts and asks for the next offset it had
   committed, that data has **already been deleted** — the broker answers `OffsetOutOfRange`, and
   what happens next is decided entirely by the consumer's **`auto_offset_reset`**.

2. **`kaf4_state`** — a **changelog / state** topic (think a KTable's backing store, or Kafka's own
   `__consumer_offsets`). Here you don't want history — you want the **current value for each key**.
   It uses `cleanup.policy=compact`: the broker keeps the latest record per key and eventually
   garbage-collects the superseded ones. A new consumer that reads the whole topic from the
   beginning should be able to rebuild current state without replaying every historical update.

Two policies, two failure modes, two halves of this module.

## 2. Break it

**(A) `delete` — the offline-consumer trap.** We create `kaf4_events` with a deliberately **tiny
`retention.ms`** (and a tiny `segment.ms` so the active segment rolls quickly — only *rolled,
non-active* segments are eligible for deletion). We produce a batch, note the start/end offsets,
then describe the offline-consumer scenario: a consumer that committed offset *N*, was down past the
retention window, and comes back to request *N* — but the earliest offset still on the topic is now
**> N**. The broker raises **`OffsetOutOfRange`**. We demonstrate the *recovery* side directly and
reliably: a `KafkaConsumer(auto_offset_reset="earliest")` pointed at the topic is **handed the
earliest offset that still exists** (not the offset it might have wanted) — which is exactly how a
reset-to-earliest consumer recovers from a truncated start. (`"latest"` would instead skip to the
end and silently *miss* everything in the gap.)

**(B) `compact` — the changelog.** We create `kaf4_state` with `cleanup.policy=compact` plus the
knobs that make compaction *eligible* fast at small scale (`segment.ms`, a very low
`min.cleanable.dirty.ratio`). We then **produce many values for the same handful of keys** —
e.g. key `user-0` gets values `v0, v1, v2, … v9`, and so on for a few keys. The topic now physically
holds *every* update, but the **intended** end state is just the **latest value per key**.

## 3. Detect it

| Where | What you see |
|-------|--------------|
| `KafkaAdminClient.describe_configs([ConfigResource(TOPIC, name)])` | the topic's live `cleanup.policy`, `retention.ms`, `retention.bytes`, `segment.ms`, `min.cleanable.dirty.ratio` — **proof the policy is applied** |
| **kafka-ui** → topic → **Settings** | the same configs in the UI |
| A stale consumer's poll | **`OffsetOutOfRange`** when the requested offset has aged out (delete policy) |
| `topic_end_offsets(topic)` + a fresh `earliest` consumer's first offset | on a truncated topic the earliest *available* offset is **> 0** — the gap is what retention removed |
| Reading a compacted topic back, grouped by key | the **set of keys** and the **latest value per key** (the compaction target); duplicate older values may still be present until the cleaner runs |

`OffsetOutOfRange` on a consumer that was simply *slow/offline* is the unmistakable signature of
"retention < how far behind I fell." (Compare **KAF-2**: lag measures *how far behind*; retention
decides *whether falling that far behind is survivable at all*.)

## 4. Diagnose

- **`delete` policy.** The log is a sequence of **segments**. A segment becomes eligible for
  deletion once it is **rolled** (no longer the active segment — controlled by `segment.ms` /
  `segment.bytes`) **and** its records are older than `retention.ms` (or the partition exceeds
  `retention.bytes`). A background thread then deletes whole segments and **advances the log-start
  (earliest) offset**. A consumer asking for an offset **below** the new log-start gets
  `OffsetOutOfRange` — the data is simply gone. `auto_offset_reset` is the *only* thing that decides
  recovery: `earliest` replays from the oldest surviving record (safe, may duplicate); `latest`
  jumps to the end (no errors, **silently skips the gap**); `none`/`error` raises so you notice.
- **`compact` policy.** The log cleaner periodically scans **rolled** segments and rewrites them,
  keeping only the **most recent record per key** (a `null`-valued record is a **tombstone** —
  retained briefly via `delete.retention.ms`, then dropped, to signal a delete). The **active**
  segment is never compacted, and a segment is only cleaned once its "dirty ratio" exceeds
  `min.cleanable.dirty.ratio`. So compaction is **eventual**: right after producing, the topic still
  contains every version; the broker collapses them to latest-per-key on its own schedule.
- **The shared truth:** both policies are **broker-scheduled background jobs**. A client sets the
  *policy and thresholds* (provable immediately) and observes the *effect* later. This module proves
  the former rigorously and is explicit that the latter is on the broker's clock.

## 5. Fix it / guidance

| Need | Policy & settings | Why |
|------|-------------------|-----|
| Event log you replay within a known window | `cleanup.policy=delete`; size **`retention.ms`** ≥ your worst-case consumer downtime + replay needs; optionally cap with **`retention.bytes`** | the offset you need still exists when you come back → no `OffsetOutOfRange` |
| Current-state / changelog topic (KTable, offsets, config) | `cleanup.policy=compact` (optionally `compact,delete`) | keeps latest-per-key forever without unbounded growth; a new consumer can rebuild state from offset 0 |
| Recover a consumer that *did* fall past retention | set **`auto_offset_reset`** deliberately — `earliest` to reprocess the surviving log (pair with an idempotent sink, see KAF-2), `latest` only if skipping the gap is acceptable | turns an unrecoverable `OffsetOutOfRange` into a defined behavior |
| Keep consumers from ever aging out | monitor **consumer lag** (KAF-2) and alert before lag approaches the retention window | retention failures are lag failures that went unnoticed |

## 6. Prove it

1. **Configs are applied (rigorous, immediate).** `describe_configs` prints, for each topic, its
   live `cleanup.policy` and retention/segment/dirty-ratio knobs. This is hard proof that the topic
   *will* be retained/compacted as configured — independent of when the broker's threads next run.

   ```
   kaf4_events :  cleanup.policy=delete   retention.ms=<tiny>   segment.ms=<tiny>   retention.bytes=...
   kaf4_state  :  cleanup.policy=compact  segment.ms=<tiny>     min.cleanable.dirty.ratio=0.01
   ```

2. **Compaction *intent* (semantics, concrete).** After producing many updates per key, we read the
   topic back and reduce to **latest value per key** — the exact end state compaction converges to.
   We print `keys × versions produced` vs. the `latest-per-key` map, and note that the broker will
   physically drop the superseded versions on its schedule (we poll the record count briefly to show
   movement *if* the cleaner has run, but never block).

3. **`OffsetOutOfRange` recovery (behavioral).** On the delete-policy topic, a fresh
   `auto_offset_reset="earliest"` consumer reports the **earliest available offset**; we explain that
   if retention had truncated the start, this is exactly the offset a recovering consumer would be
   reset to — and that `latest` would instead skip straight to the end.

## 7. Takeaways & "in real production…"

- **Retention is a contract with your consumers.** Size `retention.ms` / `retention.bytes` to the
  *longest* a consumer might be down plus any replay/reprocessing you need — not to whatever the
  default is. The cost of getting it small is silent data loss for anyone who falls behind.
- **`OffsetOutOfRange` means "I fell past retention."** Set `auto_offset_reset` on purpose:
  `earliest` to reprocess (with an idempotent sink), `latest` only when skipping the gap is fine,
  and alert on it — it should be rare and investigated.
- **Compaction is for state, not history.** Use `cleanup.policy=compact` for changelog/KTable/offset
  topics where you only care about the current value per key; it bounds size without a time window.
  Use `compact,delete` if you also want a time ceiling. Tombstones (`null` values) propagate deletes.
- **Both cleanups are background-scheduled.** Don't write code that assumes data vanished or
  collapsed the instant you changed a config — assert on the *config* and the *semantics*; treat the
  physical cleanup as eventual (tune it with `segment.ms`, `min.cleanable.dirty.ratio`,
  `log.retention.check.interval.ms`, but never depend on instant execution).
- **Retention failures are lag failures.** A healthy, monitored consumer (KAF-2) never ages out;
  page on lag approaching the retention window so you fix it before data is gone.

## 8. Teardown

`delete_topic(TOPIC)` removes both topics. `make clean` also clears any local Kafka / `.tmp/` state.
