"""Health check endpoints."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from ..runtime import HubRuntime
from .dependencies import get_runtime
from .models import DetailedStatusResponse, HealthResponse

router = APIRouter(prefix="", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(runtime: HubRuntime = Depends(get_runtime)):
    """Liveness check (also reports whether the cloud uplink is connected)."""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc),
        hub_id=runtime.settings.hub.hub_id,
        uplink_connected=runtime.agent.is_connected,
    )


@router.get("/status", response_model=DetailedStatusResponse)
async def detailed_status(runtime: HubRuntime = Depends(get_runtime)):
    """Everything the hub reports in its health messages, plus uplink agent state."""
    metrics = await runtime.health.collect_health_metrics()
    return DetailedStatusResponse(**metrics, uplink_agent=runtime.agent.get_connection_status())
