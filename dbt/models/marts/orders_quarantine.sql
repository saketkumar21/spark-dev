-- DBT-7: rows failing a rule are ROUTED here (not dropped, not build-failing) with a reason,
-- so the pipeline keeps moving and bad data is triaged out-of-band.
{{ config(materialized='table', file_format='delta') }}
with src as (select * from {{ ref('stg_orders_quality') }}),
valid_customers as (select customer_id from {{ ref('stg_customers') }})
select
    s.*,
    case
        when s.amount <= 0 then 'non_positive_amount'
        when s.status not in ('completed','refunded','pending') then 'invalid_status'
        when s.customer_id not in (select customer_id from valid_customers) then 'orphan_customer'
    end as quarantine_reason
from src s
where s.amount <= 0
   or s.status not in ('completed','refunded','pending')
   or s.customer_id not in (select customer_id from valid_customers)
