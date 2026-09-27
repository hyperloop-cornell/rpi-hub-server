"""Bench backend for real USB serial devices."""

import asyncio
from typing import Any, Callable, Dict, List, Optional

from src.logging_config import StructuredLogger

from .backend import (
    BenchBackend,
    BenchConnection,
    BenchDevice,
    BenchError,
    FlashRequest,
    new_session_id,
)
from .boards import BoardProfile, BoardRegistry
from .flashing import ArduinoCliFlasher, FirmwareFormatError, OpenOcdFlasher, ToolError, detect_format
from .serial_manager import SerialManager, SerialWriteError
from .usb_port_mapper import DeviceInfo, USBPortMapper

# After a board resets or is flashed it re-enumerates; wait this long for it to come back
REENUMERATION_TIMEOUT_SECONDS = 10.0


class UsbBenchBackend(BenchBackend):
    """Detects USB serial devices, keeps sessions open and performs bench operations."""

    def __init__(
        self,
        hub_id: str,
        mapper: USBPortMapper,
        serial_manager: SerialManager,
        default_baud_rate: int = 9600,
        scan_interval: int = 2,
        registry: Optional[BoardRegistry] = None,
        flasher_factory: Optional[Callable[[Optional[BoardProfile], BenchDevice], Any]] = None,
    ):
        super().__init__()
        self.logger = StructuredLogger(__name__)
        self.hub_id = hub_id
        self.mapper = mapper
        self.serial = serial_manager
        self.default_baud_rate = default_baud_rate
        self.scan_interval = scan_interval
        self.registry = registry or BoardRegistry()
        self.flasher_factory = flasher_factory or default_flasher
        self.flash_formats = self.registry.artifact_formats() or ["ino", "hex"]
        self._flashing: set = set()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.serial.set_data_callback(self._on_serial_data)
        self.serial.set_disconnect_callback(self._on_serial_lost)
        self.mapper.on_device_connected(self._on_mapper_added)
        self.mapper.on_device_disconnected(self._on_mapper_removed)
        await self.serial.start()
        await self.mapper.start(scan_interval=self.scan_interval)

    async def stop(self) -> None:
        await self.mapper.stop()
        await self.serial.stop()

    async def rescan(self) -> None:
        await self.mapper.refresh()

    # ------------------------------------------------------------------ queries

    def _to_device(self, info: DeviceInfo) -> BenchDevice:
        return BenchDevice(
            port_id=info.port_id,
            device_path=info.device_path,
            vendor_id=info.vendor_id,
            product_id=info.product_id,
            serial_number=info.serial_number,
            manufacturer=info.manufacturer,
            product=info.product,
            description=info.description or "",
            location=info.location,
            detected_baud=info.detected_baud,
            board=self.registry.match_device(info),
        )

    def devices(self) -> List[BenchDevice]:
        return [self._to_device(info) for info in self.mapper.port_id_to_device_info.values()]

    def get_device(self, port_id: str) -> Optional[BenchDevice]:
        info = self.mapper.port_id_to_device_info.get(port_id)
        return self._to_device(info) if info else None

    def _to_connection(self, conn) -> BenchConnection:
        return BenchConnection(
            port_id=conn.port_id,
            session_id=conn.session_id,
            device_path=conn.device_path,
            baud_rate=conn.baud_rate,
            connected_at=conn.created_at,
            bytes_read=conn.bytes_read,
            bytes_written=conn.bytes_written,
            status=conn.status.value,
        )

    def connections(self) -> List[BenchConnection]:
        return [self._to_connection(c) for c in self.serial.get_active_connections().values()]

    def get_connection(self, port_id: str) -> Optional[BenchConnection]:
        conn = self.serial.get_connection(port_id)
        return self._to_connection(conn) if conn else None

    # ------------------------------------------------------------------ operations

    def _require_device(self, port_id: str) -> BenchDevice:
        device = self.get_device(port_id)
        if device is None:
            raise BenchError(f"Port not found: {port_id}")
        return device

    def _reset_on_open(self, device: BenchDevice) -> bool:
        return getattr(device.board, "reset_on_open", True) if device.board else True

    async def open(self, port_id: str, baud_rate: Optional[int] = None) -> BenchConnection:
        return await self._open(port_id, baud_rate)

    async def _open(self, port_id: str, baud_rate: Optional[int] = None, force_reset: bool = False) -> BenchConnection:
        device = self._require_device(port_id)
        existing = self.get_connection(port_id)
        if existing:
            return existing

        baud = baud_rate or device.detected_baud or getattr(device.board, "baud", None) or self.default_baud_rate
        session_id = new_session_id()
        opened = await self.serial.open_connection(
            session_id=session_id,
            port_id=port_id,
            device_path=device.device_path,
            baud_rate=baud,
            hub_id=self.hub_id,
            reset_on_open=force_reset or self._reset_on_open(device),
        )
        if not opened:
            raise BenchError(f"Could not open {device.device_path}")

        connection = self.get_connection(port_id)
        if self.listener:
            await self.listener.on_connection_opened(connection, device)
        return connection

    async def close(self, port_id: str) -> None:
        if not await self.serial.close_connection(port_id):
            raise BenchError(f"Port {port_id} is not open")

    async def write(self, port_id: str, data: bytes) -> int:
        try:
            return await self.serial.write_to_port(port_id, data)
        except SerialWriteError as e:
            raise BenchError(str(e)) from e

    async def restart(self, port_id: str) -> BenchConnection:
        device = self._require_device(port_id)
        connection = self.serial.get_connection(port_id)
        baud = connection.baud_rate if connection else None

        if connection:
            await self.serial.close_connection(port_id)
        await asyncio.sleep(0.2)

        method = device.board.reset if device.board else "dtr"
        if method == "openocd":
            try:
                await OpenOcdFlasher(device.board.openocd_config, adapter_serial=device.serial_number).reset()
            except ToolError as e:
                raise BenchError(f"Reset failed: {e}") from e

        # "dtr": reopen with a DTR pulse at the session's own baud rate. Never open at 1200 baud
        # to reset: that "touch" puts native-USB boards (Uno R4, Leonardo) into their bootloader.
        await self._wait_for_device(device)
        return await self._open(port_id, baud, force_reset=method == "dtr")

    def _board_for_flash(self, device: BenchDevice, request: FlashRequest) -> Optional[BoardProfile]:
        if request.board_profile:
            board = self.registry.get(request.board_profile)
            if board is None:
                raise BenchError(f"Unknown board profile: {request.board_profile}")
            return board
        return device.board

    async def flash(self, port_id: str, request: FlashRequest) -> Dict[str, Any]:
        device = self._require_device(port_id)
        try:
            artifact_format = detect_format(request.firmware, request.artifact_format)
        except FirmwareFormatError as e:
            raise BenchError(str(e)) from e

        board = self._board_for_flash(device, request)
        if board is not None:
            if board.flasher == "none":
                raise BenchError(f"{board.name} cannot be flashed in its current mode")
            if board.artifacts and artifact_format not in board.artifacts:
                accepted = ", ".join(f".{a}" for a in board.artifacts)
                raise BenchError(f"{board.name} does not accept .{artifact_format} firmware (accepts {accepted})")
        fqbn = request.board_fqbn or (board.fqbn if board else None)
        flasher = self.flasher_factory(board, device)

        connection = self.serial.get_connection(port_id)
        baud = connection.baud_rate if connection else None
        if connection:
            await self.serial.close_connection(port_id)

        info = self.mapper.port_id_to_device_info.get(port_id)
        suppressed = self.mapper.suppress(info) if info else set()
        self._flashing.add(port_id)
        try:
            await asyncio.sleep(0.5)  # let the port settle
            result = await flasher.flash(device.device_path, request.firmware, artifact_format, fqbn)
        except ToolError as e:
            raise BenchError(str(e)) from e
        finally:
            self._flashing.discard(port_id)
            await self.mapper.release(suppressed)

        # The board restarts into the new firmware; reopen its session
        try:
            await self._wait_for_device(device)
            await self.open(port_id, baud)
        except BenchError as e:
            self.logger.warning("reopen_after_flash_failed", f"Flashed but could not reopen {port_id}: {e}")

        return {
            "port_id": port_id,
            "artifact_format": artifact_format,
            "board_profile": board.id if board else None,
            **result,
        }

    async def _wait_for_device(self, device: BenchDevice) -> None:
        """Wait until the device (by port_id) is present again after a reset."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REENUMERATION_TIMEOUT_SECONDS
        while self.mapper.port_id_to_device_info.get(device.port_id) is None:
            if loop.time() > deadline:
                raise BenchError(f"{device.device_path} did not come back after reset")
            await asyncio.sleep(0.5)
            await self.mapper.refresh()

    # ------------------------------------------------------------------ event plumbing

    async def _on_mapper_added(self, info: DeviceInfo) -> None:
        if info.port_id in self._flashing or not self.listener:
            return
        await self.listener.on_device_added(self._to_device(info))

    async def _on_mapper_removed(self, info: DeviceInfo) -> None:
        if info.port_id in self._flashing:
            return
        if self.serial.get_connection(info.port_id):
            await self.serial.close_connection(info.port_id)
        if self.listener:
            await self.listener.on_device_removed(self._to_device(info))

    def _on_serial_data(self, port_id: str, session_id: str, data: bytes) -> None:
        if self.listener:
            self.listener.on_data(port_id, session_id, data)

    async def _on_serial_lost(self, port_id: str, session_id: str) -> None:
        if self.listener:
            await self.listener.on_connection_lost(port_id, session_id, "serial_error")


def default_flasher(board: Optional[BoardProfile], device: BenchDevice):
    """Pick the flashing tool for a board (arduino-cli unless the board says OpenOCD)."""
    compile_timeout = board.compile_timeout if board else 300.0
    upload_timeout = board.upload_timeout if board else 120.0
    compiler = ArduinoCliFlasher(compile_timeout=compile_timeout, upload_timeout=upload_timeout)
    if board is not None and board.flasher == "openocd":
        if not board.openocd_config:
            raise BenchError(f"{board.name} has no openocd_config in config/boards.yaml")
        return OpenOcdFlasher(
            board.openocd_config, adapter_serial=device.serial_number, compiler=compiler, timeout=upload_timeout
        )
    return compiler
