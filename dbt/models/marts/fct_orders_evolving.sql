{{ config(
    materialized='incremental',
    file_format='delta',
    incremental_strategy='append',
    on_schema_change='sync_all_columns'
) }}
select
    order_id,
    amount,
    status
    {% if var("add_tax", false) %}
    , round(amount * 0.08, 2) as tax   -- a NEW column introduced on a later run
    {% endif %}
from {{ ref('stg_orders') }}
{% if is_incremental() %}
where order_id > (select max(order_id) from {{ this }})
{% endif %}
