"""
Task system for command execution.

Tasks represent executable units of work triggered by server commands.
All tasks inherit from BaseTask and implement async execute() method.
"""

from .base_task import BaseTask, TaskStatus
from .serial_write_task import SerialWriteTask
from .flash_task import FlashTask
from .restart_task import RestartTask
from .close_connection_task import CloseConnectionTask

__all__ = [
    "BaseTask",
    "TaskStatus",
    "SerialWriteTask",
    "FlashTask",
    "RestartTask",
    "CloseConnectionTask",
]
