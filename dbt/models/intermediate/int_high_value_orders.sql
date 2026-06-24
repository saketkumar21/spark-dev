-- DBT-1: ephemeral — compiled as a CTE into downstream models, never materialized as its own object.
{{ config(materialized='ephemeral') }}
select order_id, customer_id, amount, status
from {{ ref('fct_orders') }}
where amount > 100
