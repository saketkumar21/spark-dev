{{ config(materialized='table', file_format='delta', contract={'enforced': true}) }}
select
    cast(order_id as bigint) as order_id,
    cast(amount   as double) as amount,
    cast(status   as string) as status
from {{ ref('fct_orders') }}
