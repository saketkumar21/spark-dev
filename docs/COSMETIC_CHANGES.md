# Cosmetic Changes — planned (NOT yet executed)

> **Status: decided, not implemented.** This records a structure decision so it isn't lost. No
> files have been changed. Pick it up later; nothing here is urgent or blocking.

## The decision — "the notebook *is* the module" (notebook tracks only)

Today every module is a sub-folder with **both** a `README.md` and a runnable notebook, and their
Break→Detect→Fix→Prove narratives overlap (and can drift). The agreed fix:

**For the notebook tracks — `spark/`, `iceberg/`, `kafka/`, `debezium/`:**
1. **Fold each per-module README's content into its notebook** as markdown cells (scenario, the
   Detect/Fix tables, the "in real production" takeaways — the notebook already has the skeleton;
   this completes it so the notebook is the full lesson).
2. **Delete the per-module `README.md`** files.
3. **Keep the sub-folders and the notebook** (e.g. `spark/skew/spk1_data_skew.ipynb` stays put).
4. **Keep each track-level README** (`spark/README.md`, `iceberg/README.md`, `kafka/README.md`,
   `debezium/README.md`) as a **thin index/map** — the module table + the existing shared preamble
   (how to run, the `common/` toolkit, reading the UI). **Do NOT** migrate per-module depth up into
   it (that would just move the two-place split up a level and re-introduce drift).

**For `airflow/`, `capstone/`: leave exactly as-is.** They have no per-module notebook (the artifact
lives in `airflow/dags/` or is a markdown incident card), so their README/docstring *is* the module —
nothing to fold into.

> **Update (2026-06-24):** the former top-level `quality/` track was **moved into the dbt project**
> it teaches → `dbt/quality/`, and its 10 markdown-only module folders were **flattened to files**
> (`dbt/quality/dbtN_*.md`); the track index (`dbt/quality/README.md`) and the GE lab
> (`dbt/quality/great_expectations/`) stayed. `dbt build` is unaffected (dbt only compiles
> `models/seeds/tests/macros`). All cross-links were updated. This is the one place the
> "leave non-notebook tracks as-is" guidance was superseded.

Resulting convention, stated plainly:
> **Notebook tracks → the notebook is the module. Non-notebook tracks → the README is the module.**

## Why this option (vs the alternatives we weighed)

| Option | Verdict |
|--------|---------|
| **Fold README → notebook, keep folders, thin track README** | ✅ **Chosen.** Single source of truth per module (kills drift); notebook already ~80% there; **near-zero link cost** because the ~131 cross-links point at the *folders*, not the README files. |
| Keep both, de-duplicate by role | Fine, but keeps two files in sync forever — doesn't fully remove the drift risk. |
| Leave as-is | Zero effort, but the duplication you flagged stays. |
| Slim README to a pointer | Keeps a near-empty file per module for little gain. |
| Merge to one notebook **+ flatten folders + rename** | ❌ Rejected — would break/rewrite **~131** relative links (`LEARNING_PATH` 60, track READMEs 40, incident cards 22, inter-module 9) and lose folder-level browsing, for a cosmetic gain. |
| Also add a "deep info" `spark/README.md` content home | ❌ Rejected — recreates the two-place split one level up; cross-cutting depth already lives in `docs/` (`spark-ui-guide`, `troubleshooting`, `CURRICULUM_BRIEF`). |

## The one tradeoff being accepted
Browsing a module folder on GitHub will show the **rendered notebook** instead of a rendered
README. Fine for clone-and-run (the common case); a minor downgrade only for reading modules on
GitHub without opening them. The track README still gives the at-a-glance map. Edits are
**markdown-only** → code/execution untouched → re-verification is light.

## Execution plan (when greenlit)
1. **Sample first:** `spark/skew/` → fold its `README.md` into `spk1_data_skew.ipynb` (as markdown
   cells), delete the README, leave the folder + all links intact. Eyeball it.
2. If approved, **fan out** the same pass across the remaining 37 notebook modules
   (spark ×9 more, iceberg ×10, kafka ×9, debezium ×9) — parallelizable via subagents, one module
   per file, no shared-file contention.
3. **Per track:** quick link-check (the `../<module>/` folder links still resolve), strip notebook
   outputs, commit per track (`cosmetic: fold <track> module READMEs into notebooks`).
4. **Do not touch** `airflow/`, `capstone/`, or the ~131 cross-links (folders stay, so
   links keep resolving). Update `docs/LEARNING_PATH.md` only if any link text implies "README."

## Key facts to remember
- ~**131** relative links point at module **folders** (not README files) → keeping folders = no link churn.
- Per-module notebooks already carry Break→Detect→Fix→Prove markdown cells; the README "extra" is
  mostly the richer **tables** + **"in real production"** takeaways, which become 1–2 more cells.
- All notebooks are output-stripped + were verified via `nbconvert`; a markdown-only pass keeps that valid.
