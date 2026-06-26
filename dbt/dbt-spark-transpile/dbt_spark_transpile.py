"""dbt-spark-transpile — run a Snowflake (or other-dialect) dbt repo on Spark, unchanged.

A compile-phase monkeypatch: it wraps dbt-core's ``Compiler._compile_code`` so a model's
*compiled SQL body* is transpiled (via ``sqlglot``) from a source dialect to Spark **before**
dbt's materialization wrapper is applied. Because the rewrite happens pre-wrap, ``target/compiled/``
and the SQL actually executed are both Spark — no mixed-dialect string, no separate output folder.

Drop-in usage — change only config, never a model:

    # profiles.yml : point the output at Spark/Thrift instead of Snowflake.
    # dbt_project.yml :
    models:
      your_project:
        +transpile_from: snowflake     # source dialect of your existing models
        +transpile_to: spark           # optional, default 'spark'

No-op guarantee
    If ``transpile_from`` is unset or equals the target dialect, the model is **never touched**
    (sqlglot is not called).

Fix-up layer (the seamless-maker)
    ``sqlglot``'s Snowflake→Spark output is sometimes valid in *its* model of Spark but rejected by
    Spark 4.0.2's real parser — e.g. ``x NOT IN (subquery)`` becomes ``x <> ALL (subquery)``, which
    Spark does not support. The fix-up registry (``SPARK_FIXUPS``) applies AST transforms to the
    parsed tree before generating Spark, repairing these gaps (quantified-subquery → ``IN``/``NOT IN``
    first). It is extensible: add one transform per gap discovered, each EXPLAIN-verified on Spark.

Trust / fail-soft
    Anything sqlglot can't parse as the source dialect, or that yields empty/multi-statement output,
    logs a WARNING and passes the ORIGINAL SQL through UNCHANGED — Spark then errors *loudly* at run
    if that SQL is invalid (a loud failure, never a silent wrong result). To know upfront which models
    are verified-valid on Spark, run the companion check ``dbt-spark-transpile-check`` (the
    ``transpile_check`` module; see the README).

Scope
    Every opted-in model is transpiled — scope it the dbt-native way: set ``+transpile_from`` on a
    folder/model subtree (or per model) rather than project-wide. Output is pretty-printed.

OSS note
    Patches a dbt-core *private* method (``_compile_code``); import-guarded to fail open, forwards
    ``*args/**kwargs`` for signature drift, self-contained (no host-project imports).
"""
# Activate behind a guard: if dbt-core / sqlglot are not importable (e.g. this venv runs non-dbt
# Python, or the dbt version moved the seam), the patch silently does nothing rather than breaking
# every interpreter start-up in the environment.
try:
    import sqlglot
    from sqlglot import exp
    from dbt.compilation import Compiler
    from dbt.adapters.events.logging import AdapterLogger
except Exception:  # pragma: no cover - import-time guard
    Compiler = None


if Compiler is not None:
    _logger = AdapterLogger("SparkTranspile")

    _DEFAULT_TARGET = "spark"

    # ── Spark-output fix-up registry ─────────────────────────────────────────────
    # Each entry is an `exp.Expression -> exp.Expression` transform applied (via .transform,
    # bottom-up) to the parsed tree BEFORE generating Spark SQL. They repair cases where
    # sqlglot's Spark output is rejected by Spark 4.0.2's real parser. Extensible: append a
    # transform per gap found, and EXPLAIN-verify it on Spark.

    def _as_subquery(node):
        return node if isinstance(node, exp.Subquery) else exp.Subquery(this=node)

    def _fixup_quantified_subquery(node):
        """Spark has no quantified *subquery* comparison. sqlglot's Snowflake parser canonicalizes
        ``x NOT IN (subq)`` -> ``x <> ALL (subq)`` and ``x IN (subq)`` (negated/any forms) ->
        ``x = ANY (subq)``; Spark rejects both. Rewrite back to ``NOT x IN (subq)`` / ``x IN (subq)``.
        """
        if isinstance(node, exp.NEQ) and isinstance(node.expression, exp.All):
            return exp.Not(this=exp.In(this=node.this, query=_as_subquery(node.expression.this)))
        if isinstance(node, exp.EQ) and isinstance(node.expression, exp.Any):
            return exp.In(this=node.this, query=_as_subquery(node.expression.this))
        return node

    SPARK_FIXUPS = [_fixup_quantified_subquery]

    def _spark_safe_transpile(code, src, dst):
        """Parse as `src`, apply the fix-up registry (when targeting spark), generate `dst` SQL.

        Raises on anything unusual (multi-statement / empty) so the caller's fail-soft kicks in.
        """
        statements = sqlglot.parse(code, read=src)
        if len(statements) != 1 or statements[0] is None:
            raise ValueError(f"expected exactly one statement, got {len(statements)}")
        tree = statements[0]
        if dst == _DEFAULT_TARGET:
            for fixup in SPARK_FIXUPS:
                tree = tree.transform(fixup)
        out = tree.sql(dialect=dst, pretty=True)  # readable multi-line in target/compiled & run
        if not (out or "").strip():
            raise ValueError("transpile produced empty SQL")
        return out

    _orig_compile_code = Compiler._compile_code

    def _config_get(node, key):
        """Read a (possibly custom) config key off a compiled node, defensively.

        dbt stores unknown config keys (set via ``+key`` in dbt_project.yml or ``{{ config(key=…) }}``)
        on the node's config, model-level merged over project-level — so one read yields the effective
        value (per-model override beating the global default).
        """
        cfg = getattr(node, "config", None)
        getter = getattr(cfg, "get", None)
        if callable(getter):
            try:
                return getter(key)
            except Exception:
                return None
        return None

    def _patched_compile_code(self, node, manifest, extra_context=None, *args, **kwargs):
        # Run dbt's real compile first (outside the try — its errors are dbt's, must surface).
        node = _orig_compile_code(self, node, manifest, extra_context, *args, **kwargs)
        src = dst = None
        try:
            src = _config_get(node, "transpile_from")
            dst = _config_get(node, "transpile_to") or _DEFAULT_TARGET
            if not src or src == dst:
                return node  # untouched — not opted in, or already target dialect
            node.compiled_code = _spark_safe_transpile(node.compiled_code or "", src, dst)
        except Exception as e:
            uid = getattr(node, "unique_id", "<unknown>")
            _logger.warning(
                f"[dbt-spark-transpile] could not transpile {uid} from '{src}' -> "
                f"'{dst or _DEFAULT_TARGET}' ({type(e).__name__}: {e}); "
                f"passing model SQL through UNCHANGED."
            )
        return node

    Compiler._compile_code = _patched_compile_code
