-- DBT-7: only rows passing every quality rule reach the clean mart.
{{ config(materialized='table', file_format='delta') }}
with src as (select * from {{ ref('stg_orders_quality') }}),
valid_customers as (select customer_id from {{ ref('stg_customers') }})
select s.*
from src s
where s.amount > 0
  and s.status in ('completed', 'refunded', 'pending')
  and s.customer_id in (select customer_id from valid_customers)
