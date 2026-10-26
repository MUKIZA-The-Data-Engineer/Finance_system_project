"""Dagster definitions: the six assets, their checks, one job and a daily schedule.

dagster dev                                   # UI at http://localhost:3000
dagster asset materialize -m orchestration.definitions --select "*"
"""

from dagster import AssetSelection, Definitions, ScheduleDefinition, define_asset_job

from orchestration.assets import ASSETS, CHECKS, ReconPipeSettings

reconcile_job = define_asset_job(
    "reconcile", selection=AssetSelection.all(), description="Raw files to exception report."
)

# Statements land overnight; reconcile before the finance team starts the day.
daily_reconciliation = ScheduleDefinition(
    job=reconcile_job, cron_schedule="0 6 * * *", execution_timezone="Africa/Kigali"
)

defs = Definitions(
    assets=ASSETS,
    asset_checks=CHECKS,
    jobs=[reconcile_job],
    schedules=[daily_reconciliation],
    resources={"reconpipe": ReconPipeSettings()},
)
