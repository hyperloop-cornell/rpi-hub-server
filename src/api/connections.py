"""Serial connection management endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from ..bench.backend import BenchError
from ..runtime import HubRuntime
from .dependencies import get_runtime
from .models import (
    CloseConnectionResponse,
    ConnectionInfo,
    ConnectionListResponse,
    OpenConnectionRequest,
)

router = APIRouter(prefix="/connections", tags=["connections"])


def _info(connection) -> ConnectionInfo:
    return ConnectionInfo(
        port_id=connection.port_id,
        port=connection.device_path,
        status=connection.status,
        baud_rate=connection.baud_rate,
        session_id=connection.session_id,
        bytes_read=connection.bytes_read,
        bytes_written=connection.bytes_written,
    )


@router.post("", response_model=ConnectionInfo)
async def open_connection(request: OpenConnectionRequest, runtime: HubRuntime = Depends(get_runtime)):
    """Open a serial session (useful for devices the auto-connect policy skipped)."""
    if runtime.backend.get_device(request.port_id) is None:
        raise HTTPException(status_code=404, detail=f"Port not found: {request.port_id}")
    try:
        return _info(await runtime.backend.open(request.port_id, request.baud_rate))
    except BenchError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.delete("/{port_id}", response_model=CloseConnectionResponse)
async def close_connection(port_id: str, runtime: HubRuntime = Depends(get_runtime)):
    try:
        await runtime.backend.close(port_id)
    except BenchError:
        raise HTTPException(status_code=404, detail=f"No active connection for port: {port_id}")
    return CloseConnectionResponse(port_id=port_id, status="closed", message="Connection closed successfully")


@router.get("", response_model=ConnectionListResponse)
async def list_connections(runtime: HubRuntime = Depends(get_runtime)):
    connections = [_info(c) for c in runtime.backend.connections()]
    return ConnectionListResponse(connections=connections, count=len(connections))
