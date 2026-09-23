"""
Command handler: turns cloud commands into bench tasks and runs them.
"""

import asyncio
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from src.logging_config import get_logger

from .backend import BenchBackend
from .tasks import BaseTask, CloseConnectionTask, FlashTask, RestartTask, SerialWriteTask, TaskStatus

logger = get_logger(__name__)

# Finished tasks kept for GET /tasks; older ones are forgotten
MAX_FINISHED_TASKS = 200

FINISHED = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CommandHandler:
    """Queues tasks by priority and runs them with a bounded worker pool.

    Tasks on the same port run one at a time (a flash and a write to the same board must not
    interleave); tasks on different ports run concurrently.
    """

    def __init__(
        self,
        backend: BenchBackend,
        task_status_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        max_concurrent_tasks: int = 5,
    ):
        self.backend = backend
        self.task_status_callback = task_status_callback
        self.max_concurrent_tasks = max_concurrent_tasks

        self._task_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._tasks: "OrderedDict[str, BaseTask]" = OrderedDict()
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._port_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._workers: List[asyncio.Task] = []
        self._running = False
        self._sequence = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._workers = [asyncio.create_task(self._task_worker(i)) for i in range(self.max_concurrent_tasks)]
        logger.info("CommandHandler started", extra={"workers": self.max_concurrent_tasks})

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in self._tasks.values():
            if task.status == TaskStatus.RUNNING:
                task.cancel()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("CommandHandler stopped")

    async def handle_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """
        Queue a command.

        Args:
            command: {"commandId", "commandType", "portId", "params", "priority"}

        Invalid commands are reported to the cloud as failed tasks (so the GUI does not wait
        for a timeout) and then re-raised as ValueError.
        """
        command_id = command.get("commandId")
        command_type = command.get("commandType")
        port_id = command.get("portId")

        try:
            if not command_id:
                raise ValueError("Missing commandId in command envelope")
            if not command_type:
                raise ValueError("Missing commandType in command envelope")
            task = self._create_task(
                command_id=command_id,
                command_type=command_type,
                port_id=port_id,
                params=command.get("params") or {},
                priority=int(command.get("priority", 5)),
            )
        except Exception as e:
            logger.error(f"Rejected command {command_id}: {e}")
            if command_id:
                self._emit_status(
                    {
                        "task_id": command_id,
                        "command_type": command_type,
                        "port_id": port_id,
                        "status": TaskStatus.FAILED.value,
                        "error": str(e),
                    }
                )
            raise ValueError(f"Invalid command envelope: {e}") from e

        self._tasks[task.task_id] = task
        self._prune_finished()
        self._sequence += 1
        await self._task_queue.put((task.priority, self._sequence, task.task_id))
        await self._report_task_status(task)

        return {"task_id": task.task_id, "command_id": command_id, "status": task.status.value, "queued": True}

    def _create_task(
        self,
        command_id: str,
        command_type: str,
        port_id: Optional[str],
        params: Dict[str, Any],
        priority: int,
    ) -> BaseTask:
        if not port_id:
            raise ValueError(f"Missing portId for {command_type}")

        if command_type == "serial_write":
            data = params.get("data")
            if not data:
                raise ValueError("Missing 'data' parameter for serial_write")
            return SerialWriteTask(
                task_id=command_id,
                port_id=port_id,
                data=data,
                encoding=params.get("encoding", "utf-8"),
                backend=self.backend,
                priority=priority,
                params=params,
            )

        if command_type == "flash":
            firmware_data = params.get("firmwareData")
            if not firmware_data:
                raise ValueError("Missing 'firmwareData' parameter for flash")
            return FlashTask(
                task_id=command_id,
                port_id=port_id,
                firmware_data=firmware_data,
                board_fqbn=params.get("boardFqbn"),
                artifact_format=params.get("artifactFormat"),
                board_profile=params.get("boardProfile"),
                backend=self.backend,
                priority=priority,
                params={k: v for k, v in params.items() if k != "firmwareData"},
            )

        if command_type == "restart":
            return RestartTask(task_id=command_id, port_id=port_id, backend=self.backend, priority=priority, params=params)

        if command_type == "close_connection":
            return CloseConnectionTask(
                task_id=command_id, port_id=port_id, backend=self.backend, priority=priority, params=params
            )

        raise ValueError(f"Unsupported command type: {command_type}")

    async def _task_worker(self, worker_id: int) -> None:
        while self._running:
            try:
                _, _, task_id = await self._task_queue.get()
            except asyncio.CancelledError:
                break

            task = self._tasks.get(task_id)
            if task is None:
                continue

            try:
                async with self._port_locks[task.port_id]:
                    run = asyncio.create_task(task.run())
                    self._running_tasks[task_id] = run
                    try:
                        await run
                    finally:
                        self._running_tasks.pop(task_id, None)
                await self._report_task_status(task)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}", exc_info=True)

    async def _report_task_status(self, task: BaseTask) -> None:
        self._emit_status(
            {
                "task_id": task.task_id,
                "command_type": task.command_type,
                "port_id": task.port_id,
                "status": task.status.value,
                "result": task.result,
                "error": task.error,
            }
        )

    def _emit_status(self, status: Dict[str, Any]) -> None:
        if not self.task_status_callback:
            return
        try:
            self.task_status_callback({"type": "task_status", "timestamp": _utc_iso(), **status})
        except Exception as e:
            logger.error(f"Failed to report task status: {e}", exc_info=True)

    def _prune_finished(self) -> None:
        finished = [task_id for task_id, task in self._tasks.items() if task.status in FINISHED]
        for task_id in finished[: max(0, len(finished) - MAX_FINISHED_TASKS)]:
            del self._tasks[task_id]

    def get_task(self, task_id: str) -> Optional[BaseTask]:
        return self._tasks.get(task_id)

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        return [task.to_dict() for task in self._tasks.values()]

    def get_queue_size(self) -> int:
        return self._task_queue.qsize()

    def get_running_task_count(self) -> int:
        return len(self._running_tasks)
