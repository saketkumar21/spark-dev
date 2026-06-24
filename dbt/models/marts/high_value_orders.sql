-- DBT-1: a view consumer of the ephemeral model (the ephemeral CTE is inlined here).
{{ config(materialized='view') }}
select * from {{ ref('int_high_value_orders') }}

