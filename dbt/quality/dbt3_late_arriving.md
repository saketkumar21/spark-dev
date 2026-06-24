# DBT-3 — Late-arriving data & lookback windows ⚠️

> **Break → Detect → Fix → Prove.** An incremental model only re-processes "new" rows so each run
> stays cheap. The usual watermark is *event time*: "process everything newer than the latest event I
> already have." That's correct **only if data always arrives in event-time order**. It doesn't. A row
> can land **today** carrying **yesterday's** event timestamp — a *late arrival*. A tight, zero-slack
> event-time window steps right over it: its event time is already below the high-water mark, so the
> incremental filter excludes it and **the row is silently lost**. A configurable **lookback window**
> re-scans recent history every run and the `MERGE` dedups by `unique_key`, so the late row is
> recaptured and existing rows aren't duplicated.

This expands the project under [`dbt/`](README.md). Run everything from there:

```bash
cd dbt && source .env        # sets DBT_PROFILES_DIR + Thrift connection vars
dbt <cmd>                     # Thrift JDBC → Spark; fct_orders is a Delta MERGE table
```

- **Models:** [`stg_orders`](../models/staging/stg_orders.sql) (view) →
  [`fct_orders`](../models/marts/fct_orders.sql) (incremental `merge`, Delta, `unique_key=order_id`).
- **Seed:** [`orders.csv`](../seeds/orders.csv) — 14 rows, each with both an `ordered_at`
  (**EVENT** time) and a `loaded_at` (**LOAD** time, when it landed).
- **Laptop-safe:** 14 tiny rows, two vars, no infra beyond the running Spark server (`make up`).

---

## 1. The two clocks (read this first)

The whole lab hinges on separating two timestamps that production systems always have but toy
datasets usually collapse into one:

| Column | Clock | Role |
|--------|-------|------|
| `ordered_at` | **EVENT** time — when the order actually happened | what the incremental filter watermarks on |
| `loaded_at`  | **LOAD** time — when the row landed in the warehouse | gates *batch availability* |

`stg_orders` filters `loaded_at <= {{ var('load_through') }}` — this **simulates batch availability**:
"only rows that had landed by this load time are visible to this run." Advancing `load_through`
between runs is how we replay yesterday's batch and then today's, deterministically.

The seed has exactly one **genuinely late** row:

```
order_id 1100, C004, $61.00, ordered_at 2024-03-03 06:00, loaded_at 2024-03-04 08:00
```

Its event time (**day 3, 06:00**) is *earlier* than batch-1's high-water mark
(`order_id 1011`, **day 3, 19:40**) — but it didn't physically arrive until **day 4**. That is the
exact shape of a late arrival.

## 2. Break it — a naive event-time window (lookback 0)

**Run 1 — seed the fact with batch 1** (everything loaded through end of day 3):

```bash
dbt run -s stg_orders fct_orders --full-refresh \
  --vars '{load_through: "2024-03-03 23:59:59"}'
```

`fct_orders` = **12 rows**. Row 1100 is *correctly* absent — it hadn't loaded yet
(`loaded_at` is day 4, outside `load_through`). The table's max `ordered_at` is now **day 3, 19:40**.

**Run 2 — batch 2 arrives, naive incremental** (`lookback_hours: 0`):

```bash
dbt run -s stg_orders fct_orders \
  --vars '{load_through: "2024-03-04 23:59:59", lookback_hours: 0}'
```

`fct_orders` = **13 rows** — and **row 1100 is DROPPED. This is the bug.** Batch 2 makes both day-4
rows visible (1100 and 1101), but the incremental filter is:

```sql
where ordered_at > (select max(ordered_at) from {{ this }})
                   - interval '0' hour          -- lookback_hours = 0
```

`max(ordered_at)` is **day 3, 19:40**. Row 1101's event time (day 4, 10:00) clears it and is merged.
Row 1100's event time (**day 3, 06:00**) is *below* the watermark, so the `WHERE` excludes it before
the `MERGE` ever sees it. It is lost — not rejected loudly, just never selected. Re-running won't help:
the watermark only moves forward.

## 3. Detect it

The fact is small, so the symptom is a **row count** and a **presence check**:

```sql
SELECT COUNT(*) FROM analytics.fct_orders;                 -- 12 → 13 → 14
SELECT * FROM analytics.fct_orders WHERE order_id = 1100;  -- empty after Run 2 = data loss
```

The tell: a row you *know* loaded (it's in the seed, within `load_through`) is missing from the fact.
In production you don't have a 14-row seed to eyeball — you detect this with a **reconciliation count**
(source rows available vs fact rows) or a dbt test like `dbt_utils.recency` / a not-null/relationship
test that trips when expected keys go missing. Late-arrival loss is invisible unless you look for it.

## 4. Diagnose

> **An event-time watermark with zero slack assumes monotonic arrival. Late data violates that
> assumption, and the `WHERE max(event_time)` filter excludes anything that predates the high-water
> mark — even though it only just arrived.**

This is the freshness-vs-completeness trade every incremental pipeline makes. Watermark tightly and
you get cheap runs but lose stragglers; widen the window and you recapture them at the cost of
re-scanning. The fix is to make that window an explicit, tunable knob rather than an accidental `0`.

## 5. Fix it — a lookback window

Re-scan a bounded slice of recent history every run, and let the `MERGE` reconcile. Same Run 2 data,
now with **`lookback_hours: 48`**:

```bash
dbt run -s stg_orders fct_orders \
  --vars '{load_through: "2024-03-04 23:59:59", lookback_hours: 48}'
```

`fct_orders` = **14 rows** — **row 1100 is RECAPTURED. This is the fix.** The filter becomes:

```sql
where ordered_at > (select max(ordered_at) from {{ this }})
                   - interval '48' hour
```

The watermark (day 3, 19:40) is pushed back **48 hours** to day 1, 19:40, so the selected slice now
*includes* row 1100's day-3-06:00 event. Because `fct_orders` is an incremental **`merge`** keyed on
`unique_key='order_id'`, re-scanning rows that are already in the fact is **safe**: matched keys
update in place, the one genuinely missing key (1100) inserts. No duplicates, no double-counting.

**The cost vs freshness trade-off:** a bigger lookback re-scans more rows every run (here 48 h is
plenty; a 7-day lookback would re-read a week of orders on *every* build). Size the window to your
worst realistic lateness — large enough to catch stragglers, small enough that runs stay cheap. The
`unique_key` `MERGE` is what makes any lookback idempotent.

## 6. Prove it

Run all three in sequence and count after each:

| Run | `lookback_hours` | `load_through` | `fct_orders` rows | 1100 present? |
|-----|:---------------:|----------------|:-----------------:|:-------------:|
| 1 — full-refresh | n/a | `2024-03-03 23:59:59` | **12** | No (not yet loaded) |
| 2 — naive | **0** | `2024-03-04 23:59:59` | **13** | **No — DROPPED (bug)** |
| 3 — fixed | **48** | `2024-03-04 23:59:59` | **14** | **Yes — RECAPTURED (fix)** |

Row 1100 appearing in Run 3 — and the count reaching the full 14 — is the proof. The MERGE means
Run 3 doesn't double-count the 13 rows already present; it only adds the one it had been missing.

## 7. Takeaways & "in real production…"

- **Late data is the norm, not the exception** — mobile clients buffer offline, upstream batches
  retry, timezones lag. Any event-time incremental needs slack.
- **Make the lookback a `var`/config, not a magic number**, and size it to your p99 arrival lateness.
  An incremental `MERGE` on a `unique_key` makes re-scanning idempotent, so a generous window is cheap
  insurance against silent loss.
- **Separate event time from load time** in your models (as `stg_orders` does). Watermark on event
  time for correctness; gate availability on load time. Collapsing them hides exactly this bug.
- **Test for it.** Reconciliation counts or freshness/relationship tests catch dropped late rows that
  a row-count glance would miss on a real-sized table.
- Ties forward to **DBT-4** (snapshots/SCD2), where the question shifts from "did I capture the row?"
  to "did I capture every *version* of the row?"

## 8. Teardown

Nothing to tear down beyond the dbt-managed tables. `dbt run -s fct_orders --full-refresh` resets the
fact; `make clean` clears all generated data under `.tmp/`.
