-- DBT-4 helper: the snapshot's source. Flip `promote_c001` to 'yes' to change C001's tier,
-- simulating a source change between snapshot runs (so SCD2 versioning is reproducible).
{{ config(materialized='view') }}
select
    customer_id,
    full_name,
    case
        when customer_id = 'C001' and '{{ var("promote_c001", "no") }}' = 'yes' then 'platinum'
        else membership_tier
    end as membership_tier
from {{ ref('stg_customers') }}
