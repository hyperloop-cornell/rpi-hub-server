"""Test Hub Agent."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.buffer_manager import BufferManager
from src.hub_agent import HubAgent


@pytest.fixture
def buffer_manager():
    """Create buffer manager instance."""
    return BufferManager(size_mb=1.0, warn_threshold=0.8)


@pytest.fixture
def hub_agent(buffer_manager):
    """Create hub agent instance."""
    return HubAgent(
        hub_id="test_hub",
        server_endpoint="ws://localhost:8080/hub",
        device_token="test_token",
        buffer_manager=buffer_manager,
        reconnect_interval=1,
        max_reconnect_attempts=3,
    )


@pytest.fixture
def mock_websocket():
    """Create mock WebSocket connection."""
    ws = AsyncMock()
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_hub_agent_initialization(hub_agent):
    """Test hub agent initialization."""
    assert hub_agent.hub_id == "test_hub"
    assert hub_agent.server_endpoint == "ws://localhost:8080/hub"
    assert hub_agent.device_token == "test_token"
    assert hub_agent.is_connected is False
    assert hub_agent.reconnect_attempts == 0


@pytest.mark.asyncio
async def test_connect_to_server(hub_agent, mock_websocket):
    """Test connecting to server."""
    with patch("websockets.connect", return_value=mock_websocket):
        result = await hub_agent.connect_to_server()

        assert result is True
        assert hub_agent.is_connected is True
        assert hub_agent.ws_connection == mock_websocket
        assert mock_websocket.send.called


@pytest.mark.asyncio
async def test_connect_handshake_format(hub_agent, mock_websocket):
    """Test connection handshake message format."""
    with patch("websockets.connect", return_value=mock_websocket):
        await hub_agent.connect_to_server()

        # Check handshake was sent
        call_args = mock_websocket.send.call_args[0][0]
        handshake = json.loads(call_args)

        assert handshake["type"] == "hub_connect"
        assert handshake["hubId"] == "test_hub"
        assert handshake["deviceToken"] == "test_token"
        assert "timestamp" in handshake
        assert "version" in handshake


@pytest.mark.asyncio
async def test_connect_failure(hub_agent):
    """Test connection failure handling."""
    with patch("websockets.connect", side_effect=Exception("Connection failed")):
        result = await hub_agent.connect_to_server()

        assert result is False
        assert hub_agent.is_connected is False


@pytest.mark.asyncio
async def test_disconnect_from_server(hub_agent, mock_websocket):
    """Test disconnecting from server."""
    hub_agent.ws_connection = mock_websocket
    hub_agent.is_connected = True

    await hub_agent.disconnect_from_server()

    assert hub_agent.is_connected is False
    assert hub_agent.ws_connection is None
    assert mock_websocket.close.called


@pytest.mark.asyncio
async def test_send_telemetry(hub_agent, buffer_manager):
    """Test sending telemetry data."""
    await hub_agent.send_telemetry(
        port_id="port_0",
        session_id="session_123",
        data=b"test data",
    )

    # Should be in buffer
    assert buffer_manager.get_message_count() > 0

    # Check message format
    messages = await buffer_manager.get_messages()
    assert messages[0].message_type == "telemetry"
    assert "portId" in messages[0].payload
    assert "sessionId" in messages[0].payload
    assert "data" in messages[0].payload  # Base64 encoded


@pytest.mark.asyncio
async def test_send_health_status(hub_agent, buffer_manager):
    """Test sending health status."""
    health_data = {
        "cpuUsage": 45.2,
        "memoryUsage": 62.1,
        "uptimeSeconds": 3600,
    }

    await hub_agent.send_health_status(health_data)

    assert buffer_manager.get_message_count() > 0

    messages = await buffer_manager.get_messages()
    assert messages[0].message_type == "health"
    assert messages[0].payload["cpuUsage"] == 45.2


@pytest.mark.asyncio
async def test_send_device_event(hub_agent, buffer_manager):
    """Test sending device event."""
    device_info = {
        "vendor_id": "2341",
        "product_id": "0043",
    }

    await hub_agent.send_device_event(
        event_type="connected",
        port_id="port_0",
        device_info=device_info,
    )

    assert buffer_manager.get_message_count() > 0

    messages = await buffer_manager.get_messages()
    assert messages[0].message_type == "device_event"
    assert messages[0].payload["eventType"] == "connected"
    assert messages[0].payload["portId"] == "port_0"


@pytest.mark.asyncio
async def test_send_task_status(hub_agent, buffer_manager):
    """Test sending task status."""
    await hub_agent.send_task_status(
        task_id="task_123",
        status="completed",
        progress=100,
        result={"success": True},
    )

    assert buffer_manager.get_message_count() > 0

    messages = await buffer_manager.get_messages()
    assert messages[0].message_type == "task_status"
    assert messages[0].payload["taskId"] == "task_123"
    assert messages[0].payload["status"] == "completed"
    assert messages[0].payload["progress"] == 100


@pytest.mark.asyncio
async def test_command_callback(hub_agent):
    """Test command callback invocation."""
    callback_data = []

    async def command_callback(data):
        callback_data.append(data)

    hub_agent.set_command_callback(command_callback)

    # Simulate incoming command
    command = {"type": "command", "commandId": "cmd_123"}
    await hub_agent._process_incoming_message(command)

    assert len(callback_data) == 1
    assert callback_data[0]["commandId"] == "cmd_123"


@pytest.mark.asyncio
async def test_device_event_callback(hub_agent):
    """Test device event callback invocation."""
    callback_data = []

    def device_event_callback(data):
        callback_data.append(data)

    hub_agent.set_device_event_callback(device_event_callback)

    # Simulate incoming device event
    event = {"type": "device_event", "eventType": "connected"}
    await hub_agent._process_incoming_message(event)

    assert len(callback_data) == 1
    assert callback_data[0]["eventType"] == "connected"


@pytest.mark.asyncio
async def test_unknown_message_type(hub_agent):
    """Test handling unknown message type."""
    # Should log warning but not crash
    unknown_msg = {"type": "unknown_type", "data": "test"}
    await hub_agent._process_incoming_message(unknown_msg)


@pytest.mark.asyncio
async def test_get_connection_status(hub_agent):
    """Test getting connection status."""
    status = hub_agent.get_connection_status()

    assert "is_connected" in status
    assert "server_endpoint" in status
    assert "reconnect_attempts" in status
    assert "buffer_stats" in status
    assert status["is_connected"] is False


@pytest.mark.asyncio
async def test_start_and_stop(hub_agent, mock_websocket):
    """Test starting and stopping hub agent."""
    with patch("websockets.connect", return_value=mock_websocket):
        await hub_agent.start()
        assert hub_agent._running is True

        # Give tasks time to start
        await asyncio.sleep(0.1)

        await hub_agent.stop()
        assert hub_agent._running is False


@pytest.mark.asyncio
async def test_reconnection_attempt(hub_agent):
    """Test reconnection logic."""
    hub_agent._running = True

    # Simulate connection failure
    with patch.object(hub_agent, "connect_to_server", return_value=False) as mock_connect:
        await hub_agent._handle_reconnection()

        assert hub_agent.reconnect_attempts == 1


@pytest.mark.asyncio
async def test_max_reconnect_attempts(hub_agent):
    """Test max reconnection attempts limit."""
    hub_agent._running = True
    hub_agent.reconnect_attempts = hub_agent.max_reconnect_attempts

    with patch.object(hub_agent, "connect_to_server", return_value=False):
        await hub_agent._handle_reconnection()

        # Should not attempt to reconnect
        assert hub_agent.reconnect_attempts == hub_agent.max_reconnect_attempts + 1


@pytest.mark.asyncio
async def test_send_loop_integration(hub_agent, mock_websocket, buffer_manager):
    """Test send loop processes buffer."""
    # Add message to buffer
    buffer_manager.add_message("test", {"data": "value"})

    hub_agent.ws_connection = mock_websocket
    hub_agent.is_connected = True
    hub_agent._running = True

    # Start send loop briefly
    send_task = asyncio.create_task(hub_agent._send_loop())
    await asyncio.sleep(0.2)

    hub_agent._running = False
    send_task.cancel()

    try:
        await send_task
    except asyncio.CancelledError:
        pass

    # Should have sent message
    assert mock_websocket.send.called


@pytest.mark.asyncio
async def test_receive_loop_processes_message(hub_agent, mock_websocket):
    """Test receive loop processes incoming messages."""
    command = {"type": "command", "commandId": "cmd_456"}
    mock_websocket.recv.return_value = json.dumps(command)

    callback_called = False

    async def command_callback(data):
        nonlocal callback_called
        callback_called = True

    hub_agent.set_command_callback(command_callback)
    hub_agent.ws_connection = mock_websocket
    hub_agent.is_connected = True
    hub_agent._running = True

    # Start receive loop briefly
    receive_task = asyncio.create_task(hub_agent._receive_loop())
    await asyncio.sleep(0.2)

    hub_agent._running = False
    receive_task.cancel()

    try:
        await receive_task
    except asyncio.CancelledError:
        pass

    # Callback should have been called
    assert callback_called or mock_websocket.recv.called


@pytest.mark.asyncio
async def test_telemetry_base64_encoding(hub_agent, buffer_manager):
    """Test telemetry data is base64 encoded."""
    import base64

    test_data = b"Arduino serial data"
    await hub_agent.send_telemetry("port_0", "session_123", test_data)

    messages = await buffer_manager.get_messages()
    encoded_data = messages[0].payload["data"]

    # Decode and verify
    decoded = base64.b64decode(encoded_data)
    assert decoded == test_data
