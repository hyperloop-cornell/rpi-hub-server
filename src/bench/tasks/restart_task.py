"""
Restart task: reset a bench device and reopen its serial session.
"""

from typing import Any, Dict, Optional

from .base_task import BaseTask
from ..backend import BenchBackend


class RestartTask(BaseTask):
    """Resets the device and opens a fresh serial session (new session_id)."""

    def __init__(
        self,
        task_id: str,
        port_id: str,
        backend: BenchBackend,
        priority: int = 2,
        params: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(task_id=task_id, command_type="restart", port_id=port_id, params=params, priority=priority)
        self.backend = backend

    async def execute(self) -> Dict[str, Any]:
        connection = await self.backend.restart(self.port_id)
        return {
            "port_id": self.port_id,
            "restart_completed": True,
            "session_id": connection.session_id,
        }
