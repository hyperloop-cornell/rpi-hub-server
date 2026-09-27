"""Bench backend interface: where serial devices come from.

`UsbBenchBackend` talks to real USB MCUs; `SimBenchBackend` fakes them. The runtime, the
command tasks and the local API only use this interface, so a simulated hub goes through
exactly the same uplink and protocol code as a real one.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class BenchDevice:
    """A serial device attached to the hub."""

    port_id: str
    device_path: str
    vendor_id: Optional[str] = None
    product_id: Optional[str] = None
    serial_number: Optional[str] = None
    manufacturer: Optional[str] = None
    product: Optional[str] = None
    description: str = ""
    location: Optional[str] = None
    detected_baud: Optional[int] = None
    board: Optional[Any] = None  # BoardProfile when the device matches the board registry

    def board_summary(self) -> Optional[Dict[str, Any]]:
        if self.board is None:
            return None
        return self.board.summary()


@dataclass
class BenchConnection:
    """An open serial session on a device."""

    port_id: str
    session_id: str
    device_path: str
    baud_rate: int
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    bytes_read: int = 0
    bytes_written: int = 0
    status: str = "connected"


@dataclass
class FlashRequest:
    """Firmware to program onto a device."""

    firmware: bytes
    artifact_format: str  # ino | hex | bin | elf
    board_fqbn: Optional[str] = None
    board_profile: Optional[str] = None


class BenchListener(Protocol):
    """Receives backend events (implemented by HubRuntime)."""

    async def on_device_added(self, device: BenchDevice) -> None: ...

    async def on_device_removed(self, device: BenchDevice) -> None: ...

    async def on_connection_opened(self, connection: BenchConnection, device: BenchDevice) -> None: ...

    async def on_connection_lost(self, port_id: str, session_id: str, reason: str) -> None: ...

    def on_data(self, port_id: str, session_id: str, data: bytes) -> None: ...


class BenchError(RuntimeError):
    """A bench operation failed (reported to the GUI as the task error)."""


def new_session_id() -> str:
    return f"session-{uuid.uuid4().hex[:12]}"


class BenchBackend(ABC):
    """Source of serial devices and the operations the hub can perform on them."""

    #: Firmware formats this backend can flash (advertised as flash:<format> capabilities)
    flash_formats: List[str] = ["ino", "hex"]

    def __init__(self) -> None:
        self.listener: Optional[BenchListener] = None

    def set_listener(self, listener: BenchListener) -> None:
        self.listener = listener

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def devices(self) -> List[BenchDevice]: ...

    def get_device(self, port_id: str) -> Optional[BenchDevice]:
        return next((d for d in self.devices() if d.port_id == port_id), None)

    @abstractmethod
    def connections(self) -> List[BenchConnection]: ...

    def get_connection(self, port_id: str) -> Optional[BenchConnection]:
        return next((c for c in self.connections() if c.port_id == port_id), None)

    @abstractmethod
    async def open(self, port_id: str, baud_rate: Optional[int] = None) -> BenchConnection:
        """Open a serial session (raises BenchError on failure)."""

    @abstractmethod
    async def close(self, port_id: str) -> None:
        """Close the session on a port (raises BenchError if none is open)."""

    @abstractmethod
    async def write(self, port_id: str, data: bytes) -> int:
        """Write bytes to an open port and return how many were written."""

    @abstractmethod
    async def restart(self, port_id: str) -> BenchConnection:
        """Reset the device and reopen its session."""

    @abstractmethod
    async def flash(self, port_id: str, request: FlashRequest) -> Dict[str, Any]:
        """Program firmware and reopen the session afterwards."""

    async def rescan(self) -> None:
        """Re-detect devices now (optional)."""

    def capabilities(self) -> List[str]:
        return ["bench", *(f"flash:{fmt}" for fmt in self.flash_formats)]
