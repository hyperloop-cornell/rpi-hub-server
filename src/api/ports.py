"""Port management endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from ..runtime import HubRuntime
from .dependencies import get_runtime, port_info
from .models import PortDetailResponse, PortListResponse

router = APIRouter(prefix="/ports", tags=["ports"])


@router.get("", response_model=PortListResponse)
async def list_ports(runtime: HubRuntime = Depends(get_runtime)):
    """List detected serial devices (including ones not opened by the auto-connect policy)."""
    ports = [port_info(device) for device in runtime.backend.devices()]
    return PortListResponse(ports=ports, count=len(ports))


@router.post("/scan")
async def scan_ports(runtime: HubRuntime = Depends(get_runtime)):
    """Re-detect devices now instead of waiting for the next periodic scan."""
    await runtime.backend.rescan()
    return {"message": "Port scan completed", "detected_count": len(runtime.backend.devices())}


@router.get("/{port_id}", response_model=PortDetailResponse)
async def get_port_details(port_id: str, runtime: HubRuntime = Depends(get_runtime)):
    device = runtime.backend.get_device(port_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Port not found: {port_id}")

    connection = runtime.backend.get_connection(port_id)
    if connection is None:
        return PortDetailResponse(port_info=port_info(device), connection_status="disconnected")
    return PortDetailResponse(
        port_info=port_info(device),
        connection_status=connection.status,
        baud_rate=connection.baud_rate,
        session_id=connection.session_id,
        bytes_read=connection.bytes_read,
        bytes_written=connection.bytes_written,
    )
