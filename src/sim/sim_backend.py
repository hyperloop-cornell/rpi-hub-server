"""Simulated bench backend: fake boards that behave like real ones over the real uplink."""

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.bench.backend import (
    BenchBackend,
    BenchConnection,
    BenchDevice,
    BenchError,
    FlashRequest,
    new_session_id,
)
from src.bench.boards import BoardRegistry
from src.bench.flashing import FirmwareFormatError, detect_format
from src.logging_config import StructuredLogger

from .generators import GENERATORS


@dataclass
class SimDeviceSpec:
    """A simulated board, as listed under bench.sim_devices in a config profile."""

    name: str
    generator: str = "dht22"
    baud: int = 9600
    vendor_id: str = "2341"
    product_id: str = "0043"
    product: str = "Arduino Uno"
    manufacturer: str = "Arduino (www.arduino.cc)"
    interval_ms: int = 500
    flash_seconds: float = 3.0
    board: Optional[str] = None  # board registry id, when the registry is available
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, raw: Dict[str, Any]) -> "SimDeviceSpec":
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw and k != "extra"}
        return cls(**known, extra={k: v for k, v in raw.items() if k not in known})


DEFAULT_SIM_DEVICES = [
    SimDeviceSpec(name="sim-uno", generator="dht22"),
    SimDeviceSpec(
        name="sim-mega",
        generator="mpu6050",
        baud=115200,
        product_id="0042",
        product="Arduino Mega 2560",
    ),
    SimDeviceSpec(
        name="sim-nano",
        generator="voltage",
        vendor_id="1a86",
        product_id="7523",
        product="USB Serial (CH340)",
        manufacturer="QinHeng Electronics",
    ),
]


class SimBenchBackend(BenchBackend):
    """Serves fake devices through the BenchBackend interface.

    Supports every bench operation: sessions open with a fresh session_id, writes are counted,
    restart and flash take realistic time and reopen the session, and devices can be
    unplugged/replugged from tests to exercise hotplug handling.
    """

    flash_formats = ["ino", "hex", "bin", "elf"]

    def __init__(
        self,
        hub_id: str,
        specs: Optional[List[SimDeviceSpec]] = None,
        registry: Optional[BoardRegistry] = None,
        speedup: float = 1.0,
    ):
        super().__init__()
        self.logger = StructuredLogger(__name__)
        self.hub_id = hub_id
        self.specs = specs if specs is not None else list(DEFAULT_SIM_DEVICES)
        self.registry = registry
        if registry is not None and registry.artifact_formats():
            self.flash_formats = registry.artifact_formats()
        self.speedup = max(speedup, 0.001)
        self._devices: Dict[str, BenchDevice] = {}
        self._specs_by_port: Dict[str, SimDeviceSpec] = {}
        self._connections: Dict[str, BenchConnection] = {}
        self._streams: Dict[str, asyncio.Task] = {}
        self._started_at = time.monotonic()

    # ------------------------------------------------------------------ lifecycle

    def _port_id(self, spec: SimDeviceSpec) -> str:
        digest = hashlib.sha256(f"{self.hub_id}:{spec.name}".encode()).hexdigest()[:8]
        return f"port_{digest}"

    def _make_device(self, index: int, spec: SimDeviceSpec) -> BenchDevice:
        device = BenchDevice(
            port_id=self._port_id(spec),
            device_path=f"/dev/sim{index}",
            vendor_id=spec.vendor_id,
            product_id=spec.product_id,
            serial_number=f"SIM{hashlib.sha256(spec.name.encode()).hexdigest()[:12].upper()}",
            manufacturer=spec.manufacturer,
            product=spec.product,
            description=f"{spec.product} (simulated)",
            location=f"sim-{index}",
            detected_baud=spec.baud,
        )
        if self.registry is not None:
            device.board = self.registry.get(spec.board) or self.registry.match_device(device)
        return device

    async def start(self) -> None:
        self._started_at = time.monotonic()
        for index, spec in enumerate(self.specs):
            await self.plug(spec, index)

    async def stop(self) -> None:
        for port_id in list(self._connections):
            await self._close(port_id)

    async def plug(self, spec: SimDeviceSpec, index: Optional[int] = None) -> BenchDevice:
        """Attach a simulated device (as if plugged into USB)."""
        index = len(self._devices) if index is None else index
        device = self._make_device(index, spec)
        self._devices[device.port_id] = device
        self._specs_by_port[device.port_id] = spec
        if self.listener:
            await self.listener.on_device_added(device)
        return device

    async def unplug(self, port_id: str) -> None:
        """Detach a simulated device (as if pulled from USB)."""
        device = self._devices.pop(port_id, None)
        if device is None:
            raise BenchError(f"Port not found: {port_id}")
        await self._close(port_id)
        if self.listener:
            await self.listener.on_device_removed(device)

    # ------------------------------------------------------------------ queries

    def devices(self) -> List[BenchDevice]:
        return list(self._devices.values())

    def connections(self) -> List[BenchConnection]:
        return list(self._connections.values())

    # ------------------------------------------------------------------ operations

    def _require_device(self, port_id: str) -> BenchDevice:
        device = self._devices.get(port_id)
        if device is None:
            raise BenchError(f"Port not found: {port_id}")
        return device

    async def open(self, port_id: str, baud_rate: Optional[int] = None) -> BenchConnection:
        device = self._require_device(port_id)
        if port_id in self._connections:
            return self._connections[port_id]

        spec = self._specs_by_port[port_id]
        connection = BenchConnection(
            port_id=port_id,
            session_id=new_session_id(),
            device_path=device.device_path,
            baud_rate=baud_rate or spec.baud,
        )
        self._connections[port_id] = connection
        self._streams[port_id] = asyncio.create_task(self._stream(connection, spec))
        if self.listener:
            await self.listener.on_connection_opened(connection, device)
        return connection

    async def _close(self, port_id: str) -> bool:
        stream = self._streams.pop(port_id, None)
        if stream:
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)
        return self._connections.pop(port_id, None) is not None

    async def close(self, port_id: str) -> None:
        if not await self._close(port_id):
            raise BenchError(f"Port {port_id} is not open")

    async def write(self, port_id: str, data: bytes) -> int:
        connection = self._connections.get(port_id)
        if connection is None:
            raise BenchError(f"Port {port_id} is not open")
        connection.bytes_written += len(data)
        # Echo back like the "Serial Echo" preset so writes are visible in the console
        self._emit(connection, b"ECHO: " + data.rstrip(b"\r\n") + b"\n")
        return len(data)

    async def restart(self, port_id: str) -> BenchConnection:
        self._require_device(port_id)
        await self._close(port_id)
        await asyncio.sleep(1.5 / self.speedup)
        return await self.open(port_id)

    async def flash(self, port_id: str, request: FlashRequest) -> Dict[str, Any]:
        device = self._require_device(port_id)
        try:
            artifact_format = detect_format(request.firmware, request.artifact_format)
        except FirmwareFormatError as e:
            raise BenchError(str(e)) from e

        board = device.board
        if request.board_profile and self.registry is not None:
            board = self.registry.get(request.board_profile)
            if board is None:
                raise BenchError(f"Unknown board profile: {request.board_profile}")
        if board is not None and board.flasher == "none":
            raise BenchError(f"{board.name} cannot be flashed in its current mode")
        if board is not None and board.artifacts and artifact_format not in board.artifacts:
            raise BenchError(f"{board.name} does not accept .{artifact_format} firmware")
        if artifact_format == "ino" and not (request.board_fqbn or getattr(board, "fqbn", None)):
            raise BenchError("Board FQBN is required for compiling .ino source files")

        spec = self._specs_by_port[port_id]
        await self._close(port_id)
        started = time.monotonic()
        await asyncio.sleep(spec.flash_seconds / self.speedup)
        await self.open(port_id)
        return {
            "port_id": port_id,
            "artifact_format": artifact_format,
            "board_fqbn": request.board_fqbn or getattr(board, "fqbn", None),
            "board_profile": board.id if board else None,
            "flash_duration_ms": int((time.monotonic() - started) * 1000),
            "output": "Simulated flash complete",
        }

    # ------------------------------------------------------------------ data

    def _emit(self, connection: BenchConnection, data: bytes) -> None:
        connection.bytes_read += len(data)
        if self.listener:
            self.listener.on_data(connection.port_id, connection.session_id, data)

    async def _stream(self, connection: BenchConnection, spec: SimDeviceSpec) -> None:
        generator = GENERATORS.get(spec.generator, GENERATORS["dht22"])
        interval = max(spec.interval_ms, 10) / 1000 / self.speedup
        try:
            while True:
                await asyncio.sleep(interval)
                t = time.monotonic() - self._started_at
                self._emit(connection, (generator(t) + "\n").encode())
        except asyncio.CancelledError:
            pass
