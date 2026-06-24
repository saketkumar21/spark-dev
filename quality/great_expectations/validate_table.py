"""DBT-8 — standalone Great Expectations validation of a Spark/Delta/Iceberg table.

GE's Spark execution engine does not work over Spark Connect, so we read the table to a
pandas DataFrame (toPandas is Connect-safe for the small teaching tables) and validate that.
This is exactly where GE complements dbt tests: statistical / distribution / profiling checks
and standalone validation, decoupled from the dbt run.

Run:  PYTHONPATH=<repo-root> uv run python quality/great_expectations/validate_table.py [fqtn]
"""
import sys
import great_expectations as gx
from common.spark_session import spark

TABLE = sys.argv[1] if len(sys.argv) > 1 else "spark_catalog.marts.orders_clean"
pdf = spark.table(TABLE).toPandas()
print(f"Validating {TABLE}: {len(pdf)} rows, {len(pdf.columns)} cols")

context = gx.get_context()  # ephemeral
batch = (context.data_sources.add_pandas("spark_extract")
         .add_dataframe_asset(name="asset")
         .add_batch_definition_whole_dataframe("batch")
         .get_batch(batch_parameters={"dataframe": pdf}))

suite = gx.ExpectationSuite(name="orders_quality")
for exp in [
    gx.expectations.ExpectColumnValuesToNotBeNull(column="order_id"),
    gx.expectations.ExpectColumnValuesToBeUnique(column="order_id"),
    gx.expectations.ExpectColumnValuesToBeBetween(column="amount", min_value=0, max_value=100000),
    gx.expectations.ExpectColumnValuesToBeInSet(column="status",
                                                value_set=["completed", "refunded", "pending"]),
]:
    suite.add_expectation(exp)

result = batch.validate(suite)
print(f"\nGE validation success: {result.success}")
for r in result.results:
    print(f"  {'PASS' if r.success else 'FAIL'}  {r.expectation_config.type}")
sys.exit(0 if result.success else 1)
