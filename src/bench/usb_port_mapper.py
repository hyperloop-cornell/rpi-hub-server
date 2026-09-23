"""USB Port Mapper with hotplug detection and iterative baud rate detection."""

import asyncio
import hashlib
import json
import serial
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

import serial.tools.list_ports

from src.logging_config import StructuredLogger

# Baud rate candidates to try (Arduino/ESP32 defaults first, then comprehensive list)
BAUD_RATE_CANDIDATES = [
    # Arduino defaults
    9600,
    115200,
]


@dataclass
class DeviceInfo:
    """USB device information."""

    port_id: str
    device_path: str
    vendor_id: Optional[str] = None
    product_id: Optional[str] = None
    serial_number: Optional[str] = None
    manufacturer: Optional[str] = None
    product: Optional[str] = None
    location: Optional[str] = None
    detected_baud: Optional[int] = None
    description: str = ""
    hwid: str = ""
    source: str = "pyserial"  # Detection source


@dataclass
class MappedConnection:
    """Mapped connection information."""

    port_id: str
    device_path: str
    device_info: DeviceInfo
    is_available: bool = True
    last_seen: datetime = field(default_factory=datetime.now)


class USBPortMapper:
    """Manages USB port detection and stable ID mapping."""

    def __init__(
        self,
        persistence_path: str = "/var/lib/rpi-hub/port_mappings.json",
        default_baud_rate: int = 9600,
        baud_policy: Optional[Callable[["DeviceInfo"], Optional[int]]] = None,
    ):
        """Initialize USB Port Mapper.

        Args:
            persistence_path: Path to save port mappings
            default_baud_rate: Default baud rate fallback
            baud_policy: Called for each new device. Return a baud rate to use it without
                probing, 0 to skip probing (device will not be auto-connected anyway), or
                None to probe candidate baud rates.
        """
        self.logger = StructuredLogger(__name__)
        self.persistence_path = Path(persistence_path)
        self.default_baud_rate = default_baud_rate
        self.baud_policy = baud_policy

        # USB identities (location or serial number) whose hotplug callbacks are held back,
        # e.g. while a board re-enumerates during flashing
        self._suppressed: Set[str] = set()

        # Mappings (live devices only - no cross-run caching)
        self.device_path_to_port_id: Dict[str, str] = {}
        self.port_id_to_device_info: Dict[str, DeviceInfo] = {}
        
        # Cache device signatures to avoid recreating DeviceInfo unnecessarily
        self._device_signatures: Dict[str, str] = {}  # device_path -> signature hash

        # Tracking
        self.last_scan_time: Optional[datetime] = None
        self._scan_task: Optional[asyncio.Task] = None
        self._running = False

        # Hotplug callbacks
        self._device_connected_callbacks: List = []
        self._device_disconnected_callbacks: List = []

        self.logger.info("usb_mapper_initialized", "USB Port Mapper initialized")

    async def start(self, scan_interval: int = 2) -> None:
        """Start continuous port scanning.

        Args:
            scan_interval: Scan interval in seconds
        """
        self._running = True
        
        # Clean up any existing persistence file to prevent stale data
        try:
            if self.persistence_path.exists():
                self.persistence_path.unlink()
                self.logger.info(
                    "persistence_cleaned",
                    f"Removed stale persistence file: {self.persistence_path}"
                )
        except Exception as e:
            self.logger.warning(
                "persistence_cleanup_failed",
                f"Could not remove persistence file: {e}",
                error=str(e),
            )

        # Initial scan
        await self.refresh()

        # Start scanning loop
        self._scan_task = asyncio.create_task(self._scan_loop(scan_interval))
        self.logger.info(
            "usb_mapper_started",
            "USB Port Mapper scanning started",
            scan_interval=scan_interval,
        )

    async def stop(self) -> None:
        """Stop port scanning."""
        self._running = False
        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass

        self.logger.info("usb_mapper_stopped", "USB Port Mapper stopped")

    async def _scan_loop(self, interval: int) -> None:
        """Continuous scanning loop.

        Args:
            interval: Scan interval in seconds
        """
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self.refresh()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(
                    "scan_loop_error",
                    f"Error in scan loop: {e}",
                    error=str(e),
                )

    async def refresh(self) -> None:
        """Scan for USB devices and update mappings."""
        scan_start = datetime.now()

        # Get current ports from scan
        current_devices = await self._list_ports_with_baud()
        current_paths = {dev.device_path for dev in current_devices}
        
        new_devices = []
        updated_devices = []
        removed_device_ids = []

        # Process all current devices
        for device_info in current_devices:
            # Generate stable port_id from device characteristics
            port_id = await self.get_port_id(device_info.device_path, device_info)
            device_info.port_id = port_id
            
            # Debug log to verify device_path is correct
            self.logger.debug(
                "device_processing",
                f"Processing device: port_id={port_id}, device_path={device_info.device_path}",
                port_id=port_id,
                device_path=device_info.device_path,
                vendor_id=device_info.vendor_id,
                product_id=device_info.product_id,
                serial_number=device_info.serial_number,
                manufacturer=device_info.manufacturer,
                location=device_info.location,
            )
            
            # Check if this Arduino's serial number already exists in a different mapping
            if device_info.serial_number:
                for existing_port_id, existing_device in list(self.port_id_to_device_info.items()):
                    # If same serial number but different port_id/device_path, remove old mapping
                    if (existing_device.serial_number == device_info.serial_number and 
                        existing_device.device_path != device_info.device_path):
                        self.logger.info(
                            "duplicate_serial_removed",
                            f"Removing old mapping for Arduino with serial {device_info.serial_number}",
                            old_port_id=existing_port_id,
                            old_device_path=existing_device.device_path,
                            new_port_id=port_id,
                            new_device_path=device_info.device_path,
                            serial_number=device_info.serial_number,
                        )
                        # Clean up old mapping
                        self.device_path_to_port_id.pop(existing_device.device_path, None)
                        self.port_id_to_device_info.pop(existing_port_id, None)
                        self._device_signatures.pop(existing_device.device_path, None)
                        removed_device_ids.append(existing_port_id)
            
            # Check if this is a new device or existing
            is_new = device_info.device_path not in self.device_path_to_port_id
            
            # Update mappings with fresh data
            self.port_id_to_device_info[port_id] = device_info
            self.device_path_to_port_id[device_info.device_path] = port_id
            
            if is_new:
                new_devices.append(device_info)

                # Trigger callbacks for new devices
                for callback in ([] if self.is_suppressed(device_info) else self._device_connected_callbacks):
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(device_info)
                        else:
                            callback(device_info)
                    except Exception as e:
                        self.logger.error(
                            "callback_error",
                            f"Error in device connected callback: {e}",
                            error=str(e),
                        )

                self.logger.port_detected(
                    port_id,
                    device_info.device_path,
                    vendor_id=device_info.vendor_id,
                    product_id=device_info.product_id,
                    detected_baud=device_info.detected_baud,
                    source=device_info.source,
                )
            else:
                updated_devices.append(device_info)

        # Process removed devices
        for device_path, port_id in list(self.device_path_to_port_id.items()):
            if device_path not in current_paths:
                removed_device_ids.append(port_id)
                device_info = self.port_id_to_device_info.get(port_id)
                if device_info:
                    # Trigger callbacks
                    for callback in ([] if self.is_suppressed(device_info) else self._device_disconnected_callbacks):
                        try:
                            if asyncio.iscoroutinefunction(callback):
                                await callback(device_info)
                            else:
                                callback(device_info)
                        except Exception as e:
                            self.logger.error(
                                "callback_error",
                                f"Error in device disconnected callback: {e}",
                                error=str(e),
                            )

                    self.logger.port_removed(port_id, device_path=device_info.device_path)

                # Clean up mappings and signature cache
                self.device_path_to_port_id.pop(device_path, None)
                self.port_id_to_device_info.pop(port_id, None)
                self._device_signatures.pop(device_path, None)

        self.last_scan_time = datetime.now()
        scan_duration = (self.last_scan_time - scan_start).total_seconds() * 1000

        changed = bool(new_devices or removed_device_ids)
        (self.logger.info if changed else self.logger.debug)(
            "port_scan_completed",
            f"Port scan completed in {scan_duration:.0f}ms",
            duration_ms=scan_duration,
            new_devices=len(new_devices),
            updated_devices=len(updated_devices),
            removed_devices=len(removed_device_ids),
            total_ports=len(self.port_id_to_device_info),
        )

    def _has_detected_baud(self, device_path: str) -> Optional[int]:
        """Check if device already has a detected baud rate in memory.
        
        This is an in-memory cache only (not file-based) to avoid re-detecting
        baud rates during periodic scans of already-connected devices.
        
        Args:
            device_path: Device path to check
            
        Returns:
            Detected baud rate if found, None otherwise
        """
        # Check if this device path is already tracked
        port_id = self.device_path_to_port_id.get(device_path)
        if port_id:
            device_info = self.port_id_to_device_info.get(port_id)
            if device_info and device_info.detected_baud is not None:
                return device_info.detected_baud
        return None
    
    def _compute_device_signature(self, port) -> str:
        """Compute a signature hash for a device to detect changes.
        
        Args:
            port: Serial port info object
            
        Returns:
            SHA256 hash of device properties
        """
        vendor_id = f"{port.vid:04x}" if port.vid else "none"
        product_id = f"{port.pid:04x}" if port.pid else "none"
        signature_str = f"{port.device}|{vendor_id}|{product_id}|{port.serial_number or ''}|{port.location or ''}"
        return hashlib.sha256(signature_str.encode()).hexdigest()[:16]

    def _score_serial_data(self, data: bytes) -> int:
        """Score serial data based on ASCII-like content.
        
        Args:
            data: Raw bytes received from serial port
            
        Returns:
            Score (higher is better)
        """
        if not data:
            return 0
        # Count printable ASCII characters and common control chars (tab, newline, carriage return)
        return sum(32 <= b <= 126 or b in (9, 10, 13) for b in data)

    async def _detect_baud_rate_manual(self, device_path: str) -> int:
        """Detect baud rate using manual scoring approach.
        
        Tries each baud rate candidate and scores the received data.
        Returns the baud rate with the highest score.

        Args:
            device_path: Serial port device path

        Returns:
            Detected baud rate with best score
        """
        loop = asyncio.get_event_loop()
        
        def try_baud_rate(baud: int) -> tuple[int, int]:
            """Try a baud rate and return (baud, score)."""
            try:
                ser = serial.Serial(
                    device_path,
                    baudrate=baud,
                    timeout=0.2,
                    # Use conservative settings for compatibility
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                )
                
                # Clear any existing data in buffer
                ser.reset_input_buffer()
                
                # Reset Arduino by toggling DTR (triggers reset on most Arduino boards)
                ser.dtr = False
                time.sleep(0.1)
                ser.dtr = True
                
                # Wait for device to reset and start transmitting
                time.sleep(2.0)
                
                # Read up to 200 bytes
                data = ser.read(200)
                ser.close()
                score = self._score_serial_data(data)
                return (baud, score)
            except (serial.SerialException, OSError) as e:
                # Port may be busy or not accessible
                self.logger.debug(
                    "baud_test_failed",
                    f"Failed to test baud {baud} on {device_path}: {e}",
                    baud=baud,
                    device_path=device_path,
                )
                return (baud, 0)
        
        best_baud = 9600  # Default fallback
        best_score = -1
        
        # Try all baud rate candidates
        for baud in BAUD_RATE_CANDIDATES:
            baud_rate, score = await loop.run_in_executor(None, try_baud_rate, baud)
            
            self.logger.debug(
                "baud_test_score",
                f"Baud {baud_rate} scored {score} on {device_path}",
                baud=baud_rate,
                score=score,
                device_path=device_path,
            )
            
            if score > best_score:
                best_score = score
                best_baud = baud_rate
        
        self.logger.info(
            "baud_detected_manual",
            f"Detected baud rate {best_baud} (score: {best_score}) for {device_path}",
            device_path=device_path,
            baud_rate=best_baud,
            score=best_score,
        )
        
        return best_baud

    async def _list_ports_with_baud(self) -> List[DeviceInfo]:
        """List all serial ports with iterative baud rate detection.

        Returns:
            List of device information
        """
        return await self._detect_with_pyserial()

    async def _detect_with_pyserial(self) -> List[DeviceInfo]:
        """Detect ports using pyserial with iterative baud rate detection.

        Returns:
            List of detected devices
        """
        devices = []

        # Run blocking operation in executor
        loop = asyncio.get_event_loop()
        ports = await loop.run_in_executor(None, serial.tools.list_ports.comports)

        for port in ports:
            # Skip built-in serial ports (not USB devices)
            # Built-in ports typically have no vendor ID and match certain patterns
            if port.vid is None:
                # Check if it's a built-in hardware serial port
                device_lower = port.device.lower()
                if any(pattern in device_lower for pattern in ['/dev/ttys', '/dev/ttyama', 'com1', 'com2']):
                    self.logger.debug(
                        "skipping_builtin_port",
                        f"Skipping built-in serial port {port.device}",
                        device_path=port.device,
                    )
                    continue
            
            # Compute device signature to check if it changed
            current_signature = self._compute_device_signature(port)
            cached_signature = self._device_signatures.get(port.device)
            
            # If device signature matches cache, reuse existing DeviceInfo
            if cached_signature == current_signature:
                port_id = self.device_path_to_port_id.get(port.device)
                if port_id:
                    cached_device = self.port_id_to_device_info.get(port_id)
                    if cached_device:
                        # Device unchanged - reuse cached object
                        devices.append(cached_device)
                        continue
            
            # Device is new or changed - create/update DeviceInfo
            vendor_id = f"{port.vid:04x}" if port.vid else None
            product_id = f"{port.pid:04x}" if port.pid else None
            
            # Check if we already have a detected baud for this device (in-memory)
            detected_baud = self._has_detected_baud(port.device)
            
            policy_baud = None
            if detected_baud is None and self.baud_policy is not None:
                probe_info = DeviceInfo(
                    port_id="",
                    device_path=port.device,
                    vendor_id=vendor_id,
                    product_id=product_id,
                    serial_number=port.serial_number,
                    manufacturer=port.manufacturer,
                    product=port.product,
                    location=port.location,
                    description=port.description,
                )
                policy_baud = self.baud_policy(probe_info)

            if detected_baud is not None:
                # Device already known in this session - skip detection
                self.logger.debug(
                    "baud_skipped_known_device",
                    f"Skipping baud detection for known device {port.device} (using {detected_baud})",
                    device_path=port.device,
                    baud_rate=detected_baud,
                )
            elif policy_baud is not None:
                # Known board (or a device that will not be opened): no probing
                detected_baud = policy_baud or None
            else:
                # New device - detect baud rate
                detected_baud = await self._detect_baud_rate_manual(port.device)
            
            device_info = DeviceInfo(
                port_id="",  # Will be set later
                device_path=port.device,
                vendor_id=vendor_id,
                product_id=product_id,
                serial_number=port.serial_number,
                manufacturer=port.manufacturer,
                product=port.product,
                location=port.location,
                description=port.description,
                hwid=port.hwid,
                detected_baud=detected_baud,
                source="pyserial",
            )
            
            # Update signature cache
            self._device_signatures[port.device] = current_signature
            
            # Only log when device info is actually created/changed
            if cached_signature != current_signature:
                self.logger.info(
                    "device_info_created",
                    f"Created DeviceInfo: device_path={port.device}, vendor_id={vendor_id}, product_id={product_id}",
                    device_path=port.device,
                    vendor_id=vendor_id,
                    product_id=product_id,
                    serial_number=port.serial_number,
                    manufacturer=port.manufacturer,
                    location=port.location,
                )

            devices.append(device_info)

        return devices

    async def get_port_id(
        self, device_path: str, device_info: Optional[DeviceInfo] = None
    ) -> str:
        """Get stable port ID for a device path.

        Generates consistent ID based on USB location and serial number.

        Args:
            device_path: Device path (e.g., /dev/ttyUSB0)
            device_info: Optional device information

        Returns:
            Stable port ID
        """
        # Generate stable ID from device characteristics
        if device_info:
            # Use location and serial number for stable ID, fallback to device path if no unique identifiers
            id_source = (
                f"{device_info.location or ''}"
                f"{device_info.serial_number or ''}"
                f"{device_info.vendor_id or ''}"
                f"{device_info.product_id or ''}"
            )
            # If no unique identifiers available, include device path to ensure uniqueness
            if not id_source or id_source == "":
                id_source = device_path
        else:
            # Fallback to device path
            id_source = device_path

        # Generate short hash
        hash_obj = hashlib.sha256(id_source.encode())
        port_id = f"port_{hash_obj.hexdigest()[:8]}"

        return port_id

    async def get_device_info(self, port_id: str) -> Optional[DeviceInfo]:
        """Get device information for a port ID.

        Args:
            port_id: Port identifier

        Returns:
            Device information or None if not found
        """
        return self.port_id_to_device_info.get(port_id)

    def get_all_devices(self) -> List[Dict[str, any]]:
        """Get all devices as dictionaries for API responses.

        Returns:
            List of device information dictionaries
        """
        devices = []
        for port_id, device_info in self.port_id_to_device_info.items():
            device_dict = {
                "port_id": port_id,
                "port": device_info.device_path,
                "description": device_info.description,
                "manufacturer": device_info.manufacturer,
                "serial_number": device_info.serial_number,
                "vendor_id": device_info.vendor_id,
                "product_id": device_info.product_id,
                "detected_baud": device_info.detected_baud,
            }
            
            # Debug log
            self.logger.debug(
                "get_all_devices_entry",
                f"Device {port_id}: device_path={device_info.device_path}",
                port_id=port_id,
                device_path=device_info.device_path,
                device_dict=device_dict,
            )
            
            devices.append(device_dict)
        
        self.logger.debug(
            "get_all_devices",
            f"Returning {len(devices)} devices",
            count=len(devices),
            port_ids=list(self.port_id_to_device_info.keys()),
        )
        
        return devices

    def get_device_by_id(self, port_id: str) -> Optional[Dict[str, any]]:
        """Get device by port ID as dictionary for API responses.

        Args:
            port_id: Port identifier

        Returns:
            Device information dictionary or None if not found
        """
        device_info = self.port_id_to_device_info.get(port_id)
        if not device_info:
            return None
        
        return {
            "port_id": port_id,
            "port": device_info.device_path,
            "description": device_info.description,
            "manufacturer": device_info.manufacturer,
            "serial_number": device_info.serial_number,
            "vendor_id": device_info.vendor_id,
            "product_id": device_info.product_id,
            "detected_baud": device_info.detected_baud,
        }

    async def list_mapped_connections(self) -> List[MappedConnection]:
        """List all mapped connections.

        Returns:
            List of mapped connections
        """
        connections = []
        for port_id, device_info in self.port_id_to_device_info.items():
            connection = MappedConnection(
                port_id=port_id,
                device_path=device_info.device_path,
                device_info=device_info,
                is_available=True,
            )
            connections.append(connection)

        return connections

    async def remove_mapping(self, port_id: str) -> bool:
        """Remove a port mapping.

        Args:
            port_id: Port identifier to remove

        Returns:
            True if removed, False if not found
        """
        device_info = self.port_id_to_device_info.get(port_id)
        if not device_info:
            return False

        self.device_path_to_port_id.pop(device_info.device_path, None)
        self.port_id_to_device_info.pop(port_id, None)

        self.logger.info(
            "mapping_removed",
            f"Removed mapping for {port_id}",
            port_id=port_id,
        )

        return True

    def on_device_connected(self, callback) -> None:
        """Register callback for device connection events.

        Args:
            callback: Async or sync function(device_info: DeviceInfo)
        """
        self._device_connected_callbacks.append(callback)

    def on_device_disconnected(self, callback) -> None:
        """Register callback for device disconnection events.

        Args:
            callback: Async or sync function(device_info: DeviceInfo)
        """
        self._device_disconnected_callbacks.append(callback)

    # Hotplug suppression

    @staticmethod
    def identity_keys(device_info: "DeviceInfo") -> Set[str]:
        """Keys that identify a physical board across re-enumeration (USB path, serial number)."""
        keys = set()
        if device_info.location:
            keys.add("loc:" + device_info.location.split(":")[0])
        if device_info.serial_number:
            keys.add("sn:" + device_info.serial_number)
        return keys

    def is_suppressed(self, device_info: "DeviceInfo") -> bool:
        return bool(self.identity_keys(device_info) & self._suppressed)

    def suppress(self, device_info: "DeviceInfo") -> Set[str]:
        """Hold back hotplug callbacks for this board; returns the keys to pass to release()."""
        keys = self.identity_keys(device_info)
        self._suppressed |= keys
        return keys

    async def release(self, keys: Set[str]) -> None:
        """Stop suppressing and replay 'connected' for matching devices that are present now."""
        self._suppressed -= keys
        await self.refresh()
        for device_info in list(self.port_id_to_device_info.values()):
            if self.identity_keys(device_info) & keys:
                for callback in self._device_connected_callbacks:
                    try:
                        result = callback(device_info)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        self.logger.error("callback_error", f"Error in device connected callback: {e}", error=str(e))
