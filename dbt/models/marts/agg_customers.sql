with customers as (

    select * from {{ ref('stg_customers') }}

)

SELECT 
    *
from customers
QUALIFY ROW_NUMBER() OVER(PARTITION BY membership_tier ORDER BY joined_at) = 1
