"""
Base task class for all executable tasks.
"""

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from src.logging_config import get_logger

from ..backend import BenchError

logger = get_logger(__name__)


class TaskStatus(str, Enum):
    """Task execution status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BaseTask(ABC):
    """
    Base class for all executable tasks.
    
    Tasks are created from command envelopes and executed asynchronously.
    Each task tracks its own status, result, and error information.
    """
    
    def __init__(
        self,
        task_id: str,
        command_type: str,
        port_id: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        priority: int = 5
    ):
        """
        Initialize base task.
        
        Args:
            task_id: Unique task identifier
            command_type: Type of command (serial_write, flash, restart)
            port_id: Target port ID (if applicable)
            params: Task-specific parameters
            priority: Task priority (1=highest, 10=lowest)
        """
        self.task_id = task_id
        self.command_type = command_type
        self.port_id = port_id
        self.params = params or {}
        self.priority = priority
        
        self.status = TaskStatus.PENDING
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        
        self.created_at = datetime.now(timezone.utc)
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        
        self._cancel_event = asyncio.Event()
        
        logger.info(
            f"Task created",
            extra={
                "task_id": self.task_id,
                "command_type": self.command_type,
                "port_id": self.port_id,
                "priority": self.priority,
            }
        )
    
    @abstractmethod
    async def execute(self) -> Dict[str, Any]:
        """
        Execute the task.
        
        Returns:
            Dict containing task result data
            
        Raises:
            Exception: If task execution fails
        """
        pass
    
    async def run(self) -> None:
        """
        Run the task with status tracking and error handling.
        
        This method wraps execute() with common task lifecycle management.
        """
        try:
            self.status = TaskStatus.RUNNING
            self.started_at = datetime.now(timezone.utc)
            
            logger.info(
                f"Task started",
                extra={
                    "task_id": self.task_id,
                    "command_type": self.command_type,
                    "port_id": self.port_id
                }
            )
            
            # Execute with cancellation support
            execute_task = asyncio.create_task(self.execute())
            cancel_task = asyncio.create_task(self._cancel_event.wait())
            
            done, pending = await asyncio.wait(
                {execute_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            if cancel_task in done:
                # Task was cancelled
                self.status = TaskStatus.CANCELLED
                self.error = "Task cancelled"
                logger.warning(
                    f"Task cancelled",
                    extra={
                        "task_id": self.task_id,
                        "command_type": self.command_type
                    }
                )
            else:
                # Task completed
                self.result = execute_task.result()
                self.status = TaskStatus.COMPLETED
                self.completed_at = datetime.now(timezone.utc)
                
                duration_ms = (
                    (self.completed_at - self.started_at).total_seconds() * 1000
                    if self.started_at else 0
                )
                
                logger.info(
                    f"Task completed",
                    extra={
                        "task_id": self.task_id,
                        "command_type": self.command_type,
                        "port_id": self.port_id,
                        "duration_ms": duration_ms,
                        "result": self.result
                    }
                )
                
        except Exception as e:
            self.status = TaskStatus.FAILED
            self.error = str(e)
            self.completed_at = datetime.now(timezone.utc)
            # Expected failures (bad input, device gone, tool errors) do not need a traceback
            expected = isinstance(e, (ValueError, BenchError))
            
            logger.error(
                f"Task failed: {e}",
                extra={
                    "task_id": self.task_id,
                    "command_type": self.command_type,
                    "port_id": self.port_id,
                    "error": str(e)
                },
                exc_info=not expected
            )
    
    def cancel(self) -> None:
        """Cancel the task if it's running."""
        if self.status == TaskStatus.RUNNING:
            self._cancel_event.set()
            logger.info(
                f"Task cancellation requested",
                extra={"task_id": self.task_id, "command_type": self.command_type}
            )
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert task to dictionary for serialization.
        
        Returns:
            Dictionary representation of task
        """
        return {
            "task_id": self.task_id,
            "command_type": self.command_type,
            "port_id": self.port_id,
            "priority": self.priority,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
    
    def __lt__(self, other: "BaseTask") -> bool:
        """Compare tasks by priority for priority queue."""
        return self.priority < other.priority
