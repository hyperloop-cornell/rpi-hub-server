"""Composition root: builds the hub's components from settings and wires them together.

    backend (USB or simulated devices) --events--> HubRuntime --messages--> HubAgent --> cloud
    cloud --commands--> HubAgent --> CommandHandler --tasks--> backend

Nothing below this module reaches for globals; everything gets its collaborators here.
"""

import asyncio
from typing import Any, Dict, List, Optional

from src.bench.backend import BenchBackend, BenchConnection, BenchDevice
from src.bench.command_handler import CommandHandler
from src.config import Settings
from src.health.health_reporter import HealthReporter
from src.health.uplink_status import UplinkStatusReader
from src.logging_config import get_logger
from src.uplink.buffer_manager import BufferManager
from src.uplink.hub_agent import HubAgent

logger = get_logger(__name__)

# The hub re-announces every open device after each (re)connect (see cloud CAP_DEVICE_SNAPSHOT)
CAP_DEVICE_SNAPSHOT = "device_snapshot"

# Retry reopening a device whose serial session failed while it stays attached
LOST_CONNECTION_RETRY_SECONDS = 3.0
LOST_CONNECTION_MAX_RETRIES = 3


def create_backend(settings: Settings) -> BenchBackend:
    """Build the bench backend selected by bench.source."""
    hub_id = settings.hub.hub_id

    if settings.bench.source == "sim":
        from src.sim import SimBenchBackend, SimDeviceSpec

        specs = [SimDeviceSpec.from_config(raw) for raw in settings.bench.sim_devices] or None
        return SimBenchBackend(hub_id=hub_id, specs=specs, speedup=settings.bench.sim_speedup)

    from src.bench.serial_manager import SerialManager
    from src.bench.usb_backend import UsbBenchBackend
    from src.bench.usb_port_mapper import USBPortMapper

    mapper = USBPortMapper(
        persistence_path=settings.storage.port_mapping_file,
        default_baud_rate=settings.serial.default_baud_rate,
    )
    serial_manager = SerialManager(
        max_connections=settings.serial.max_connections,
        connection_retry_attempts=settings.serial.connection_retry_attempts,
        default_timeout=settings.serial.default_timeout,
    )
    return UsbBenchBackend(
        hub_id=hub_id,
        mapper=mapper,
        serial_manager=serial_manager,
        default_baud_rate=settings.serial.default_baud_rate,
        scan_interval=settings.serial.scan_interval,
    )


class HubRuntime:
    """Owns every hub component and translates backend events into uplink messages."""

    def __init__(self, settings: Settings, backend: Optional[BenchBackend] = None):
        self.settings = settings
        self.backend = backend or create_backend(settings)
        self.backend.set_listener(self)

        self.uplink_status = UplinkStatusReader(
            settings.uplink.status_file, stale_after_seconds=settings.uplink.stale_after_seconds
        )
        self.buffer = BufferManager(size_mb=settings.buffer.size_mb, warn_threshold=settings.buffer.warn_threshold)
        self.agent = HubAgent(
            hub_id=settings.hub.hub_id,
            server_endpoint=settings.hub.server_endpoint,
            device_token=settings.hub.device_token,
            buffer_manager=self.buffer,
            reconnect_interval=settings.hub.reconnect_interval,
            max_reconnect_attempts=settings.hub.max_reconnect_attempts,
            capabilities=self.capabilities(),
            profile=self.profile,
            buffer_telemetry_while_disconnected=settings.buffer.telemetry_while_disconnected,
        )
        self.commands = CommandHandler(
            backend=self.backend,
            task_status_callback=self.agent.send_task_status_update,
            max_concurrent_tasks=5,
        )
        self.health = HealthReporter(
            report_interval=settings.health.report_interval,
            health_callback=self.agent.send_health_status,
            service_metrics=self.service_metrics,
            profile=self.profile,
            uplink=self.uplink_status.read,
        )

        self.agent.set_command_callback(self._on_command)
        self.agent.add_connected_callback(self._send_device_snapshot)
        self._background: set = set()
        self._started = False

    # ------------------------------------------------------------------ identity

    def capabilities(self) -> List[str]:
        return [*self.backend.capabilities(), CAP_DEVICE_SNAPSHOT]

    def profile(self) -> Dict[str, Any]:
        return {
            "name": self.settings.hub.profile_name or None,
            "mode": self.settings.hub.mode,
            "uplink": self.uplink_status.active_uplink(),
        }

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        logger.info(
            "Starting hub runtime",
            extra={
                "hub_id": self.settings.hub.hub_id,
                "profile": self.settings.hub.profile_name or "default",
                "bench_source": self.settings.bench.source,
                "auto_connect": self.settings.auto_connect_policy,
            },
        )
        await self.commands.start()
        await self.health.start()
        await self.agent.start()
        await self.backend.start()
        self._started = True

    async def stop(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.health.stop()
        await self.commands.stop()
        await self.backend.stop()
        await self.agent.stop()
        self._started = False

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # ------------------------------------------------------------------ backend events

    def should_auto_connect(self, device: BenchDevice) -> bool:
        policy = self.settings.auto_connect_policy
        if policy == "all":
            return True
        if policy == "known_boards":
            return device.board is not None and getattr(device.board, "auto_connect", True)
        return False

    async def on_device_added(self, device: BenchDevice) -> None:
        if not self.should_auto_connect(device):
            logger.info(
                "Device detected but not opened (auto_connect policy)",
                extra={"port_id": device.port_id, "vendor_id": device.vendor_id, "product_id": device.product_id},
            )
            return
        try:
            await self.backend.open(device.port_id)
        except Exception as e:
            logger.warning(f"Auto-connect failed for {device.port_id}: {e}")

    async def on_device_removed(self, device: BenchDevice) -> None:
        if self.agent.is_connected:
            await self.agent.send_device_event(
                "disconnected",
                device.port_id,
                {"port": device.device_path, "vendor_id": device.vendor_id, "product_id": device.product_id},
            )

    async def on_connection_opened(self, connection: BenchConnection, device: BenchDevice) -> None:
        if self.agent.is_connected:
            await self.agent.send_device_event("connected", device.port_id, self.device_info(device, connection))

    async def on_connection_lost(self, port_id: str, session_id: str, reason: str) -> None:
        if self.agent.is_connected:
            await self.agent.send_device_event(
                "disconnected", port_id, {"session_id": session_id, "reason": reason}
            )
        self._spawn(self._reopen_lost(port_id))

    def on_data(self, port_id: str, session_id: str, data: bytes) -> None:
        self.agent.queue_telemetry(port_id, session_id, data)

    async def _reopen_lost(self, port_id: str) -> None:
        """A read failed but the device may still be attached (glitch, brown-out): retry."""
        for _ in range(LOST_CONNECTION_MAX_RETRIES):
            await asyncio.sleep(LOST_CONNECTION_RETRY_SECONDS)
            device = self.backend.get_device(port_id)
            if device is None or self.backend.get_connection(port_id) is not None:
                return
            try:
                await self.backend.open(port_id)
                return
            except Exception as e:
                logger.info(f"Reopen of {port_id} failed: {e}")

    # ------------------------------------------------------------------ uplink events

    @staticmethod
    def device_info(device: BenchDevice, connection: Optional[BenchConnection]) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "port": device.device_path,
            "vendor_id": device.vendor_id,
            "product_id": device.product_id,
            "serial_number": device.serial_number,
            "manufacturer": device.manufacturer,
            "product": device.product,
            "description": device.description or device.product,
            "auto_connected": True,
        }
        if connection:
            info["baud_rate"] = connection.baud_rate
            info["session_id"] = connection.session_id
        board = device.board_summary()
        if board:
            info["board_profile"] = board
        return info

    async def _send_device_snapshot(self) -> None:
        """After each connect, announce every open session (the cloud cleared its cache)."""
        for connection in self.backend.connections():
            device = self.backend.get_device(connection.port_id)
            if device:
                await self.agent.send_device_event("connected", device.port_id, self.device_info(device, connection))

    async def _on_command(self, envelope: Dict[str, Any]) -> None:
        command = envelope.get("command")
        if not isinstance(command, dict):
            logger.error("Command envelope missing 'command' field")
            return
        try:
            await self.commands.handle_command(command)
        except ValueError:
            pass  # already reported to the cloud as a failed task

    # ------------------------------------------------------------------ metrics

    def service_metrics(self) -> Dict[str, Any]:
        connections = self.backend.connections()
        return {
            "serial": {
                "active_connections": len(connections),
                "ports": [
                    {
                        "port_id": c.port_id,
                        "status": c.status,
                        "baud_rate": c.baud_rate,
                        "bytes_read": c.bytes_read,
                        "bytes_written": c.bytes_written,
                    }
                    for c in connections
                ],
            },
            "usb": {"detected_devices": len(self.backend.devices())},
            "tasks": {
                "queue_size": self.commands.get_queue_size(),
                "running_count": self.commands.get_running_task_count(),
                "total_tasks": len(self.commands.get_all_tasks()),
            },
            "buffer": self.buffer.get_stats(),
            "uplink_agent": {
                "connected": self.agent.is_connected,
                "reconnect_attempts": self.agent.reconnect_attempts,
                "last_error": self.agent.last_error,
            },
        }
