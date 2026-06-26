{% macro generate_schema_name(custom_schema_name, node) -%}
    {#
        Format-driven catalog routing (the "schema-string trick").

        dbt-spark renders a relation as `schema.identifier` and forbids the
        `database` field, so we route a model to the catalog that matches its
        `file_format` by prepending the catalog onto the schema:
            file_format='delta'   -> spark_catalog.<schema>.<table>
            file_format='iceberg' -> iceberg_catalog.<schema>.<table>
            file_format='hudi'    -> hudi_catalog.<schema>.<table>   (once installed)
        The user only sets `file_format`; the table lands in the right catalog.

        Base schema resolution keeps the repo's convention: a custom schema is used
        directly (no target-schema prefix). If the resolved schema already names a
        catalog (contains a dot), or the file_format isn't mapped (views, seeds with
        no format, etc.), it is left untouched — so existing models are unaffected.
    #}
    {%- set format_to_catalog = {
        'delta': 'spark_catalog',
        'iceberg': 'iceberg_catalog',
        'hudi': 'hudi_catalog'
    } -%}

    {%- set base_schema = (custom_schema_name | trim) if custom_schema_name is not none else target.schema -%}
    {%- set file_format = node.config.get('file_format') if node is not none else none -%}
    {# spark_catalog is set as default catalog #}
    {%- set catalog = format_to_catalog.get(file_format, 'spark_catalog') -%}

    {%- if '.' not in base_schema -%}
        {{ catalog ~ '.' ~ base_schema }}
    {%- else -%}
        {{ base_schema }}
    {%- endif -%}
{%- endmacro %}
