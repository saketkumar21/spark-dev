-- POC for dbt-spark-transpile: this model is written in **Snowflake SQL**.
-- At compile time it is transpiled to Spark SQL (Spark 4.0.2 has no QUALIFY, IFF, etc.):
--   QUALIFY ROW_NUMBER() … = 1   ->  windowed subquery + WHERE
--   IFF(c, a, b)                 ->  IF(c, a, b)
--   x::string                    ->  CAST(x AS STRING)
--   NVL(x, y)                    ->  COALESCE(x, y)
-- Opt-in is the explicit per-model config below (it also inherits the project-level
-- default in dbt_project.yml; either alone is enough).
{{ config(materialized='table', transpile_from='snowflake') }}

select
    customer_id,
    full_name,
    membership_tier,
    iff(tenure_days > 365, 'veteran', 'newcomer')::string as cohort,
    nvl(city, 'unknown')                                  as city
from {{ ref('stg_customers') }}
qualify row_number() over (partition by membership_tier order by joined_at) = 1
