"""Serial Manager: opens USB serial ports, reads them continuously and writes to them."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Optional

import serial

from src.logging_config import StructuredLogger


class ConnectionStatus(Enum):
    """Connection status enumeration."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class SerialWriteError(RuntimeError):
    """Writing to a serial port failed."""


@dataclass
class Connection:
    """Serial connection information."""

    hub_id: str
    session_id: str
    port_id: str
    device_path: str
    baud_rate: int
    data_bits: int = 8
    stop_bits: int = 1
    parity: str = "N"  # N, E, O, M, S
    timeout: float = 1.0
    serial_port: Optional[serial.Serial] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: ConnectionStatus = ConnectionStatus.DISCONNECTED
    error_count: int = 0
    bytes_read: int = 0
    bytes_written: int = 0

    def get_device_path(self) -> str:
        return self.device_path

    def get_baud_rate(self) -> int:
        return self.baud_rate

    def is_open(self) -> bool:
        return (
            self.serial_port is not None
            and self.serial_port.is_open
            and self.status == ConnectionStatus.CONNECTED
        )

    def update_activity(self) -> None:
        self.last_activity_at = datetime.now(timezone.utc)


class SerialManager:
    """Manages serial port connections and operations."""

    def __init__(
        self,
        max_connections: int = 10,
        connection_retry_attempts: int = 3,
        default_timeout: float = 1.0,
        task_queue_size: Optional[int] = None,  # accepted for config compatibility; unused
    ):
        self.logger = StructuredLogger(__name__)
        self.max_connections = max_connections
        self.connection_retry_attempts = max(1, connection_retry_attempts)
        self.default_timeout = default_timeout

        self.active_connections: Dict[str, Connection] = {}
        self._reader_tasks: Dict[str, asyncio.Task] = {}
        self._running = False

        # Callback(port_id, session_id, data: bytes); may be sync or async
        self._data_callback: Optional[Callable] = None
        # Callback(port_id, session_id); called when a read fails (device gone or broken)
        self._disconnect_callback: Optional[Callable] = None

    async def start(self) -> None:
        self._running = True
        self.logger.info("serial_manager_started", "Serial Manager started")

    async def stop(self) -> None:
        self._running = False
        for port_id in list(self.active_connections.keys()):
            await self.close_connection(port_id)
        self.logger.info("serial_manager_stopped", "Serial Manager stopped")

    def set_data_callback(self, callback: Callable) -> None:
        self._data_callback = callback

    def set_disconnect_callback(self, callback: Callable) -> None:
        self._disconnect_callback = callback

    async def open_connection(
        self,
        session_id: str,
        port_id: str,
        device_path: str,
        baud_rate: int,
        hub_id: str = "",
        reset_on_open: bool = True,
        **kwargs: Any,
    ) -> bool:
        """Open a serial connection and start reading it.

        Args:
            reset_on_open: Pulse DTR after opening so classic Arduinos restart their sketch.
                Boards with native USB (Uno R4, Leonardo) or an ST-LINK bridge do not need it.
        """
        existing = self.active_connections.get(port_id)
        if existing and existing.is_open():
            self.logger.warning(
                "connection_already_open",
                f"Connection already open on {port_id}",
                port_id=port_id,
                session_id=session_id,
            )
            return True
        if existing:
            await self.close_connection(port_id)

        if len(self.active_connections) >= self.max_connections:
            self.logger.error(
                "max_connections_reached",
                f"Maximum connections ({self.max_connections}) reached",
                max_connections=self.max_connections,
            )
            return False

        connection = Connection(
            hub_id=hub_id,
            session_id=session_id,
            port_id=port_id,
            device_path=device_path,
            baud_rate=baud_rate,
            data_bits=kwargs.get("data_bits", 8),
            stop_bits=kwargs.get("stop_bits", 1),
            parity=kwargs.get("parity", "N"),
            timeout=kwargs.get("timeout", self.default_timeout),
        )

        self.logger.info(
            "connection_requested",
            f"Opening connection on {port_id}",
            session_id=session_id,
            port_id=port_id,
            device_path=device_path,
            baud_rate=baud_rate,
        )

        loop = asyncio.get_running_loop()
        for attempt in range(1, self.connection_retry_attempts + 1):
            try:
                connection.status = ConnectionStatus.CONNECTING
                ser = await loop.run_in_executor(
                    None,
                    lambda: serial.Serial(
                        port=device_path,
                        baudrate=baud_rate,
                        bytesize=connection.data_bits,
                        stopbits=connection.stop_bits,
                        parity=connection.parity,
                        timeout=connection.timeout,
                    ),
                )
                ser.reset_input_buffer()

                if reset_on_open:
                    ser.dtr = False
                    await asyncio.sleep(0.1)
                    ser.dtr = True
                    await asyncio.sleep(2)  # let the bootloader hand over to the sketch

                connection.serial_port = ser
                connection.status = ConnectionStatus.CONNECTED
                self.active_connections[port_id] = connection
                self._reader_tasks[port_id] = asyncio.create_task(self._read_continuously(connection))

                self.logger.connection_opened(
                    session_id,
                    port_id,
                    device_path=device_path,
                    baud_rate=baud_rate,
                    attempt=attempt,
                )
                return True

            except (serial.SerialException, OSError) as e:
                connection.status = ConnectionStatus.ERROR
                connection.error_count += 1
                if attempt < self.connection_retry_attempts:
                    delay = 2 ** (attempt - 1)
                    self.logger.info(
                        "connection_retry",
                        f"Connection failed on {port_id}, retrying in {delay}s",
                        port_id=port_id,
                        attempt=attempt,
                        next_delay_s=delay,
                        error=str(e),
                    )
                    await asyncio.sleep(delay)
                else:
                    self.logger.connection_failed(port_id, attempt, str(e), device_path=device_path)

            except Exception as e:
                connection.status = ConnectionStatus.ERROR
                self.logger.error(
                    "connection_error",
                    f"Unexpected error opening {port_id}: {e}",
                    port_id=port_id,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                break

        self.logger.error(
            "connection_abandoned",
            f"Abandoned connection attempts on {port_id}",
            port_id=port_id,
            attempts=self.connection_retry_attempts,
        )
        return False

    async def close_connection(self, port_id: str) -> bool:
        """Close a connection. Safe to call from the connection's own reader task."""
        connection = self.active_connections.pop(port_id, None)
        reader = self._reader_tasks.pop(port_id, None)
        if connection is None:
            return False

        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass

        try:
            if connection.serial_port and connection.serial_port.is_open:
                connection.serial_port.close()
        except Exception as e:
            self.logger.warning("connection_close_error", f"Error closing {port_id}: {e}", port_id=port_id, error=str(e))

        connection.status = ConnectionStatus.DISCONNECTED
        self.logger.info(
            "connection_closed",
            f"Connection {port_id} closed",
            port_id=port_id,
            session_id=connection.session_id,
        )
        return True

    async def _read_continuously(self, connection: Connection) -> None:
        port_id = connection.port_id
        loop = asyncio.get_running_loop()

        while self._running and connection.is_open():
            try:
                data = await loop.run_in_executor(None, connection.serial_port.read, 1024)
            except asyncio.CancelledError:
                return
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                # TypeError/AttributeError: the port was closed underneath the read
                if connection.status != ConnectionStatus.CONNECTED:
                    return
                self.logger.error(
                    "serial_read_error",
                    f"Serial read error on {port_id}: {e}",
                    port_id=port_id,
                    error=str(e),
                )
                connection.status = ConnectionStatus.ERROR
                connection.error_count += 1
                await self.close_connection(port_id)
                self._notify_disconnect(port_id, connection.session_id)
                return

            if not data:
                continue

            connection.update_activity()
            connection.bytes_read += len(data)
            self.logger.serial_read(port_id, len(data), data.hex())

            if self._data_callback:
                try:
                    result = self._data_callback(port_id, connection.session_id, data)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    self.logger.error("data_callback_error", f"Error in data callback: {e}", port_id=port_id, error=str(e))

    def _notify_disconnect(self, port_id: str, session_id: str) -> None:
        if not self._disconnect_callback:
            return
        try:
            result = self._disconnect_callback(port_id, session_id)
            if asyncio.iscoroutine(result):
                # Run outside the (finished) reader task
                asyncio.create_task(result)
        except Exception as e:
            self.logger.error("disconnect_callback_error", f"Error in disconnect callback: {e}", port_id=port_id, error=str(e))

    async def write_to_port(self, port_id: str, data: bytes) -> int:
        """Write bytes and return the count written (raises SerialWriteError on failure)."""
        connection = self.active_connections.get(port_id)
        if not connection or not connection.is_open():
            raise SerialWriteError(f"Port {port_id} is not open")

        try:
            loop = asyncio.get_running_loop()
            bytes_written = await loop.run_in_executor(None, connection.serial_port.write, data)
        except (serial.SerialException, OSError) as e:
            connection.status = ConnectionStatus.ERROR
            connection.error_count += 1
            raise SerialWriteError(f"Serial write error on {port_id}: {e}") from e

        bytes_written = bytes_written if isinstance(bytes_written, int) else len(data)
        connection.update_activity()
        connection.bytes_written += bytes_written
        self.logger.serial_write(port_id, bytes_written)
        return bytes_written

    async def pulse_dtr(self, port_id: str, low_seconds: float = 0.1) -> None:
        """Reset a classic Arduino through its open connection by dropping DTR."""
        connection = self.active_connections.get(port_id)
        if not connection or not connection.is_open():
            raise SerialWriteError(f"Port {port_id} is not open")
        connection.serial_port.dtr = False
        await asyncio.sleep(low_seconds)
        connection.serial_port.dtr = True

    async def flush_port(self, port_id: str) -> bool:
        connection = self.active_connections.get(port_id)
        if not connection or not connection.is_open():
            return False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, connection.serial_port.reset_input_buffer)
            await loop.run_in_executor(None, connection.serial_port.reset_output_buffer)
            return True
        except Exception as e:
            self.logger.error("flush_error", f"Error flushing {port_id}: {e}", port_id=port_id, error=str(e))
            return False

    def get_active_connections(self) -> Dict[str, Connection]:
        return self.active_connections.copy()

    def get_connection_status(self, port_id: str) -> Optional[ConnectionStatus]:
        connection = self.active_connections.get(port_id)
        return connection.status if connection else None

    def get_connection(self, port_id: str) -> Optional[Connection]:
        return self.active_connections.get(port_id)

    def get_port_error_count(self, port_id: str) -> int:
        connection = self.active_connections.get(port_id)
        return connection.error_count if connection else 0
