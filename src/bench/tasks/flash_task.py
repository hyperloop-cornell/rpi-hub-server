"""
Flash task: program firmware onto a bench device.
"""

import base64
import binascii
from typing import Any, Dict, Optional

from .base_task import BaseTask
from ..backend import BenchBackend, FlashRequest


class FlashTask(BaseTask):
    """Decodes the uploaded firmware and hands it to the backend's flasher."""

    def __init__(
        self,
        task_id: str,
        port_id: str,
        firmware_data: str,
        backend: BenchBackend,
        board_fqbn: Optional[str] = None,
        artifact_format: Optional[str] = None,
        board_profile: Optional[str] = None,
        priority: int = 3,
        params: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(task_id=task_id, command_type="flash", port_id=port_id, params=params, priority=priority)
        try:
            self.firmware = base64.b64decode(firmware_data, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"Invalid firmware data encoding: {e}") from e
        self.board_fqbn = board_fqbn
        self.artifact_format = artifact_format
        self.board_profile = board_profile
        self.backend = backend

    async def execute(self) -> Dict[str, Any]:
        request = FlashRequest(
            firmware=self.firmware,
            artifact_format=self.artifact_format,
            board_fqbn=self.board_fqbn,
            board_profile=self.board_profile,
        )
        return await self.backend.flash(self.port_id, request)
