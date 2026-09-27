"""Test Serial Manager."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import serial

from src.bench.serial_manager import (
    Connection,
    ConnectionStatus,
    SerialManager,
    SerialWriteError,
)


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch):
    """Skip the DTR-reset and retry delays."""
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr("src.bench.serial_manager.asyncio.sleep", fast_sleep)


@pytest.fixture
def serial_manager():
    """Create Serial Manager instance."""
    return SerialManager(
        max_connections=5,
        task_queue_size=10,
        connection_retry_attempts=3,
        default_timeout=1.0,
    )


@pytest.fixture
def mock_serial():
    """Create mock Serial object."""
    mock = MagicMock(spec=serial.Serial)
    mock.is_open = True
    mock.read.return_value = b"test data"
    mock.write.return_value = 9
    mock.timeout = 1.0
    return mock


@pytest.mark.asyncio
async def test_serial_manager_initialization(serial_manager):
    """Test serial manager initialization."""
    assert serial_manager.max_connections == 5
    assert serial_manager.connection_retry_attempts == 3
    assert len(serial_manager.active_connections) == 0


@pytest.mark.asyncio
async def test_connection_dataclass():
    """Test Connection dataclass."""
    conn = Connection(
        hub_id="hub_01",
        session_id="session_123",
        port_id="port_0",
        device_path="/dev/ttyUSB0",
        baud_rate=115200,
    )

    assert conn.get_device_path() == "/dev/ttyUSB0"
    assert conn.get_baud_rate() == 115200
    assert conn.is_open() is False
    assert conn.status == ConnectionStatus.DISCONNECTED


@pytest.mark.asyncio
async def test_connection_update_activity():
    """Test connection activity update."""
    conn = Connection(
        hub_id="hub_01",
        session_id="session_123",
        port_id="port_0",
        device_path="/dev/ttyUSB0",
        baud_rate=9600,
    )

    initial_time = conn.last_activity_at
    await asyncio.sleep(0.01)
    conn.update_activity()

    assert conn.last_activity_at > initial_time


@pytest.mark.asyncio
async def test_start_and_stop(serial_manager):
    """Test starting and stopping serial manager."""
    await serial_manager.start()
    assert serial_manager._running is True

    await serial_manager.stop()
    assert serial_manager._running is False


@pytest.mark.asyncio
async def test_open_connection_success(serial_manager, mock_serial):
    """Test successful connection opening."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        result = await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
            hub_id="hub_01",
        )

        assert result is True
        assert "port_0" in serial_manager.active_connections
        assert serial_manager.active_connections["port_0"].is_open()

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_open_connection_already_open(serial_manager, mock_serial):
    """Test opening already open connection."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        # Open first time
        await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        # Try to open again
        result = await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        assert result is True  # Should succeed (already open)

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_open_connection_max_connections(serial_manager, mock_serial):
    """Test max connections limit."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        # Open max connections
        for i in range(5):
            await serial_manager.open_connection(
                session_id=f"session_{i}",
                port_id=f"port_{i}",
                device_path=f"/dev/ttyUSB{i}",
                baud_rate=115200,
            )

        # Try to open one more
        result = await serial_manager.open_connection(
            session_id="session_6",
            port_id="port_6",
            device_path="/dev/ttyUSB6",
            baud_rate=115200,
        )

        assert result is False  # Should fail (max reached)
        assert len(serial_manager.active_connections) == 5

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_open_connection_retry_logic(serial_manager):
    """Test connection retry with exponential backoff."""
    mock_serial = MagicMock(spec=serial.Serial)

    with patch("serial.Serial") as mock_serial_class:
        # First two attempts fail, third succeeds
        mock_serial_class.side_effect = [
            serial.SerialException("Port busy"),
            serial.SerialException("Port busy"),
            mock_serial,
        ]

        await serial_manager.start()

        result = await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        assert result is True
        assert mock_serial_class.call_count == 3

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_open_connection_all_retries_fail(serial_manager):
    """Test connection when all retry attempts fail."""
    with patch("serial.Serial") as mock_serial_class:
        # All attempts fail
        mock_serial_class.side_effect = serial.SerialException("Port not found")

        await serial_manager.start()

        result = await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        assert result is False
        assert mock_serial_class.call_count == 3  # All retry attempts

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_close_connection(serial_manager, mock_serial):
    """Test closing connection."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        result = await serial_manager.close_connection("port_0")

        assert result is True
        assert "port_0" not in serial_manager.active_connections
        assert mock_serial.close.called

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_close_connection_not_found(serial_manager):
    """Test closing non-existent connection."""
    await serial_manager.start()

    result = await serial_manager.close_connection("nonexistent")

    assert result is False

    await serial_manager.stop()


@pytest.mark.asyncio
async def test_write_to_port(serial_manager, mock_serial):
    """Test writing to serial port."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        data = b"Hello Arduino"
        mock_serial.write.return_value = len(data)
        result = await serial_manager.write_to_port("port_0", data)

        assert result == len(data)
        assert serial_manager.get_connection("port_0").bytes_written == len(data)
        mock_serial.write.assert_called_once_with(data)

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_write_to_port_not_open(serial_manager):
    """Test writing to unopened port."""
    await serial_manager.start()

    with pytest.raises(SerialWriteError):
        await serial_manager.write_to_port("port_0", b"test")

    await serial_manager.stop()




@pytest.mark.asyncio
async def test_flush_port(serial_manager, mock_serial):
    """Test flushing port buffers."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        result = await serial_manager.flush_port("port_0")

        assert result is True
        assert mock_serial.reset_input_buffer.called
        assert mock_serial.reset_output_buffer.called

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_data_callback(serial_manager, mock_serial):
    """Test data callback invocation."""
    callback_data = []
    received = asyncio.Event()

    async def data_callback(port_id, session_id, data):
        callback_data.append((port_id, session_id, data))
        received.set()

    serial_manager.set_data_callback(data_callback)

    mock_serial.read.return_value = b"callback test"

    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        await asyncio.wait_for(received.wait(), timeout=2)
        assert callback_data[0] == ("port_0", "session_123", b"callback test")

        await serial_manager.stop()




@pytest.mark.asyncio
async def test_get_active_connections(serial_manager, mock_serial):
    """Test getting active connections."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        await serial_manager.open_connection(
            session_id="session_1",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        await serial_manager.open_connection(
            session_id="session_2",
            port_id="port_1",
            device_path="/dev/ttyUSB1",
            baud_rate=9600,
        )

        connections = serial_manager.get_active_connections()

        assert len(connections) == 2
        assert "port_0" in connections
        assert "port_1" in connections

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_get_connection_status(serial_manager, mock_serial):
    """Test getting connection status."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        status = serial_manager.get_connection_status("port_0")
        assert status == ConnectionStatus.CONNECTED

        status = serial_manager.get_connection_status("nonexistent")
        assert status is None

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_get_connection(serial_manager, mock_serial):
    """Test getting connection object."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        conn = serial_manager.get_connection("port_0")
        assert conn is not None
        assert conn.port_id == "port_0"
        assert conn.session_id == "session_123"

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_get_port_error_count(serial_manager, mock_serial):
    """Test getting port error count."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()

        await serial_manager.open_connection(
            session_id="session_123",
            port_id="port_0",
            device_path="/dev/ttyUSB0",
            baud_rate=115200,
        )

        # Initial error count should be 0
        count = serial_manager.get_port_error_count("port_0")
        assert count == 0

        # Simulate an error
        conn = serial_manager.get_connection("port_0")
        conn.error_count = 5

        count = serial_manager.get_port_error_count("port_0")
        assert count == 5

        await serial_manager.stop()


@pytest.mark.asyncio
async def test_open_without_reset_skips_dtr(serial_manager, mock_serial):
    """Native-USB boards are opened without a DTR pulse."""
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()
        await serial_manager.open_connection("s", "port_0", "/dev/ttyACM0", 115200, reset_on_open=False)
        assert mock_serial.dtr is not False  # never pulled low
        await serial_manager.stop()


@pytest.mark.asyncio
async def test_read_error_closes_connection_and_notifies(serial_manager, mock_serial):
    """A failing read unregisters the connection (no self-await) and reports the loss."""
    lost = asyncio.Event()
    seen = []

    async def on_lost(port_id, session_id):
        seen.append((port_id, session_id))
        lost.set()

    mock_serial.read.side_effect = serial.SerialException("device reports readiness to read but returned no data")
    serial_manager.set_disconnect_callback(on_lost)
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()
        await serial_manager.open_connection("session_1", "port_0", "/dev/ttyUSB0", 9600)
        await asyncio.wait_for(lost.wait(), timeout=2)

    assert seen == [("port_0", "session_1")]
    assert serial_manager.get_connection("port_0") is None
    await serial_manager.stop()


@pytest.mark.asyncio
async def test_bytes_read_counted(serial_manager, mock_serial):
    received = asyncio.Event()
    mock_serial.read.side_effect = [b"abc", b"de"] + [b""] * 1000

    def on_data(port_id, session_id, data):
        if serial_manager.get_connection("port_0").bytes_read >= 5:
            received.set()

    serial_manager.set_data_callback(on_data)
    with patch("serial.Serial", return_value=mock_serial):
        await serial_manager.start()
        await serial_manager.open_connection("s", "port_0", "/dev/ttyUSB0", 9600)
        await asyncio.wait_for(received.wait(), timeout=2)
        assert serial_manager.get_connection("port_0").bytes_read == 5
        await serial_manager.stop()
