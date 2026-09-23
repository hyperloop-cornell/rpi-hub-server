"""Shared helpers for the local API routers."""

from fastapi import HTTPException, Request

from ..bench.backend import BenchDevice
from ..runtime import HubRuntime
from .models import PortInfo


def get_runtime(request: Request) -> HubRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Hub runtime is not running")
    return runtime


def port_info(device: BenchDevice) -> PortInfo:
    return PortInfo(
        port_id=device.port_id,
        port=device.device_path,
        description=device.description or "",
        manufacturer=device.manufacturer,
        serial_number=device.serial_number,
        vendor_id=device.vendor_id,
        product_id=device.product_id,
        detected_baud=device.detected_baud,
        board_profile=device.board_summary(),
    )
