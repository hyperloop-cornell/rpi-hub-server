"""Test USB Port Mapper."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.bench.usb_port_mapper import DeviceInfo, MappedConnection, USBPortMapper


@pytest.fixture
def temp_persistence_path(tmp_path):
    """Create temporary persistence path."""
    return str(tmp_path / "port_mappings.json")


@pytest.fixture
def usb_mapper(temp_persistence_path):
    """Create USB Port Mapper instance."""
    return USBPortMapper(
        persistence_path=temp_persistence_path,
        default_baud_rate=9600,
    )


@pytest.fixture
def mock_pyserial_ports():
    """Mock pyserial port list."""
    port1 = Mock()
    port1.device = "/dev/ttyUSB0"
    port1.vid = 0x2341
    port1.pid = 0x0043
    port1.serial_number = "12345"
    port1.manufacturer = "Arduino"
    port1.product = "Arduino Uno"
    port1.location = "1-1.1"
    port1.description = "Arduino Uno"
    port1.hwid = "USB VID:PID=2341:0043"

    port2 = Mock()
    port2.device = "/dev/ttyUSB1"
    port2.vid = 0x1A86
    port2.pid = 0x7523
    port2.serial_number = "54321"
    port2.manufacturer = "QinHeng Electronics"
    port2.product = "USB Serial"
    port2.location = "1-1.2"
    port2.description = "USB Serial"
    port2.hwid = "USB VID:PID=1A86:7523"

    return [port1, port2]


@pytest.fixture
def mock_arduino_cli_output():
    """Mock arduino-cli board list output."""
    return {
        "detected_ports": [
            {
                "matching_boards": [
                    {"name": "Arduino Uno", "fqbn": "arduino:avr:uno"}
                ],
                "port": {
                    "address": "/dev/ttyUSB0",
                    "protocol": "serial",
                    "protocol_label": "Serial Port (USB)",
                    "properties": {
                        "pid": "0043",
                        "vid": "2341",
                        "serialNumber": "12345",
                    },
                },
            }
        ]
    }


@pytest.mark.asyncio
async def test_usb_mapper_initialization(usb_mapper):
    """Test USB mapper initialization."""
    assert usb_mapper.default_baud_rate == 9600
    assert len(usb_mapper.device_path_to_port_id) == 0
    assert len(usb_mapper.port_id_to_device_info) == 0
    assert usb_mapper.last_scan_time is None




@pytest.mark.asyncio
async def test_detect_with_pyserial(usb_mapper, mock_pyserial_ports):
    """Test port detection with pyserial."""
    with patch("serial.tools.list_ports.comports", return_value=mock_pyserial_ports):
        devices = await usb_mapper._detect_with_pyserial()

        assert len(devices) == 2
        assert devices[0].device_path == "/dev/ttyUSB0"
        assert devices[0].vendor_id == "2341"
        assert devices[0].product_id == "0043"
        assert devices[0].serial_number == "12345"
        assert devices[0].detected_baud == 9600  # Default



@pytest.mark.asyncio
async def test_get_port_id_stable(usb_mapper):
    """Test stable port ID generation."""
    device_info = DeviceInfo(
        port_id="",
        device_path="/dev/ttyUSB0",
        vendor_id="2341",
        product_id="0043",
        serial_number="12345",
        location="1-1.1",
    )

    port_id1 = await usb_mapper.get_port_id("/dev/ttyUSB0", device_info)
    port_id2 = await usb_mapper.get_port_id("/dev/ttyUSB0", device_info)

    # Should return same ID
    assert port_id1 == port_id2
    assert port_id1.startswith("port_")


@pytest.mark.asyncio
async def test_get_port_id_different_devices(usb_mapper):
    """Test different devices get different port IDs."""
    device_info1 = DeviceInfo(
        port_id="",
        device_path="/dev/ttyUSB0",
        serial_number="12345",
        location="1-1.1",
    )

    device_info2 = DeviceInfo(
        port_id="",
        device_path="/dev/ttyUSB1",
        serial_number="54321",
        location="1-1.2",
    )

    port_id1 = await usb_mapper.get_port_id("/dev/ttyUSB0", device_info1)
    port_id2 = await usb_mapper.get_port_id("/dev/ttyUSB1", device_info2)

    assert port_id1 != port_id2




@pytest.mark.asyncio
async def test_refresh_new_device(usb_mapper, mock_pyserial_ports):
    """Test refresh detects new devices."""
    callback_called = False
    detected_device = None

    def callback(device_info):
        nonlocal callback_called, detected_device
        callback_called = True
        detected_device = device_info

    usb_mapper.on_device_connected(callback)

    with patch("serial.tools.list_ports.comports", return_value=mock_pyserial_ports):
        usb_mapper._arduino_cli_available = False
        await usb_mapper.refresh()

    assert callback_called
    assert detected_device is not None
    assert len(usb_mapper.port_id_to_device_info) == 2


@pytest.mark.asyncio
async def test_refresh_removed_device(usb_mapper, mock_pyserial_ports):
    """Test refresh detects removed devices."""
    # Add device
    device_info = DeviceInfo(
        port_id="port_test",
        device_path="/dev/ttyUSB9",
    )
    usb_mapper.device_path_to_port_id["/dev/ttyUSB9"] = "port_test"
    usb_mapper.port_id_to_device_info["port_test"] = device_info

    callback_called = False

    def callback(device_info):
        nonlocal callback_called
        callback_called = True

    usb_mapper.on_device_disconnected(callback)

    with patch("serial.tools.list_ports.comports", return_value=mock_pyserial_ports):
        usb_mapper._arduino_cli_available = False
        await usb_mapper.refresh()

    assert callback_called
    assert "port_test" not in usb_mapper.port_id_to_device_info


@pytest.mark.asyncio
async def test_get_device_info(usb_mapper):
    """Test get device info."""
    device_info = DeviceInfo(
        port_id="port_test",
        device_path="/dev/ttyUSB0",
    )
    usb_mapper.port_id_to_device_info["port_test"] = device_info

    result = await usb_mapper.get_device_info("port_test")
    assert result == device_info

    result = await usb_mapper.get_device_info("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_list_mapped_connections(usb_mapper):
    """Test list mapped connections."""
    device_info1 = DeviceInfo(
        port_id="port_1",
        device_path="/dev/ttyUSB0",
    )
    device_info2 = DeviceInfo(
        port_id="port_2",
        device_path="/dev/ttyUSB1",
    )

    usb_mapper.port_id_to_device_info["port_1"] = device_info1
    usb_mapper.port_id_to_device_info["port_2"] = device_info2

    connections = await usb_mapper.list_mapped_connections()

    assert len(connections) == 2
    assert all(isinstance(c, MappedConnection) for c in connections)
    assert all(c.is_available for c in connections)


@pytest.mark.asyncio
async def test_remove_mapping(usb_mapper):
    """Test remove mapping."""
    device_info = DeviceInfo(
        port_id="port_test",
        device_path="/dev/ttyUSB0",
    )
    usb_mapper.device_path_to_port_id["/dev/ttyUSB0"] = "port_test"
    usb_mapper.port_id_to_device_info["port_test"] = device_info

    result = await usb_mapper.remove_mapping("port_test")
    assert result is True
    assert "port_test" not in usb_mapper.port_id_to_device_info
    assert "/dev/ttyUSB0" not in usb_mapper.device_path_to_port_id

    result = await usb_mapper.remove_mapping("nonexistent")
    assert result is False




@pytest.mark.asyncio
async def test_start_and_stop(usb_mapper, mock_pyserial_ports):
    """Test start and stop scanning."""
    with patch("serial.tools.list_ports.comports", return_value=mock_pyserial_ports):
        usb_mapper._arduino_cli_available = False

        await usb_mapper.start(scan_interval=1)
        assert usb_mapper._running is True
        assert usb_mapper._scan_task is not None

        # Let it run briefly
        await asyncio.sleep(0.1)

        await usb_mapper.stop()
        assert usb_mapper._running is False


@pytest.mark.asyncio
async def test_async_callback(usb_mapper, mock_pyserial_ports):
    """Test async callback for device connection."""
    callback_called = False

    async def async_callback(device_info):
        nonlocal callback_called
        callback_called = True
        await asyncio.sleep(0.01)  # Simulate async work

    usb_mapper.on_device_connected(async_callback)

    with patch("serial.tools.list_ports.comports", return_value=mock_pyserial_ports):
        usb_mapper._arduino_cli_available = False
        await usb_mapper.refresh()

    assert callback_called


@pytest.mark.asyncio
async def test_baud_policy_skips_probing(temp_persistence_path, mock_pyserial_ports):
    """A known board's baud comes from policy; 0 means "do not probe" (device will not be opened)."""
    policy = {"2341": 115200, "1a86": 0}
    mapper = USBPortMapper(
        persistence_path=temp_persistence_path,
        baud_policy=lambda info: policy.get(info.vendor_id),
    )
    with patch("serial.tools.list_ports.comports", return_value=mock_pyserial_ports), \
            patch.object(mapper, "_detect_baud_rate_manual", new=AsyncMock(return_value=9600)) as probe:
        await mapper.refresh()

    probe.assert_not_called()
    bauds = {info.device_path: info.detected_baud for info in mapper.port_id_to_device_info.values()}
    assert bauds == {"/dev/ttyUSB0": 115200, "/dev/ttyUSB1": None}


@pytest.mark.asyncio
async def test_suppressed_board_callbacks_replayed_on_release(usb_mapper, mock_pyserial_ports):
    """While a board re-enumerates during flashing its hotplug callbacks are held back, then replayed."""
    connected, disconnected = [], []
    usb_mapper.on_device_connected(lambda info: connected.append(info.device_path))
    usb_mapper.on_device_disconnected(lambda info: disconnected.append(info.device_path))

    with patch("serial.tools.list_ports.comports", return_value=mock_pyserial_ports):
        await usb_mapper.refresh()
    assert sorted(connected) == ["/dev/ttyUSB0", "/dev/ttyUSB1"]
    connected.clear()

    uno = next(i for i in usb_mapper.port_id_to_device_info.values() if i.device_path == "/dev/ttyUSB0")
    keys = usb_mapper.suppress(uno)
    assert "loc:1-1.1" in keys and "sn:12345" in keys

    # Board disappears (bootloader touch) and comes back on a new path with the same USB location
    with patch("serial.tools.list_ports.comports", return_value=[mock_pyserial_ports[1]]):
        await usb_mapper.refresh()
    returned = Mock(**{a: getattr(mock_pyserial_ports[0], a) for a in
                       ("vid", "pid", "serial_number", "manufacturer", "product", "location", "description", "hwid")})
    returned.device = "/dev/ttyACM0"
    with patch("serial.tools.list_ports.comports", return_value=[returned, mock_pyserial_ports[1]]):
        await usb_mapper.refresh()
        assert connected == [] and disconnected == []

        await usb_mapper.release(keys)

    assert connected == ["/dev/ttyACM0"]
    assert disconnected == []
