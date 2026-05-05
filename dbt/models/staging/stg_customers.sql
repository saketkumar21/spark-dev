with source as (

    select * from {{ ref('customers') }}

),

renamed as (

    select
        customer_id,
        name                                    as full_name,
        email,
        phone,
        city,
        state,
        lower(membership_tier)                  as membership_tier,
        cast(join_date as date)                 as joined_at,
        datediff(current_date(), cast(join_date as date)) as tenure_days

    from source

)

select * from renamed
