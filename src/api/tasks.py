"""Task endpoints: the same commands the cloud sends, issued locally."""

import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from ..runtime import HubRuntime
from .dependencies import get_runtime
from .models import (
    FlashFirmwareRequest,
    RestartDeviceRequest,
    SerialWriteRequest,
    TaskListResponse,
    TaskResponse,
    TaskStatusResponse,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


async def _submit(
    runtime: HubRuntime, command_type: str, port_id: str, params: Dict[str, Any], priority: int
) -> TaskResponse:
    if runtime.backend.get_device(port_id) is None:
        raise HTTPException(status_code=404, detail=f"Port not found: {port_id}")
    command = {
        "commandId": f"local-{uuid.uuid4()}",
        "commandType": command_type,
        "portId": port_id,
        "params": params,
        "priority": priority,
    }
    try:
        return TaskResponse(**await runtime.commands.handle_command(command))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/write", response_model=TaskResponse)
async def write_to_serial(request: SerialWriteRequest, runtime: HubRuntime = Depends(get_runtime)):
    return await _submit(
        runtime, "serial_write", request.port_id, {"data": request.data, "encoding": request.encoding}, request.priority
    )


@router.post("/flash", response_model=TaskResponse)
async def flash_firmware(request: FlashFirmwareRequest, runtime: HubRuntime = Depends(get_runtime)):
    params: Dict[str, Any] = {"firmwareData": request.firmware_data, "boardFqbn": request.board_fqbn}
    if request.artifact_format:
        params["artifactFormat"] = request.artifact_format
    return await _submit(runtime, "flash", request.port_id, params, request.priority)


@router.post("/restart", response_model=TaskResponse)
async def restart_device(request: RestartDeviceRequest, runtime: HubRuntime = Depends(get_runtime)):
    return await _submit(runtime, "restart", request.port_id, {}, request.priority)


@router.get("/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str, runtime: HubRuntime = Depends(get_runtime)):
    task = runtime.commands.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return TaskStatusResponse(**task.to_dict())


@router.get("", response_model=TaskListResponse)
async def list_tasks(runtime: HubRuntime = Depends(get_runtime)):
    tasks = [TaskStatusResponse(**task) for task in runtime.commands.get_all_tasks()]
    return TaskListResponse(tasks=tasks, count=len(tasks))
