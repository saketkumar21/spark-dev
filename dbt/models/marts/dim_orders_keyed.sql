-- DBT-10: idempotent surrogate key via a reusable macro (deterministic md5 of the business key).
{{ config(materialized='view') }}
select
    {{ surrogate_key(['order_id', 'customer_id']) }} as order_key,
    order_id, customer_id, amount, status
from {{ ref('fct_orders') }}
