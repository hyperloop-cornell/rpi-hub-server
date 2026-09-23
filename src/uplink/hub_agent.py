"""Hub Agent: the persistent, authenticated WebSocket uplink to the cloud service."""

import asyncio
import base64
import json
import random
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

import websockets
from websockets.exceptions import ConnectionClosed

from src.logging_config import StructuredLogger
from src.uplink.buffer_manager import BufferManager, iso_utc, utc_now

PROTOCOL_VERSION = "1.1.0"

# How long a dropped connection waits before redialing, before backoff kicks in
MIN_RECONNECT_DELAY_SECONDS = 1.0
MAX_RECONNECT_DELAY_SECONDS = 60.0

# A connection must stay up this long to reset the backoff; shorter ones (for example the
# server rejecting the device token) count as failures so the hub does not redial rapidly.
STABLE_CONNECTION_SECONDS = 30.0


class HubAgent:
    """Keeps one WebSocket connection to the cloud open and drains the outbound buffer.

    A single supervisor task owns the connection: it dials, sends the handshake, runs the
    send and receive loops, and redials with capped exponential backoff when anything fails.
    Messages produced while disconnected stay in the BufferManager (bounded, telemetry dropped
    first) and are sent after reconnecting.
    """

    def __init__(
        self,
        hub_id: str,
        server_endpoint: str,
        device_token: str,
        buffer_manager: BufferManager,
        reconnect_interval: float = 5,
        max_reconnect_attempts: int = 0,
        capabilities: Optional[List[str]] = None,
        profile: Optional[Union[Dict[str, Any], Callable[[], Dict[str, Any]]]] = None,
        buffer_telemetry_while_disconnected: bool = True,
    ):
        """
        Args:
            hub_id: Unique hub identifier
            server_endpoint: WebSocket server URL
            device_token: Authentication token
            buffer_manager: Outbound message buffer
            reconnect_interval: Base reconnect delay in seconds (doubles per failure, max 60s)
            max_reconnect_attempts: Consecutive failed dials before giving up; 0 = never give up
            capabilities: Advertised in the handshake (e.g. "bench", "flash:bin")
            profile: Advertised in the handshake ({name, mode, uplink}); a callable is
                evaluated at every connect so the reported uplink is current
            buffer_telemetry_while_disconnected: Keep telemetry produced during outages
        """
        self.logger = StructuredLogger(__name__)
        self.hub_id = hub_id
        self.server_endpoint = server_endpoint
        self.device_token = device_token
        self.buffer_manager = buffer_manager
        self.reconnect_interval = max(float(reconnect_interval), MIN_RECONNECT_DELAY_SECONDS)
        self.max_reconnect_attempts = max_reconnect_attempts
        self.capabilities = list(capabilities or ["bench", "flash:ino", "flash:hex"])
        self.profile = profile if callable(profile) else dict(profile or {})
        self.buffer_telemetry_while_disconnected = buffer_telemetry_while_disconnected

        self.ws_connection = None
        self.is_connected = False
        self.reconnect_attempts = 0
        self.connected_since: Optional[datetime] = None
        self.last_error: Optional[str] = None

        self._running = False
        self._supervisor_task: Optional[asyncio.Task] = None

        self._command_callback: Optional[Callable] = None
        self._device_event_callback: Optional[Callable] = None
        self._connected_callbacks: List[Callable[[], Awaitable[None]]] = []

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the connection supervisor (returns immediately)."""
        if self._running:
            return
        self._running = True
        self._supervisor_task = asyncio.create_task(self._supervise())
        self.logger.info("hub_agent_started", "Hub Agent started", endpoint=self.server_endpoint)

    async def stop(self) -> None:
        """Stop the supervisor and close the connection."""
        self._running = False
        if self._supervisor_task:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass
            self._supervisor_task = None
        await self.disconnect_from_server()
        self.logger.info("hub_agent_stopped", "Hub Agent stopped")

    async def disconnect_from_server(self) -> None:
        connection, self.ws_connection = self.ws_connection, None
        self._mark_disconnected()
        if connection is not None:
            try:
                await connection.close()
            except Exception as e:
                self.logger.warning("ws_disconnect_error", f"Error closing connection: {e}", error=str(e))

    def _mark_disconnected(self) -> None:
        if self.is_connected:
            self.logger.warning("ws_disconnected", "Disconnected from server")
        self.is_connected = False
        self.connected_since = None

    async def _supervise(self) -> None:
        while self._running:
            try:
                await self._connect_once()
                await self._run_connection()
                if self._connection_age() >= STABLE_CONNECTION_SECONDS:
                    self.reconnect_attempts = 0
                else:
                    self.reconnect_attempts += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self.reconnect_attempts += 1
                self.logger.error(
                    "ws_connection_error",
                    f"Uplink error: {self.last_error}",
                    endpoint=self.server_endpoint,
                    attempt=self.reconnect_attempts,
                )
            finally:
                connection, self.ws_connection = self.ws_connection, None
                self._mark_disconnected()
                if connection is not None:
                    try:
                        await connection.close()
                    except Exception:
                        pass

            if not self._running:
                break
            if self.max_reconnect_attempts and self.reconnect_attempts >= self.max_reconnect_attempts:
                self.logger.error(
                    "max_reconnect_attempts_reached",
                    "Maximum reconnection attempts reached; uplink stopped",
                    attempts=self.reconnect_attempts,
                )
                self._running = False
                break

            delay = self._reconnect_delay()
            self.logger.info(
                "ws_reconnecting",
                f"Reconnecting in {delay:.1f}s",
                delay_s=round(delay, 1),
                attempt=self.reconnect_attempts + 1,
            )
            await asyncio.sleep(delay)

    def _connection_age(self) -> float:
        if self.connected_since is None:
            return 0.0
        return (utc_now() - self.connected_since).total_seconds()

    def _reconnect_delay(self) -> float:
        exponent = max(self.reconnect_attempts - 1, 0)
        base = min(self.reconnect_interval * (2 ** exponent), MAX_RECONNECT_DELAY_SECONDS)
        return base * random.uniform(0.8, 1.2)

    async def _connect_once(self) -> None:
        self.logger.info("ws_connecting", f"Connecting to {self.server_endpoint}", endpoint=self.server_endpoint)
        self.ws_connection = await websockets.connect(
            self.server_endpoint,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
            max_size=None,
        )
        await self.ws_connection.send(json.dumps(self.build_handshake()))
        self.is_connected = True
        self.connected_since = utc_now()
        self.last_error = None
        self.logger.info("ws_connected", "Connected to server", endpoint=self.server_endpoint)

        for callback in list(self._connected_callbacks):
            try:
                await callback()
            except Exception as e:
                self.logger.error("connected_callback_error", f"Error in on-connect callback: {e}", error=str(e))

    def build_handshake(self) -> Dict[str, Any]:
        return {
            "type": "hub_connect",
            "hubId": self.hub_id,
            "deviceToken": self.device_token,
            "timestamp": iso_utc(utc_now()),
            "version": PROTOCOL_VERSION,
            "capabilities": self.capabilities,
            "profile": self.profile() if callable(self.profile) else self.profile,
        }

    async def _run_connection(self) -> None:
        """Run send and receive loops until either one stops."""
        sender = asyncio.create_task(self._send_loop())
        receiver = asyncio.create_task(self._receive_loop())
        done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc

    # ------------------------------------------------------------------ loops

    async def _send_loop(self) -> None:
        while self._running and self.is_connected:
            if not await self.buffer_manager.wait_for_message(timeout=1.0):
                continue
            message = await self.buffer_manager.pop_message()
            if message is None or self.ws_connection is None:
                continue

            envelope = {
                "type": message.message_type,
                "hubId": self.hub_id,
                "timestamp": iso_utc(message.timestamp),
                **message.payload,
            }
            try:
                await self.ws_connection.send(json.dumps(envelope))
            except ConnectionClosed:
                # Keep the message for the next connection
                self.buffer_manager.requeue_front(message)
                self.logger.warning("ws_connection_closed", "Connection closed during send")
                return
            self.logger.ws_send(message.message_type, payload_bytes=message.size_bytes)

    async def _receive_loop(self) -> None:
        while self._running and self.ws_connection is not None:
            try:
                raw = await self.ws_connection.recv()
            except ConnectionClosed as e:
                code = getattr(getattr(e, "rcvd", None), "code", None)
                reason = getattr(getattr(e, "rcvd", None), "reason", "")
                if code == 1008:
                    self.last_error = f"Rejected by server: {reason}"
                    self.logger.error(
                        "ws_rejected",
                        f"Server rejected the hub ({reason}); check HUB_ID and DEVICE_TOKEN",
                        reason=reason,
                    )
                else:
                    self.logger.warning("ws_connection_closed", f"Connection closed by server: {e}")
                return

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                self.logger.error("message_parse_error", f"Error parsing message: {e}", error=str(e))
                continue
            if not isinstance(data, dict):
                continue

            self.logger.ws_receive(data.get("type") or "unknown", payload_size=len(raw))
            await self._process_incoming_message(data)

    async def _process_incoming_message(self, data: Dict[str, Any]) -> None:
        message_type = data.get("type")

        if message_type == "command":
            await self._invoke(self._command_callback, data, "command_callback_error")
        elif message_type == "device_event":
            await self._invoke(self._device_event_callback, data, "device_event_callback_error")
        elif message_type == "hub_connected":
            self.logger.info("hub_acknowledged", "Server acknowledged hub connection")
        else:
            self.logger.warning("unknown_message_type", f"Unknown message type: {message_type}", message_type=message_type)

    async def _invoke(self, callback: Optional[Callable], data: Dict[str, Any], error_event: str) -> None:
        if not callback:
            return
        try:
            result = callback(data)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            self.logger.error(error_event, f"Error in callback: {e}", error=str(e))

    # ------------------------------------------------------------------ outbound API

    async def send_telemetry(self, port_id: str, session_id: str, data: bytes) -> None:
        self.queue_telemetry(port_id, session_id, data)

    def queue_telemetry(self, port_id: str, session_id: str, data: bytes) -> None:
        """Buffer serial data for the cloud (base64 encoded)."""
        if not data:
            return
        if not self.is_connected and not self.buffer_telemetry_while_disconnected:
            return
        self.buffer_manager.add_message(
            "telemetry",
            {"portId": port_id, "sessionId": session_id, "data": base64.b64encode(data).decode("ascii")},
        )

    async def send_health_status(self, health_data: Dict[str, Any]) -> None:
        self.buffer_manager.add_message("health", health_data)

    async def send_device_event(
        self,
        event_type: str,
        port_id: str,
        device_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {"eventType": event_type, "portId": port_id}
        if device_info:
            payload["deviceInfo"] = device_info
        self.buffer_manager.add_message("device_event", payload)

    async def send_task_status(
        self,
        task_id: str,
        status: str,
        progress: Optional[int] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        self.send_task_status_update(
            {"task_id": task_id, "status": status, "progress": progress, "result": result, "error": error}
        )

    def send_task_status_update(self, task_status_data: Dict[str, Any]) -> None:
        """Queue a task status (accepts snake_case or camelCase keys)."""
        payload: Dict[str, Any] = {
            "taskId": task_status_data.get("task_id") or task_status_data.get("taskId"),
            "status": task_status_data.get("status"),
            "commandType": task_status_data.get("command_type") or task_status_data.get("commandType"),
            "portId": task_status_data.get("port_id") or task_status_data.get("portId"),
        }
        for key in ("result", "error", "progress"):
            if task_status_data.get(key) is not None:
                payload[key] = task_status_data[key]
        self.buffer_manager.add_message("task_status", payload)

    # ------------------------------------------------------------------ registration

    def set_command_callback(self, callback: Callable) -> None:
        self._command_callback = callback

    def set_device_event_callback(self, callback: Callable) -> None:
        self._device_event_callback = callback

    def add_connected_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run `callback` after every successful connect and handshake."""
        self._connected_callbacks.append(callback)

    def get_connection_status(self) -> Dict[str, Any]:
        return {
            "is_connected": self.is_connected,
            "server_endpoint": self.server_endpoint,
            "connected_since": iso_utc(self.connected_since) if self.connected_since else None,
            "reconnect_attempts": self.reconnect_attempts,
            "last_error": self.last_error,
            "buffer_stats": self.buffer_manager.get_stats(),
        }
