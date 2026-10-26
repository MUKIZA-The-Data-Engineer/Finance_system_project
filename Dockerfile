# ReconPipe: run the whole reconciliation in a container.
#   docker compose up      -> reconcile data/raw/, then serve the review app on :8501
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The package and its dependencies (incl. the review app's Streamlit).
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[app]"

# Everything a run needs besides the package: dbt project, app, inputs, ground truth.
COPY dbt ./dbt
COPY app ./app
COPY data/raw ./data/raw
COPY tests/expected_results.json ./tests/expected_results.json

ENV RECONPIPE_DB=/app/warehouse/warehouse.duckdb
RUN mkdir -p warehouse reports data/quarantine

EXPOSE 8501
CMD ["python", "-m", "reconpipe"]
