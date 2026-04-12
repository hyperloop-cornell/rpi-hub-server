"""Hub Agent for WebSocket communication with server."""

import asyncio
import json
from datetime import datetime
from typing import Any, Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.buffer_manager import BufferManager
from src.logging_config import StructuredLogger


class HubAgent:
    """Manages WebSocket connection to server and message routing."""

    def __init__(
        self,
        hub_id: str,
        server_endpoint: str,
        device_token: str,
        buffer_manager: BufferManager,
        reconnect_interval: int = 5,
        max_reconnect_attempts: int = 10,
    ):
        """Initialize Hub Agent.

        Args:
            hub_id: Unique hub identifier
            server_endpoint: WebSocket server URL
            device_token: Authentication token
            buffer_manager: Buffer manager instance
            reconnect_interval: Reconnection interval in seconds
            max_reconnect_attempts: Maximum reconnection attempts
        """
        self.logger = StructuredLogger(__name__)
        self.hub_id = hub_id
        self.server_endpoint = server_endpoint
        self.device_token = device_token
        self.buffer_manager = buffer_manager
        self.reconnect_interval = reconnect_interval
        self.max_reconnect_attempts = max_reconnect_attempts

        # Connection state
        self.ws_connection: Optional[websockets.WebSocketClientProtocol] = None
        self.is_connected = False
        self.reconnect_attempts = 0

        # Tasks
        self._sender_task: Optional[asyncio.Task] = None
        self._receiver_task: Optional[asyncio.Task] = None
        self._running = False

        # Message callbacks
        self._command_callback: Optional[Callable] = None
        self._device_event_callback: Optional[Callable] = None

        self.logger.info(
            "hub_agent_initialized",
            "Hub Agent initialized",
            hub_id=hub_id,
            server_endpoint=server_endpoint,
        )

    async def start(self) -> None:
        """Start hub agent and connect to server."""
        self._running = True
        await self.connect_to_server()

        self.logger.info("hub_agent_started", "Hub Agent started")

    async def stop(self) -> None:
        """Stop hub agent and disconnect."""
        self._running = False

        # Cancel tasks
        if self._sender_task:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass

        if self._receiver_task:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass

        # Disconnect
        await self.disconnect_from_server()

        self.logger.info("hub_agent_stopped", "Hub Agent stopped")

    async def connect_to_server(self) -> bool:
        """Connect to WebSocket server with authentication.

        Returns:
            True if connected successfully
        """
        try:
            # Parse endpoint for logging
            from urllib.parse import urlparse
            parsed = urlparse(self.server_endpoint)
            
            self.logger.info(
                "ws_connecting",
                f"Connecting to {self.server_endpoint}",
                endpoint=self.server_endpoint,
                scheme=parsed.scheme,
                host=parsed.hostname,
                port=parsed.port or (443 if parsed.scheme == "wss" else 80),
                path=parsed.path,
            )
            
            # Check DNS resolution if not localhost
            if parsed.hostname and parsed.hostname not in ["localhost", "127.0.0.1"]:
                try:
                    import socket
                    resolved_ip = socket.gethostbyname(parsed.hostname)
                    self.logger.info(
                        "dns_resolved",
                        f"Resolved {parsed.hostname} to {resolved_ip}",
                        hostname=parsed.hostname,
                        ip=resolved_ip,
                    )
                except socket.gaierror as dns_error:
                    self.logger.error(
                        "dns_resolution_failed",
                        f"Failed to resolve hostname {parsed.hostname}: {dns_error}",
                        hostname=parsed.hostname,
                        error=str(dns_error),
                    )
                    raise

            # Connect to WebSocket
            self.logger.info(
                "ws_attempting_connection",
                f"Attempting WebSocket connection with ping_interval=20s, ping_timeout=10s",
            )
            
            self.ws_connection = await websockets.connect(
                self.server_endpoint,
                ping_interval=20,
                ping_timeout=10,
            )

            # Send handshake
            handshake = {
                "type": "hub_connect",
                "hubId": self.hub_id,
                "deviceToken": self.device_token,
                "timestamp": datetime.now().isoformat(),
                "version": "1.0.0",
            }

            await self.ws_connection.send(json.dumps(handshake))

            self.is_connected = True
            self.reconnect_attempts = 0

            # Start sender and receiver tasks
            self._sender_task = asyncio.create_task(self._send_loop())
            self._receiver_task = asyncio.create_task(self._receive_loop())

            self.logger.info(
                "Sender and receiver tasks created",
                extra={
                    "event": "tasks_created",
                    "sender_task_done": self._sender_task.done(),
                    "receiver_task_done": self._receiver_task.done(),
                }
            )

            self.logger.info(
                "ws_connected",
                "Connected to server",
                endpoint=self.server_endpoint,
            )

            return True

        except Exception as e:
            self.is_connected = False
            
            # Extract more details for common error types
            error_details = {
                "error": str(e),
                "error_type": type(e).__name__,
                "endpoint": self.server_endpoint,
            }
            
            # Add errno for OSError/ConnectionRefusedError
            if hasattr(e, 'errno'):
                error_details["errno"] = e.errno
                
            # Add specific messages for common errors
            error_msg = f"Error connecting to server: {e}"
            if "Connection refused" in str(e) or (hasattr(e, 'errno') and e.errno == 111):
                error_msg += " - The server is not accepting connections. Check if the cloud service is running and accessible from this device."
            elif "Name or service not known" in str(e) or "getaddrinfo failed" in str(e):
                error_msg += " - DNS resolution failed. Check the hostname in SERVER_ENDPOINT."
            elif "Network is unreachable" in str(e):
                error_msg += " - Network unreachable. Check your network connection."
            elif "timed out" in str(e).lower():
                error_msg += " - Connection timed out. Check if the server is accessible and not blocked by firewall."
            
            self.logger.error(
                "ws_connection_error",
                error_msg,
                **error_details,
            )

            # Schedule reconnection
            if self._running:
                asyncio.create_task(self._handle_reconnection())

            return False

    async def disconnect_from_server(self) -> None:
        """Disconnect from WebSocket server."""
        if self.ws_connection:
            try:
                await self.ws_connection.close()
                self.logger.info("ws_disconnected", "Disconnected from server")
            except Exception as e:
                self.logger.error(
                    "ws_disconnect_error",
                    f"Error disconnecting: {e}",
                    error=str(e),
                )

        self.ws_connection = None
        self.is_connected = False

    async def _handle_reconnection(self) -> None:
        """Handle reconnection with exponential backoff."""
        if not self._running:
            return

        self.reconnect_attempts += 1

        if self.reconnect_attempts > self.max_reconnect_attempts:
            self.logger.error(
                "max_reconnect_attempts_reached",
                "Maximum reconnection attempts reached",
                attempts=self.reconnect_attempts,
            )
            return

        # Exponential backoff (up to 60s)
        delay = min(self.reconnect_interval * (2 ** (self.reconnect_attempts - 1)), 60)

        self.logger.info(
            "ws_reconnecting",
            f"Reconnecting in {delay}s (attempt {self.reconnect_attempts})",
            delay_s=delay,
            attempt=self.reconnect_attempts,
        )

        await asyncio.sleep(delay)
        await self.connect_to_server()

    async def _send_loop(self) -> None:
        """Send buffered messages to server."""
        self.logger.info(
            "Message sender started",
            extra={
                "event": "sender_started",
                "is_connected": self.is_connected,
                "is_running": self._running,
            }
        )

        while self._running and self.is_connected:
            try:
                # Get message from buffer
                message = await self.buffer_manager.pop_message()

                if message:
                    # Format message for transmission
                    envelope = {
                        "type": message.message_type,
                        "hubId": self.hub_id,
                        "timestamp": message.timestamp.isoformat(),
                        **message.payload,
                    }

                    # Send to server
                    if self.ws_connection:
                        await self.ws_connection.send(json.dumps(envelope))

                        self.logger.info(
                            f"Sent {message.message_type} message",
                            extra={
                                "event": "message_sent",
                                "message_type": message.message_type,
                                "payload_bytes": message.size_bytes,
                            }
                        )
                        
                        self.logger.ws_send(
                            message.message_type,
                            payload_bytes=message.size_bytes,
                        )
                    else:
                        self.logger.warning(
                            "WebSocket connection lost, cannot send message",
                            extra={"event": "ws_connection_lost", "message_type": message.message_type}
                        )
                else:
                    # No messages, wait a bit
                    await asyncio.sleep(0.1)

            except ConnectionClosed:
                self.logger.warning(
                    "ws_connection_closed",
                    "WebSocket connection closed during send",
                )
                self.is_connected = False
                if self._running:
                    asyncio.create_task(self._handle_reconnection())
                break

            except asyncio.CancelledError:
                break

            except Exception as e:
                self.logger.error(
                    "send_loop_error",
                    f"Error in send loop: {e}",
                    error=str(e),
                )
                await asyncio.sleep(1)

        self.logger.info(
            "Message sender stopped",
            extra={
                "event": "sender_stopped",
                "is_connected": self.is_connected,
                "is_running": self._running,
            }
        )

    async def _receive_loop(self) -> None:
        """Receive messages from server."""
        self.logger.info(
            "Message receiver started",
            extra={"event": "receiver_started"}
        )

        while self._running and self.is_connected:
            try:
                if not self.ws_connection:
                    break

                # Receive message
                message = await self.ws_connection.recv()
                data = json.loads(message)

                message_type = data.get("type")

                self.logger.ws_receive(
                    message_type or "unknown",
                    payload_size=len(message),
                )

                # Route message to appropriate handler
                await self._process_incoming_message(data)

            except ConnectionClosed:
                self.logger.warning(
                    "ws_connection_closed",
                    "WebSocket connection closed during receive",
                )
                self.is_connected = False
                if self._running:
                    asyncio.create_task(self._handle_reconnection())
                break

            except asyncio.CancelledError:
                break

            except json.JSONDecodeError as e:
                self.logger.error(
                    "message_parse_error",
                    f"Error parsing message: {e}",
                    error=str(e),
                )

            except Exception as e:
                self.logger.error(
                    "receive_loop_error",
                    f"Error in receive loop: {e}",
                    error=str(e),
                )
                await asyncio.sleep(1)

        self.logger.info(
            "Message receiver stopped",
            extra={"event": "receiver_stopped"}
        )

    async def _process_incoming_message(self, data: Dict[str, Any]) -> None:
        """Process incoming message from server.

        Args:
            data: Message data
        """
        message_type = data.get("type")

        if message_type == "command":
            # Route to command handler
            if self._command_callback:
                try:
                    if asyncio.iscoroutinefunction(self._command_callback):
                        await self._command_callback(data)
                    else:
                        self._command_callback(data)
                except Exception as e:
                    self.logger.error(
                        "command_callback_error",
                        f"Error in command callback: {e}",
                        error=str(e),
                    )

        elif message_type == "device_event":
            # Route to device event handler
            if self._device_event_callback:
                try:
                    if asyncio.iscoroutinefunction(self._device_event_callback):
                        await self._device_event_callback(data)
                    else:
                        self._device_event_callback(data)
                except Exception as e:
                    self.logger.error(
                        "device_event_callback_error",
                        f"Error in device event callback: {e}",
                        error=str(e),
                    )

        else:
            self.logger.warning(
                "unknown_message_type",
                f"Unknown message type: {message_type}",
                message_type=message_type,
            )

    async def send_telemetry(
        self,
        port_id: str,
        session_id: str,
        data: bytes,
    ) -> None:
        """Send telemetry data to server.

        Args:
            port_id: Port identifier
            session_id: Session identifier
            data: Serial data (will be base64 encoded)
        """
        import base64

        payload = {
            "portId": port_id,
            "sessionId": session_id,
            "data": base64.b64encode(data).decode("utf-8"),
        }

        self.buffer_manager.add_message("telemetry", payload)

    async def send_health_status(self, health_data: Dict[str, Any]) -> None:
        """Send health status to server.

        Args:
            health_data: Health metrics
        """
        self.buffer_manager.add_message("health", health_data)

    async def send_device_event(
        self,
        event_type: str,
        port_id: str,
        device_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send device event to server.

        Args:
            event_type: Event type (connected, disconnected)
            port_id: Port identifier
            device_info: Optional device information
        """
        payload = {
            "eventType": event_type,
            "portId": port_id,
        }

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
        """Send task status to server.

        Args:
            task_id: Task identifier
            status: Task status (completed, failed, running)
            progress: Optional progress percentage
            result: Optional result data
            error: Optional error message
        """
        payload = {
            "taskId": task_id,
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if progress is not None:
            payload["progress"] = progress
        if result is not None:
            payload["result"] = result
        if error is not None:
            payload["error"] = error

        self.buffer_manager.add_message("task_status", payload)
    
    def send_task_status_update(self, task_status_data: Dict[str, Any]) -> None:
        """Send task status update from task status callback data.
        
        This is a convenience method for use with CommandHandler's task_status_callback.
        Can be called synchronously from the callback.
        
        Args:
            task_status_data: Task status data from CommandHandler callback
        """
        # Extract fields from task status data
        task_id = task_status_data.get("task_id") or task_status_data.get("taskId")
        status = task_status_data.get("status")
        result = task_status_data.get("result")
        error = task_status_data.get("error")
        progress = task_status_data.get("progress")
        
        # Add to buffer (synchronous operation)
        payload = {
            "taskId": task_id,
            "status": status,
            "timestamp": task_status_data.get("timestamp") or datetime.utcnow().isoformat(),
            # Additional fields passed through for client convenience
            "commandType": task_status_data.get("command_type") or task_status_data.get("commandType"),
            "portId": task_status_data.get("port_id") or task_status_data.get("portId"),
        }
        
        if result is not None:
            payload["result"] = result
        if error is not None:
            payload["error"] = error
        if progress is not None:
            payload["progress"] = progress
        
        self.buffer_manager.add_message("task_status", payload)

    def set_command_callback(self, callback: Callable) -> None:
        """Set callback for command messages.

        Args:
            callback: Async or sync function(data: Dict)
        """
        self._command_callback = callback

    def set_device_event_callback(self, callback: Callable) -> None:
        """Set callback for device event messages.

        Args:
            callback: Async or sync function(data: Dict)
        """
        self._device_event_callback = callback

    def get_connection_status(self) -> Dict[str, Any]:
        """Get connection status.

        Returns:
            Connection status information
        """
        return {
            "is_connected": self.is_connected,
            "server_endpoint": self.server_endpoint,
            "reconnect_attempts": self.reconnect_attempts,
            "buffer_stats": self.buffer_manager.get_stats(),
        }
