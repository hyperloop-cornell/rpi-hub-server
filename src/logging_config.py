"""JSON structured logging configuration."""

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict

from pythonjsonlogger import jsonlogger


class CustomJsonFormatter(jsonlogger.JsonFormatter):
    """Custom JSON formatter with standardized fields."""

    def add_fields(
        self,
        log_record: Dict[str, Any],
        record: logging.LogRecord,
        message_dict: Dict[str, Any],
    ) -> None:
        """Add custom fields to log record.

        Args:
            log_record: Output log record dictionary
            record: Python logging LogRecord
            message_dict: Parsed message dictionary
        """
        super().add_fields(log_record, record, message_dict)

        # Add timestamp in ISO 8601 format
        log_record["timestamp"] = datetime.now(timezone.utc).isoformat()

        # Add standard fields
        log_record["level"] = record.levelname
        log_record["module"] = record.name

        # Move 'message' to root if it exists
        if "message" in log_record:
            log_record["message"] = log_record["message"]

        # Extract event and context from extra fields
        if hasattr(record, "event"):
            log_record["event"] = record.event
        if hasattr(record, "context"):
            log_record["context"] = record.context

        # Remove internal fields
        for field in ["name", "msg", "args", "levelname", "created", "msecs"]:
            log_record.pop(field, None)


def setup_logging(level: str = "INFO", format_type: str = "json") -> None:
    """Configure application logging.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format_type: Log format ('json' or 'text')
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Remove existing handlers
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)

    if format_type.lower() == "json":
        # JSON formatter for structured logging
        formatter = CustomJsonFormatter(
            "%(timestamp)s %(level)s %(module)s %(event)s %(message)s"
        )
    else:
        # Text formatter for human-readable logs
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    root_logger.setLevel(log_level)

    # Reduce noise from external libraries
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the specified name.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


class StructuredLogger:
    """Helper class for structured logging with consistent event format."""

    def __init__(self, name: str):
        """Initialize structured logger.

        Args:
            name: Logger name (typically __name__)
        """
        self.logger = logging.getLogger(name)

    def log(
        self,
        level: int,
        event: str,
        message: str = "",
        **context: Any,
    ) -> None:
        """Log a structured message.

        Args:
            level: Log level (logging.INFO, logging.ERROR, etc.)
            event: Event type identifier
            message: Optional human-readable message
            **context: Additional context fields
        """
        extra = {"event": event, "context": context}
        self.logger.log(level, message or event, extra=extra)

    def debug(self, event: str, message: str = "", **context: Any) -> None:
        """Log debug message."""
        self.log(logging.DEBUG, event, message, **context)

    def info(self, event: str, message: str = "", **context: Any) -> None:
        """Log info message."""
        self.log(logging.INFO, event, message, **context)

    def warning(self, event: str, message: str = "", **context: Any) -> None:
        """Log warning message."""
        self.log(logging.WARNING, event, message, **context)

    def error(self, event: str, message: str = "", **context: Any) -> None:
        """Log error message."""
        self.log(logging.ERROR, event, message, **context)

    def critical(self, event: str, message: str = "", **context: Any) -> None:
        """Log critical message."""
        self.log(logging.CRITICAL, event, message, **context)

    # Convenience methods for common events

    def serial_read(self, port_id: str, bytes_read: int, data_hex: str = "") -> None:
        """Log serial read event."""
        self.debug(
            "serial_read",
            f"Read {bytes_read} bytes from {port_id}",
            port_id=port_id,
            bytes=bytes_read,
            data_hex=data_hex[:100] if data_hex else None,  # Truncate long data
        )

    def serial_write(self, port_id: str, bytes_written: int) -> None:
        """Log serial write event."""
        self.debug(
            "serial_write",
            f"Wrote {bytes_written} bytes to {port_id}",
            port_id=port_id,
            bytes=bytes_written,
        )

    def ws_send(self, msg_type: str, **context: Any) -> None:
        """Log WebSocket send event."""
        self.debug(
            "ws_send",
            f"Sending {msg_type} message",
            msg_type=msg_type,
            **context,
        )

    def ws_receive(self, msg_type: str, **context: Any) -> None:
        """Log WebSocket receive event."""
        self.debug(
            "ws_receive",
            f"Received {msg_type} message",
            msg_type=msg_type,
            **context,
        )

    def task_created(self, task_id: str, task_type: str, **context: Any) -> None:
        """Log task creation event."""
        self.info(
            "task_created",
            f"Created task {task_id} ({task_type})",
            task_id=task_id,
            task_type=task_type,
            **context,
        )

    def task_completed(
        self, task_id: str, duration_ms: float, result: str = "success"
    ) -> None:
        """Log task completion event."""
        self.info(
            "task_completed",
            f"Task {task_id} completed in {duration_ms}ms",
            task_id=task_id,
            duration_ms=duration_ms,
            result=result,
        )

    def connection_opened(self, session_id: str, port_id: str, **context: Any) -> None:
        """Log connection opened event."""
        self.info(
            "connection_opened",
            f"Opened connection on {port_id}",
            session_id=session_id,
            port_id=port_id,
            **context,
        )

    def connection_failed(
        self, port_id: str, attempt: int, error: str, **context: Any
    ) -> None:
        """Log connection failure event."""
        self.error(
            "connection_failed",
            f"Connection failed on {port_id} (attempt {attempt}): {error}",
            port_id=port_id,
            attempt=attempt,
            error=error,
            **context,
        )

    def port_detected(self, port_id: str, device_path: str, **context: Any) -> None:
        """Log port detection event."""
        self.info(
            "port_detected",
            f"Detected port {port_id} at {device_path}",
            port_id=port_id,
            device_path=device_path,
            **context,
        )

    def port_removed(self, port_id: str, **context: Any) -> None:
        """Log port removal event."""
        self.info(
            "port_removed",
            f"Port {port_id} removed",
            port_id=port_id,
            **context,
        )

    def buffer_warning(self, usage_mb: float, threshold_mb: float) -> None:
        """Log buffer warning event."""
        self.warning(
            "buffer_warning",
            f"Buffer at {usage_mb:.2f}MB (threshold: {threshold_mb:.2f}MB)",
            usage_mb=usage_mb,
            threshold_mb=threshold_mb,
            utilization=usage_mb / (threshold_mb / 0.8) if threshold_mb > 0 else 0,
        )

    def buffer_drop(self, dropped_count: int, message_type: str) -> None:
        """Log buffer drop event."""
        self.warning(
            "buffer_drop",
            f"Dropped {dropped_count} messages (type: {message_type})",
            dropped_count=dropped_count,
            message_type=message_type,
        )
