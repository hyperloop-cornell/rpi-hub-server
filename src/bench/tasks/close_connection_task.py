"""
Close connection task: end the serial session on a port.
"""

from typing import Any, Dict, Optional

from .base_task import BaseTask
from ..backend import BenchBackend


class CloseConnectionTask(BaseTask):
    """Closes the serial session on a port (the device stays attached)."""

    def __init__(
        self,
        task_id: str,
        port_id: str,
        backend: BenchBackend,
        priority: int = 1,
        params: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(task_id=task_id, command_type="close_connection", port_id=port_id, params=params, priority=priority)
        self.backend = backend

    async def execute(self) -> Dict[str, Any]:
        await self.backend.close(self.port_id)
        return {"port_id": self.port_id, "status": "closed"}
