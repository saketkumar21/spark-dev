{% macro surrogate_key(cols) -%}
md5(concat_ws('|'{% for c in cols %}, coalesce(cast({{ c }} as string), '_null_'){% endfor %}))
{%- endmacro %}
