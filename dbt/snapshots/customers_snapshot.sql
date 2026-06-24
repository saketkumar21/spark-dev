{% snapshot customers_snapshot %}
{{ config(
    target_schema='snapshots',
    unique_key='customer_id',
    strategy='check',
    check_cols=['membership_tier'],
    file_format='delta'
) }}
select customer_id, full_name, membership_tier from {{ ref('snap_customers_src') }}
{% endsnapshot %}
