from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.dependencies import CurrentUser
from app.core.openapi import error_responses
from app.schemas.analytics import AnalyticsFilters, AnalyticsResult
from app.services.analytics import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["Analytics"], responses=error_responses(401, 403))


@router.get("", response_model=AnalyticsResult, responses=error_responses(422))
async def analyze(
    request: Request, principal: CurrentUser, filters: Annotated[AnalyticsFilters, Query()]
) -> AnalyticsResult:
    """Committed rows; [start_time, end_time), Taiwan UTC+8 buckets, at most 1000 groups each.

    Both sources are available to authenticated readers. Use created_by for a
    specific user's manually created/imported records. Use category to select a
    record device, or device_id to select a system-generated measurement device.
    System history also supports event_id and is_anomaly across summary and export.
    Category statistics group by records.category or realtime_samples.device_id;
    for realtime, their category field contains the device ID, not the metric name.
    Missing buckets are omitted, not filled with fabricated zero measurements.
    Decimal results are serialized as strings. Empty totals are zero; empty
    averages and extrema are null. MariaDB computes AVG at decimal precision.
    """
    return await AnalyticsService(request.app.state.session_factory).analyze(principal, filters)
