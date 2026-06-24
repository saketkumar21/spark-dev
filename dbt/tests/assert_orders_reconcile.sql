-- SINGULAR test: every raw quality row is either clean or quarantined (none lost/duplicated).
with raw as (select count(*) as n from {{ ref('stg_orders_quality') }}),
     parts as (
        select (select count(*) from {{ ref('orders_clean') }})
             + (select count(*) from {{ ref('orders_quarantine') }}) as n
     )
select 'reconcile_mismatch' as failure
from raw join parts on true
where raw.n != parts.n
