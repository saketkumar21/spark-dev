{% macro generate_schema_name(custom_schema_name, node) -%}
    {#
        Production-grade schema naming:
        - If a custom schema is specified, use it directly (no prefix).
        - This avoids dbt's default behavior of prepending the target schema,
          which results in names like "analytics_staging" instead of just "staging".
    #}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
