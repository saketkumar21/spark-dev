{% test non_negative(model, column_name) %}
-- a custom GENERIC test: fails for any row where the column is negative
select * from {{ model }} where {{ column_name }} < 0
{% endtest %}
