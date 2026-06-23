# KAF-3 — Consumer groups & rebalancing

> **Break → Detect → Fix → Prove.** Consumers in the same **group** split a topic's partitions
> among themselves — each partition is owned by exactly one member, so the group's throughput
> scales with partition count. Whenever membership changes (a consumer joins, leaves, or is
> *presumed dead*), the group **rebalances**: partitions are revoked and reassigned. During a
> stop-the-world rebalance **every** consumer pauses, and if offsets weren't committed before a
> partition was revoked, work gets **reprocessed or lost**.

- **Notebook:** [`kaf3_rebalancing.ipynb`](./kaf3_rebalancing.ipynb)
- **Toolkit used:** `common.kafka_helpers` (`ensure_topic`, `produce_events`, `delete_topic`) +
  raw `kafka-python` `KafkaConsumer` to form a group and watch `.assignment()` shift.
- **Run against:** the Kafka broker (`make up`) — consumers use the host listener
  `localhost:29092`; inspect the group live in **kafka-ui** at http://localhost:8080 → topic →
  **Consumers**.
- **Time:** ~10 min. **Laptop-safe:** a bounded ~600-message batch, no infinite loops — every
  consumer uses `consumer_timeout_ms` so polls return on their own and is `close()`d; the topic is
  deleted at teardown.

---

## What you can observe in one process (and what you can't)

True multi-process rebalancing — a separate worker dying mid-batch, a **stop-the-world pause**, a
`session.timeout.ms` expiry firing in real time — needs more than one OS process and can't be
faithfully reproduced inside a single notebook. So, like the SPK-2 / SPK-3 OOM modules, this
module is **honest about the boundary**:

- **Demonstrated for real:** several `KafkaConsumer` objects sharing one `group_id` form a group
  in this one process, and we watch `.assignment()` **split** as a member joins and **merge back**
  as one leaves. That partition reassignment *is* the rebalance — it's the observable core.
- **Described with snippets (not executed):** the wall-clock *pause* during a stop-the-world
  rebalance, a slow consumer being evicted on `max.poll.interval.ms` expiry, and the
  multi-process config fixes (`session.timeout.ms`, static membership, cooperative-sticky). These
  are shown as code/config you'd use in a real deployment.

## 1. The scenario

An orders service publishes to a 3-partition topic. A consumer group (`kaf3-grp`) reads it. The
team scales the group up and down — deploys add a consumer, crashes remove one — and each change
triggers a rebalance. On-call notices the dashboard *freezes* for a moment on every deploy, and
occasionally a record is processed twice. *Why does scaling the group cause pauses and duplicates?*

## 2. Break it — watch assignment shift as the group changes

The topic has **3 partitions**; we produce a bounded batch so there's data to consume.

1. **Consumer A alone.** Start one `KafkaConsumer(group_id="kaf3-grp")`, poll it → `A.assignment()`
   holds **all 3 partitions** (it's the only member).
2. **Consumer B joins.** Start a second consumer with the **same** `group_id`, poll **both** → the
   group rebalances and assignment **splits** to roughly **A:2 / B:1**. The split is the rebalance.
3. **"Kill" B.** Call `B.close()` (a graceful `LeaveGroup` — the in-process stand-in for a crash),
   then poll A → A **reclaims all 3** partitions (rebalance back).

Each step prints the per-consumer assignment, so you watch ownership move `3 → split → 3`.

> The hazard this models: if B was mid-batch and hadn't committed when its partition was revoked,
> A re-reads those offsets → **duplicate processing** (or, with auto-commit firing too early,
> **lost** work). Rebalances are exactly when offset-commit discipline (KAF-2) matters most.

## 3. Detect it

| Where | What you see |
|-------|--------------|
| `consumer.assignment()` after a poll | the set of `TopicPartition`s this member owns; it **changes** across join/leave |
| **kafka-ui** → topic `kaf3_orders` → **Consumers** → `kaf3-grp` | members and their assigned partitions, live; member count and ownership shift as the group changes |
| consumer logs (real deployment) | `Revoking previously assigned partitions …` / `Setting newly assigned partitions …` — one pair per rebalance; a *storm* is many of these back-to-back |

The signature of a rebalance is **assignment changing** — partitions revoked from one member and
handed to another. A *healthy* group shows stable assignments; a flapping one rebalances
repeatedly (a "rebalance storm").

## 4. Diagnose — why rebalances happen and why they hurt

A rebalance is triggered by any **membership change**:

- a consumer **joins** (scale-up / deploy) or **leaves** gracefully (`close()` → `LeaveGroup`),
- a consumer is **presumed dead**: it misses heartbeats for `session.timeout.ms`, **or** it takes
  longer than `max.poll.interval.ms` between `poll()` calls (a slow batch *looks* dead even though
  the process is alive),
- the **partition count** of a subscribed topic changes.

Why it hurts (classic **eager** rebalancing, the default before cooperative): it's
**stop-the-world** — every member revokes *all* its partitions and consumption **pauses across the
whole group** until the new assignment is computed and accepted. The longer/more frequent the
rebalances, the more wall-clock time the group spends paused instead of processing. And because
partitions get revoked, any uncommitted offsets on a revoked partition become reprocessing
(at-least-once) or loss (if auto-commit committed ahead of processing).

## 5. Fix it / mitigate

The notebook **shows the config** for each (and applies what's observable in one process); the
runtime *effect* of the first three is a multi-process property described, not executed.

| Fix | Config | Why it helps |
|-----|--------|--------------|
| **Tune liveness timeouts** | `session.timeout.ms` (e.g. 45s), `heartbeat.interval.ms` (≈ ⅓ of session), `max.poll.interval.ms` (above your worst-case batch time) | Stops a *slow-but-alive* consumer from being falsely evicted — the #1 cause of avoidable rebalance storms. Raise `max.poll.interval.ms` when each batch does heavy work. |
| **Static membership** | `group.instance.id="worker-1"` (stable per instance) | A consumer that restarts with the **same** id rejoins as the *same* member within `session.timeout.ms` — the broker **skips the rebalance** entirely. Eliminates the deploy/restart rebalance. |
| **Cooperative-sticky assignor** | `partition.assignment.strategy=[CooperativeStickyAssignor]` | **Incremental** rebalancing: only the partitions that actually move are revoked; everyone else keeps consuming. Turns stop-the-world into a partial pause. |
| **Right-size the group** | `#consumers ≤ #partitions` | Extra consumers beyond the partition count sit **idle** (no partition to own) yet still participate in rebalances — cost with no throughput gain. |
| **Commit before you can be revoked** | manual commit *after* processing (KAF-2) + idempotent sink | Makes the inevitable reprocessing on revoke **harmless** → effective exactly-once. |

## 6. Prove it

The assignment table across the three steps is the proof — ownership moves and then returns:

| Step | A owns | B owns | total partitions covered |
|------|-------:|-------:|-------------------------:|
| A alone | 3 | — | 3 |
| B joins (rebalance) | ~2 | ~1 | 3 |
| B leaves (rebalance back) | 3 | — | 3 |

Every partition is always owned by exactly one member, the count is conserved at 3, and the
*split → merge* is the rebalance happening in front of you.

## 7. Takeaways & "in real production…"

- **Rebalances are costly — minimize them.** Every membership change pauses the group (fully, with
  eager assignment); frequent rebalances ("storms") are an availability problem, not a curiosity.
- **#consumers ≤ #partitions.** Partitions cap useful parallelism; extra consumers idle but still
  rebalance. Scale partitions first if you need more throughput.
- **Most avoidable rebalances are false-death evictions.** Tune `session.timeout.ms` /
  `heartbeat.interval.ms`, and set `max.poll.interval.ms` above your worst-case batch — a slow
  consumer must not look dead.
- **Use static membership for rolling restarts** (`group.instance.id`) so deploys don't rebalance,
  and **cooperative-sticky** so the rebalances you can't avoid are incremental, not stop-the-world.
- **Pair with offset discipline (KAF-2).** Commit after processing + an idempotent sink so the
  reprocessing a revoke causes is harmless.

## 8. Teardown

Every `KafkaConsumer` is `close()`d (no lingering group members), and `delete_topic(TOPIC)` removes
the topic. `make clean` also clears any local Kafka / `.tmp/` state.
