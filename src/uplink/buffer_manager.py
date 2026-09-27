"""Bounded outbound message buffer between hub components and the cloud uplink."""

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional

from src.logging_config import StructuredLogger

# Dropped first when the buffer is full; everything else (task status, device events,
# health) is only dropped once no telemetry is left.
DROPPABLE_FIRST = "telemetry"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(moment: datetime) -> str:
    """ISO 8601 in UTC with a Z suffix."""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class BufferedMessage:
    """Message stored in buffer."""

    message_type: str
    payload: Dict[str, Any]
    timestamp: datetime
    size_bytes: int


class BufferManager:
    """FIFO buffer with a byte limit; the uplink drains it while connected."""

    def __init__(
        self,
        size_mb: float = 10.0,
        warn_threshold: float = 0.8,
    ):
        self.logger = StructuredLogger(__name__)
        self.size_bytes = int(size_mb * 1024 * 1024)
        self.warn_threshold = warn_threshold
        self.warn_threshold_bytes = int(self.size_bytes * warn_threshold)

        self.buffer: Deque[BufferedMessage] = deque()
        self.current_size_bytes = 0

        self._above_threshold = False
        self._drop_count = 0
        self._total_messages = 0
        self._total_drops = 0
        self._available = asyncio.Event()

    def add_message(self, message_type: str, payload: Dict[str, Any]) -> bool:
        """Append a message, dropping older ones if the buffer is full."""
        message_size = len(json.dumps(payload).encode("utf-8"))
        if message_size > self.size_bytes:
            self.logger.error(
                "buffer_message_too_large",
                f"Dropping {message_type} message larger than the whole buffer",
                message_type=message_type,
                size_bytes=message_size,
            )
            return False

        self._total_messages += 1
        while self.current_size_bytes + message_size > self.size_bytes and self.buffer:
            self._drop_one()

        self.buffer.append(BufferedMessage(message_type, payload, utc_now(), message_size))
        self.current_size_bytes += message_size
        self._check_threshold()
        self._available.set()

        self.logger.debug(
            "message_buffered",
            f"Buffered {message_type} message",
            message_type=message_type,
            size_bytes=message_size,
            buffer_length=len(self.buffer),
        )
        return True

    def requeue_front(self, message: BufferedMessage) -> None:
        """Put a message that failed to send back at the head of the queue."""
        self.buffer.appendleft(message)
        self.current_size_bytes += message.size_bytes
        while self.current_size_bytes > self.size_bytes and len(self.buffer) > 1:
            self._drop_one()
        self._check_threshold()
        self._available.set()

    def _drop_one(self) -> None:
        """Drop the oldest telemetry message, or the oldest message if there is none."""
        index = next((i for i, m in enumerate(self.buffer) if m.message_type == DROPPABLE_FIRST), 0)
        dropped = self.buffer[index]
        del self.buffer[index]
        self.current_size_bytes -= dropped.size_bytes
        self._drop_count += 1
        self._total_drops += 1
        self.logger.buffer_drop(1, dropped.message_type)

    def _check_threshold(self) -> None:
        utilization = self.current_size_bytes / self.size_bytes
        if not self._above_threshold and self.current_size_bytes >= self.warn_threshold_bytes:
            self._above_threshold = True
            self.logger.buffer_warning(
                self.current_size_bytes / (1024 * 1024),
                self.warn_threshold_bytes / (1024 * 1024),
            )
        elif self._above_threshold and self.current_size_bytes < self.warn_threshold_bytes:
            self._above_threshold = False
            self.logger.info(
                "buffer_below_threshold",
                f"Buffer dropped below threshold: {utilization:.1%}",
                usage_mb=self.current_size_bytes / (1024 * 1024),
                utilization=utilization,
            )

    async def get_messages(self, max_messages: Optional[int] = None) -> list[BufferedMessage]:
        """Peek at buffered messages without removing them."""
        if max_messages is None:
            return list(self.buffer)
        return list(self.buffer)[:max_messages]

    async def pop_message(self) -> Optional[BufferedMessage]:
        """Remove and return the oldest message, or None if empty."""
        if not self.buffer:
            self._available.clear()
            return None
        message = self.buffer.popleft()
        self.current_size_bytes -= message.size_bytes
        self._check_threshold()
        if not self.buffer:
            self._available.clear()
        return message

    async def pop_messages(self, count: int) -> list[BufferedMessage]:
        messages = []
        for _ in range(count):
            message = await self.pop_message()
            if message is None:
                break
            messages.append(message)
        return messages

    async def wait_for_message(self, timeout: Optional[float] = None) -> bool:
        """Wait until the buffer is non-empty. Returns False on timeout."""
        if self.buffer:
            return True
        self._available.clear()
        try:
            await asyncio.wait_for(self._available.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return bool(self.buffer)

    def clear(self) -> None:
        cleared_count = len(self.buffer)
        self.buffer.clear()
        self.current_size_bytes = 0
        self._above_threshold = False
        self._available.clear()
        self.logger.info(
            "buffer_cleared",
            f"Cleared {cleared_count} messages from buffer",
            cleared_count=cleared_count,
        )

    def get_stats(self) -> Dict[str, Any]:
        utilization = self.current_size_bytes / self.size_bytes if self.size_bytes > 0 else 0
        return {
            "size_mb": self.size_bytes / (1024 * 1024),
            "used_mb": self.current_size_bytes / (1024 * 1024),
            "utilization_percent": utilization * 100,
            "message_count": len(self.buffer),
            "total_messages": self._total_messages,
            "total_drops": self._total_drops,
            "current_drop_count": self._drop_count,
            "above_threshold": self._above_threshold,
        }

    def is_above_threshold(self) -> bool:
        return self._above_threshold

    def get_utilization(self) -> float:
        return self.current_size_bytes / self.size_bytes if self.size_bytes > 0 else 0

    def get_message_count(self) -> int:
        return len(self.buffer)

    def reset_drop_count(self) -> int:
        count = self._drop_count
        self._drop_count = 0
        return count
