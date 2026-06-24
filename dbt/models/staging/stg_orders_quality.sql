{{ config(materialized='view') }}
select
    cast(order_id as bigint)      as order_id,
    customer_id,
    cast(amount as double)        as amount,
    status,
    cast(ordered_at as timestamp) as ordered_at
from {{ ref('orders_quality_raw') }}
