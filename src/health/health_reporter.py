"""
Health Reporter for system and service metrics.

Collects and reports comprehensive health metrics including:
- System metrics (CPU, memory, disk, temperature)
- Service metrics (provided by the runtime: connections, tasks, buffer)
- Hub profile and uplink state (Wi-Fi / cellular, from the uplink manager)
- Per-port error tracking
- Uptime tracking
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import psutil

from src.logging_config import get_logger

logger = get_logger(__name__)

MetricsProvider = Callable[[], Dict[str, Any]]


class HealthReporter:
    """Collects health metrics periodically and hands them to a callback (the uplink)."""

    def __init__(
        self,
        report_interval: int = 30,
        health_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        service_metrics: Optional[MetricsProvider] = None,
        profile: Optional[MetricsProvider] = None,
        uplink: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    ):
        """
        Args:
            report_interval: Reporting interval in seconds
            health_callback: Receives each report (sync or async)
            service_metrics: Returns service-level metrics (connections, tasks, buffer)
            profile: Returns {name, mode} of this hub
            uplink: Returns the uplink manager status, or None when unknown
        """
        self.report_interval = report_interval
        self.health_callback = health_callback
        self.service_metrics = service_metrics
        self.profile = profile
        self.uplink = uplink

        self._start_time = time.time()
        self._port_errors: Dict[str, int] = {}
        self._port_read_errors: Dict[str, int] = {}
        self._port_write_errors: Dict[str, int] = {}
        self._reporter_task: Optional[asyncio.Task] = None
        self._running = False

        # Prime psutil so later non-blocking cpu_percent() calls are meaningful
        psutil.cpu_percent(interval=None)

    async def start(self) -> None:
        if self._running:
            logger.warning("HealthReporter already running")
            return
        self._running = True
        self._reporter_task = asyncio.create_task(self._report_loop())
        logger.info("HealthReporter started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._reporter_task:
            self._reporter_task.cancel()
            try:
                await self._reporter_task
            except asyncio.CancelledError:
                pass
            self._reporter_task = None
        logger.info("HealthReporter stopped")

    async def _report_loop(self) -> None:
        while self._running:
            try:
                await self._send_health_report(await self.collect_health_metrics())
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health reporter loop: {e}", exc_info=True)
            await asyncio.sleep(self.report_interval)

    async def collect_health_metrics(self) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "uptime_seconds": self.get_uptime_seconds(),
            "system": self._collect_system_metrics(),
            "service": self._call(self.service_metrics) or {},
            "errors": self._collect_error_metrics(),
        }
        profile = self._call(self.profile)
        if profile:
            metrics["profile"] = profile
        uplink = self._call(self.uplink)
        if uplink:
            metrics["uplink"] = uplink
        return metrics

    @staticmethod
    def _call(provider: Optional[Callable[[], Any]]) -> Any:
        if provider is None:
            return None
        try:
            return provider()
        except Exception as e:
            logger.error(f"Health metrics provider failed: {e}", exc_info=True)
            return None

    def _collect_system_metrics(self) -> Dict[str, Any]:
        try:
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            system_metrics: Dict[str, Any] = {
                "cpu": {"percent": psutil.cpu_percent(interval=None), "count": psutil.cpu_count()},
                "memory": {
                    "total_mb": memory.total / (1024 * 1024),
                    "available_mb": memory.available / (1024 * 1024),
                    "used_mb": memory.used / (1024 * 1024),
                    "percent": memory.percent,
                },
                "disk": {
                    "total_gb": disk.total / (1024**3),
                    "used_gb": disk.used / (1024**3),
                    "free_gb": disk.free / (1024**3),
                    "percent": disk.percent,
                },
            }
            try:
                temps = psutil.sensors_temperatures()
                for sensor in ("cpu_thermal", "coretemp"):
                    if temps and sensor in temps:
                        system_metrics["temperature"] = {"cpu_celsius": temps[sensor][0].current}
                        break
            except (AttributeError, OSError):
                pass
            return system_metrics
        except Exception as e:
            logger.error(f"Error collecting system metrics: {e}", exc_info=True)
            return {}

    def _collect_error_metrics(self) -> Dict[str, Any]:
        return {
            "total_errors": sum(self._port_errors.values()),
            "port_errors": dict(self._port_errors),
            "port_read_errors": dict(self._port_read_errors),
            "port_write_errors": dict(self._port_write_errors),
        }

    def record_port_error(self, port_id: str, error_type: str = "general") -> None:
        if error_type == "read":
            self._port_read_errors[port_id] = self._port_read_errors.get(port_id, 0) + 1
        elif error_type == "write":
            self._port_write_errors[port_id] = self._port_write_errors.get(port_id, 0) + 1
        self._port_errors[port_id] = self._port_errors.get(port_id, 0) + 1

    def reset_port_errors(self, port_id: Optional[str] = None) -> None:
        if port_id:
            self._port_errors.pop(port_id, None)
            self._port_read_errors.pop(port_id, None)
            self._port_write_errors.pop(port_id, None)
        else:
            self._port_errors.clear()
            self._port_read_errors.clear()
            self._port_write_errors.clear()

    async def _send_health_report(self, health_data: Dict[str, Any]) -> None:
        if not self.health_callback:
            return
        try:
            result = self.health_callback(health_data)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.error(f"Failed to send health report: {e}", exc_info=True)

    def get_uptime_seconds(self) -> int:
        return int(time.time() - self._start_time)

    def get_error_count(self, port_id: Optional[str] = None) -> int:
        if port_id:
            return self._port_errors.get(port_id, 0)
        return sum(self._port_errors.values())
