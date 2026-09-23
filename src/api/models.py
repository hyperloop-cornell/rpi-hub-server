"""Pydantic models for the local HTTP API."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "healthy"
    timestamp: datetime
    hub_id: str
    uplink_connected: bool


class DetailedStatusResponse(BaseModel):
    timestamp: str
    uptime_seconds: int
    system: Dict[str, Any]
    service: Dict[str, Any]
    errors: Dict[str, Any]
    profile: Optional[Dict[str, Any]] = None
    uplink: Optional[Dict[str, Any]] = None
    uplink_agent: Dict[str, Any]


class PortInfo(BaseModel):
    port_id: str
    port: str
    description: str = ""
    manufacturer: Optional[str] = None
    serial_number: Optional[str] = None
    vendor_id: Optional[str] = None
    product_id: Optional[str] = None
    detected_baud: Optional[int] = None
    board_profile: Optional[Dict[str, Any]] = None


class PortListResponse(BaseModel):
    ports: List[PortInfo]
    count: int


class PortDetailResponse(BaseModel):
    port_info: PortInfo
    connection_status: str
    baud_rate: Optional[int] = None
    session_id: Optional[str] = None
    bytes_read: Optional[int] = None
    bytes_written: Optional[int] = None


class OpenConnectionRequest(BaseModel):
    port_id: str = Field(..., description="Port ID to connect to")
    baud_rate: Optional[int] = Field(None, description="Baud rate (detected or board default if omitted)")


class ConnectionInfo(BaseModel):
    port_id: str
    port: str
    status: str
    baud_rate: int
    session_id: str
    bytes_read: int
    bytes_written: int


class ConnectionListResponse(BaseModel):
    connections: List[ConnectionInfo]
    count: int


class CloseConnectionResponse(BaseModel):
    port_id: str
    status: str
    message: str


class SerialWriteRequest(BaseModel):
    port_id: str = Field(..., description="Target port ID")
    data: str = Field(..., description="Data to write")
    encoding: Literal["utf-8", "base64"] = Field("utf-8", description="Data encoding")
    priority: int = Field(5, ge=1, le=10, description="Task priority (1=highest)")


class FlashFirmwareRequest(BaseModel):
    port_id: str = Field(..., description="Target port ID")
    firmware_data: str = Field(..., description="Base64 encoded firmware")
    board_fqbn: Optional[str] = Field(None, description="Board FQBN (required for .ino)")
    artifact_format: Optional[Literal["ino", "hex", "bin", "elf"]] = Field(None, description="Firmware format")
    priority: int = Field(3, ge=1, le=10, description="Task priority")


class RestartDeviceRequest(BaseModel):
    port_id: str = Field(..., description="Target port ID")
    priority: int = Field(2, ge=1, le=10, description="Task priority")


class TaskResponse(BaseModel):
    task_id: str
    command_id: str
    status: str
    queued: bool


class TaskStatusResponse(BaseModel):
    task_id: str
    command_type: str
    port_id: Optional[str]
    status: str
    priority: int
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class TaskListResponse(BaseModel):
    tasks: List[TaskStatusResponse]
    count: int
