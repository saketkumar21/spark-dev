"""dbt-polyglot — run any-dialect dbt models on Spark unchanged.

Transpiles each opted-in model's SQL to Spark via sqlglot at dbt's compile phase.
Install:

    pip install dbt-polyglot

Config (dbt_project.yml):

    models:
      your_project:
        +transpile_from: snowflake

To validate the transpiled SQL against your warehouse before a heavy run, use dbt's
own native flag — no extra tooling needed:

    dbt build --empty           # build every model with zero input rows
    dbt show --limit 0 -s model # read-only: validate without materializing
"""
# Single source of truth for the version: read installed package metadata (pyproject.toml).
try:
    from importlib.metadata import version
    __version__ = version("dbt-polyglot")
except Exception:                      # not installed (e.g. running from a source checkout)
    __version__ = "0.0.0+unknown"

# Activate the compile-time transpile patch. Import-guarded so non-dbt Python is unaffected.
try:
    from dbt_polyglot.transpile import patch_compiler
    patch_compiler()
except Exception:
    pass
