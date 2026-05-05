with customers as (

    select * from {{ ref('stg_customers') }}

),

enriched as (

    select
        customer_id,
        full_name,
        email,
        phone,
        city,
        state,

        -- Regional grouping
        case
            when state in ('CA', 'OR', 'WA', 'AZ', 'NV')        then 'West'
            when state in ('TX', 'FL', 'GA', 'NC', 'TN', 'VA')  then 'South'
            when state in ('NY', 'PA', 'MA', 'NJ', 'CT')        then 'Northeast'
            when state in ('IL', 'OH', 'MN', 'CO', 'MI', 'IN')  then 'Midwest'
            else 'Other'
        end as region,

        -- Tier with explicit rank for sorting/filtering
        membership_tier,
        case membership_tier
            when 'platinum' then 4
            when 'gold'     then 3
            when 'silver'   then 2
            when 'bronze'   then 1
        end as tier_rank,

        -- Tenure segmentation
        joined_at,
        tenure_days,
        case
            when tenure_days >= 1825 then 'veteran'     -- 5+ years
            when tenure_days >= 1095 then 'loyal'       -- 3-5 years
            when tenure_days >=  365 then 'established' -- 1-3 years
            else 'new'                                  -- < 1 year
        end as tenure_segment,

        current_date() as _loaded_at

    from customers

)

select * from enriched
