"""
RPi Hub Service entry point.

    python -m src.main          (uses api.host/api.port from config; recommended)
    uvicorn src.main:app        (host/port from the uvicorn command line)

Configuration: config/config.yaml plus the profile named by HUB_PROFILE (see src/config.py).
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import connections_router, health_router, ports_router, tasks_router
from .config import get_settings
from .logging_config import get_logger, setup_logging
from .runtime import HubRuntime

settings = get_settings()
setup_logging(settings.logging.level, settings.logging.format)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.hub.device_token:
        logger.warning("DEVICE_TOKEN is not set - the cloud will reject this hub", extra={"hub_id": settings.hub.hub_id})

    runtime = HubRuntime(settings)
    app.state.runtime = runtime
    await runtime.start()
    logger.info(
        "RPi Hub Service started",
        extra={"hub_id": settings.hub.hub_id, "server_endpoint": settings.hub.server_endpoint},
    )
    try:
        yield
    finally:
        logger.info("Shutting down RPi Hub Service")
        await runtime.stop()
        app.state.runtime = None


app = FastAPI(
    title="RPi Hub Service",
    description="Raspberry Pi hub service: USB serial MCUs to the cloud telemetry service",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.api.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", extra={"method": request.method, "url": str(request.url)}, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc), "timestamp": datetime.now(timezone.utc).isoformat()},
    )


app.include_router(health_router)
app.include_router(ports_router)
app.include_router(connections_router)
app.include_router(tasks_router)


@app.get("/")
async def root():
    return {
        "service": "RPi Hub Service",
        "version": "1.1.0",
        "hub_id": settings.hub.hub_id,
        "profile": settings.hub.profile_name or None,
        "status": "running",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.api.host,
        port=settings.api.port,
        log_level=settings.logging.level.lower(),
    )
