"""Unit tests for the transpile + fix-up layer. No Spark required (pure sqlglot string checks),
so they run in CI. Importing the package monkeypatches dbt-core's Compiler at import time, which
needs dbt-core installed (a declared dependency); the helper under test is module-level.

Run:  pip install -e ".[test]" && pytest
"""
import pytest
import dbt_spark_transpile as m

transpile = m._spark_safe_transpile  # parse(src) -> fix-ups -> generate(spark)


def test_not_in_subquery_is_not_emitted_as_unsupported_all():
    # sqlglot's Snowflake reader turns NOT IN (subq) into `<> ALL (subq)`, which Spark rejects.
    # The fix-up must rewrite it back to NOT ... IN (subquery).
    out = transpile("select 1 from x where a not in (select a from y)", "snowflake", "spark")
    assert "ALL" not in out.upper()
    assert "NOT" in out.upper() and "IN (" in out.replace("\n", " ").upper().replace("IN(", "IN (")


def test_eq_any_subquery_becomes_in():
    out = transpile("select 1 from x where a = any (select a from y)", "snowflake", "spark")
    assert "ANY" not in out.upper()
    assert "IN" in out.upper()


def test_qualify_is_rewritten_to_subquery():
    out = transpile("select a from x qualify row_number() over (order by a) = 1", "snowflake", "spark")
    assert "QUALIFY" not in out.upper()


def test_common_snowflake_functions_translate():
    out = transpile("select iff(a > 0, 1, 0) c, nvl(b, 'x') d, a::string e from x", "snowflake", "spark")
    up = out.upper()
    assert "IFF(" not in up          # IFF -> IF
    assert "::" not in out           # ::  -> CAST
    assert "CAST(" in up


def test_plain_spark_passthrough_is_valid():
    # A statement valid in both dialects still produces parseable Spark.
    out = transpile("select a, b from x where a = 1", "snowflake", "spark")
    assert "SELECT" in out.upper() and "FROM X" in out.upper()


@pytest.mark.parametrize("bad", ["", "/* only a comment */", "select 1; select 2"])
def test_empty_or_multistatement_raises_so_failsoft_engages(bad):
    # _spark_safe_transpile raises on empty / multi-statement; the compile-phase wrapper catches
    # that and passes the ORIGINAL SQL through unchanged (fail-soft).
    with pytest.raises(Exception):
        transpile(bad, "snowflake", "spark")
