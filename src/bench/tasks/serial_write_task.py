"""
Serial write task for sending data to a bench device.
"""

import base64
import binascii
from typing import Any, Dict, Optional

from .base_task import BaseTask
from ..backend import BenchBackend


class SerialWriteTask(BaseTask):
    """Writes text (utf-8) or base64-encoded bytes to a serial port."""

    def __init__(
        self,
        task_id: str,
        port_id: str,
        data: str,
        backend: BenchBackend,
        encoding: str = "utf-8",
        priority: int = 5,
        params: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(task_id=task_id, command_type="serial_write", port_id=port_id, params=params, priority=priority)
        if encoding not in ("utf-8", "base64"):
            raise ValueError(f"Unsupported encoding: {encoding}")
        self.data = data
        self.encoding = encoding
        self.backend = backend

    def _payload(self) -> bytes:
        if self.encoding == "base64":
            try:
                return base64.b64decode(self.data, validate=True)
            except (binascii.Error, ValueError) as e:
                raise ValueError(f"Invalid base64 data: {e}") from e
        return self.data.encode("utf-8")

    async def execute(self) -> Dict[str, Any]:
        bytes_written = await self.backend.write(self.port_id, self._payload())
        return {"port_id": self.port_id, "bytes_written": bytes_written, "encoding": self.encoding}
