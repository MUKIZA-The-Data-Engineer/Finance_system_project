-- Custom generic test: a zero-value line is not a transaction.
-- Mirrors the amount_non_zero rule in src/reconpipe/validate/contracts.py, so a
-- row that slipped past Python validation still fails the build.
{% test not_zero(model, column_name) %}
select *
from {{ model }}
where {{ column_name }} = 0
{% endtest %}
