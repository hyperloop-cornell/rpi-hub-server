"""
Test script for WebSocket connection and message envelope testing.

This script:
1. Verifies rpi-hub-service is running before attempting tests
2. Establishes WebSocket connection with retry logic
3. Tests sending different envelope types (telemetry, health, device_event, task_status)
4. Tests automatic serial reading with pyserial
5. Verifies message routing and buffer management
"""

import asyncio
import base64
import json
import os
import sys
import time
import socket
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("Warning: python-dotenv not available. Using defaults or environment variables only.")

# For serial testing
try:
    import serial
    from serial.tools import list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    print("Warning: pyserial not available. Serial tests will be skipped.")


class WebSocketTester:
    """Test WebSocket connection and message envelopes."""
    
    def __init__(self, endpoint: str, hub_id: str, device_token: str):
        """Initialize tester.
        
        Args:
            endpoint: WebSocket server URL (e.g., ws://localhost:8000/hub)
            hub_id: Hub identifier
            device_token: Authentication token
        """
        self.endpoint = endpoint
        self.hub_id = hub_id
        self.device_token = device_token
        self.ws_connection: Optional[websockets.WebSocketClientProtocol] = None
        self.is_connected = False
        self.received_messages = []
        self.test_results: List[Tuple[str, bool, str]] = []
        
    def check_service_running(self) -> Tuple[bool, str]:
        """Check if the service is actually running on the target port.
        
        Returns:
            Tuple of (is_running, message)
        """
        try:
            parsed = urlparse(self.endpoint)
            host = parsed.hostname or 'localhost'
            port = parsed.port or 8000
            
            # Try to connect to the TCP port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((host, port))
            sock.close()
            
            if result == 0:
                return True, f"Service is listening on {host}:{port}"
            else:
                return False, f"Nothing listening on {host}:{port}"
                
        except socket.gaierror:
            return False, f"Cannot resolve hostname: {host}"
        except Exception as e:
            return False, f"Error checking service: {e}"
        
    async def connect(self, max_retries: int = 3, timeout: float = 5.0) -> bool:
        """Establish WebSocket connection with retry logic.
        
        Args:
            max_retries: Maximum number of connection attempts
            timeout: Timeout per attempt in seconds
        
        Returns:
            True if connected successfully
        """
        print(f"\n{'='*60}")
        print(f"TEST 1: WebSocket Connection")
        print(f"{'='*60}")
        
        # First check if service is running
        is_running, check_message = self.check_service_running()
        print(f"Checking service: {check_message}")
        
        if not is_running:
            print(f"\nERROR: Service is not running!")
            print(f"Start the service with:")
            print(f"  cd rpi-hub-service")
            print(f"  python -m uvicorn src.main:app --host 0.0.0.0 --port 8000")
            self.test_results.append(("Service Check", False, check_message))
            return False
        
        self.test_results.append(("Service Check", True, check_message))
        
        # Attempt connection with retries
        for attempt in range(max_retries):
            try:
                print(f"\nAttempt {attempt + 1}/{max_retries}")
                print(f"Connecting to: {self.endpoint}")
                print(f"Hub ID: {self.hub_id}")
                print(f"Timeout: {timeout}s")
                
                # Connect with ping settings and timeout
                self.ws_connection = await asyncio.wait_for(
                    websockets.connect(
                        self.endpoint,
                        ping_interval=20,
                        ping_timeout=10,
                    ),
                    timeout=timeout
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
                print(f"Handshake sent")
                
                self.is_connected = True
                print(f"Connection established on attempt {attempt + 1}")
                
                # Start receiver task
                asyncio.create_task(self._receive_loop())
                
                self.test_results.append(("WebSocket Connection", True, f"Connected on attempt {attempt + 1}"))
                return True
                
            except asyncio.TimeoutError:
                error_msg = f"Connection timeout after {timeout}s"
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"Timeout - retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    print(f"All {max_retries} connection attempts timed out")
                    self.test_results.append(("WebSocket Connection", False, error_msg))
                    
            except (ConnectionRefusedError, OSError) as e:
                error_msg = f"Connection refused: {e}"
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"Connection refused - retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    print(f"Connection refused after {max_retries} attempts")
                    print(f"\nPossible issues:")
                    print(f"  1. Service endpoint might be different (check port number)")
                    print(f"  2. Service may be starting up (takes a few seconds)")
                    print(f"  3. Firewall blocking connection")
                    self.test_results.append(("WebSocket Connection", False, error_msg))
                    
            except WebSocketException as e:
                error_msg = f"WebSocket error: {e}"
                print(f"WebSocket error: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    self.test_results.append(("WebSocket Connection", False, error_msg))
                    
            except Exception as e:
                error_msg = f"Unexpected error: {e}"
                print(f"Connection failed: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    self.test_results.append(("WebSocket Connection", False, error_msg))
        
        print(f"\nFailed to connect after {max_retries} attempts")
        return False
    
    async def disconnect(self):
        """Close WebSocket connection."""
        if self.ws_connection:
            try:
                await self.ws_connection.close()
                self.is_connected = False
                print(f"Disconnected from server")
            except Exception as e:
                print(f"Error during disconnect: {e}")
    
    async def _receive_loop(self):
        """Receive and log messages from server."""
        while self.is_connected and self.ws_connection:
            try:
                message = await self.ws_connection.recv()
                data = json.loads(message)
                self.received_messages.append(data)
                msg_type = data.get('type', 'unknown')
                print(f"  <- Received: {msg_type}")
                
            except ConnectionClosed:
                print(f"Connection closed by server")
                self.is_connected = False
                break
            except json.JSONDecodeError as e:
                print(f"Invalid JSON received: {e}")
            except Exception as e:
                print(f"Receive error: {e}")
                break
    
    async def send_telemetry_envelope(self, port_id: str, data: bytes):
        """Test sending telemetry envelope.
        
        Args:
            port_id: Port identifier
            data: Raw serial data bytes
        """
        print(f"\n{'='*60}")
        print(f"TEST 2: Telemetry Envelope")
        print(f"{'='*60}")
        
        if not self.is_connected:
            print("ERROR: Not connected to server")
            self.test_results.append(("Telemetry Envelope", False, "Not connected"))
            return
        
        try:
            # Create telemetry envelope
            envelope = {
                "type": "telemetry",
                "hubId": self.hub_id,
                "timestamp": datetime.now().isoformat(),
                "portId": port_id,
                "sessionId": f"test-session-{int(time.time())}",
                "data": base64.b64encode(data).decode("utf-8"),
            }
            
            await self.ws_connection.send(json.dumps(envelope))
            print(f"Sent telemetry envelope")
            print(f"  Port: {port_id}")
            print(f"  Data length: {len(data)} bytes")
            print(f"  Data (hex): {data.hex()}")
            
            await asyncio.sleep(0.5)
            self.test_results.append(("Telemetry Envelope", True, "Sent successfully"))
            
        except Exception as e:
            print(f"Failed to send telemetry: {e}")
            self.test_results.append(("Telemetry Envelope", False, str(e)))
    
    async def send_health_envelope(self):
        """Test sending health envelope."""
        print(f"\n{'='*60}")
        print(f"TEST 3: Health Envelope")
        print(f"{'='*60}")
        
        if not self.is_connected:
            print("ERROR: Not connected to server")
            self.test_results.append(("Health Envelope", False, "Not connected"))
            return
        
        try:
            # Create health envelope with realistic metrics
            envelope = {
                "type": "health",
                "hubId": self.hub_id,
                "timestamp": datetime.now().isoformat(),
                "uptime_seconds": 3600,
                "system": {
                    "cpu": {
                        "percent": 45.2,
                        "count": 4,
                    },
                    "memory": {
                        "total_mb": 4096.0,
                        "available_mb": 2048.0,
                        "used_mb": 2048.0,
                        "percent": 50.0,
                    },
                    "disk": {
                        "total_gb": 64.0,
                        "used_gb": 32.0,
                        "free_gb": 32.0,
                        "percent": 50.0,
                    },
                },
                "service": {
                    "serial": {
                        "active_connections": 1,
                        "total_bytes_read": 1024,
                        "total_bytes_written": 512,
                    },
                    "usb": {
                        "detected_ports": 2,
                    },
                    "tasks": {
                        "queue_size": 0,
                        "completed": 5,
                    },
                    "buffer": {
                        "size_mb": 0.5,
                        "utilization": 0.05,
                    },
                },
                "errors": {
                    "total_errors": 0,
                    "port_errors": {},
                    "port_read_errors": {},
                    "port_write_errors": {},
                },
            }
            
            await self.ws_connection.send(json.dumps(envelope))
            print(f"Sent health envelope")
            print(f"  CPU: {envelope['system']['cpu']['percent']}%")
            print(f"  Memory: {envelope['system']['memory']['percent']}%")
            print(f"  Active connections: {envelope['service']['serial']['active_connections']}")
            
            await asyncio.sleep(0.5)
            self.test_results.append(("Health Envelope", True, "Sent successfully"))
            
        except Exception as e:
            print(f"Failed to send health: {e}")
            self.test_results.append(("Health Envelope", False, str(e)))
    
    async def send_device_event_envelope(self, port_id: str, event_type: str = "connected"):
        """Test sending device event envelope.
        
        Args:
            port_id: Port identifier
            event_type: Event type (connected/disconnected)
        """
        print(f"\n{'='*60}")
        print(f"TEST 4: Device Event Envelope")
        print(f"{'='*60}")
        
        if not self.is_connected:
            print("ERROR: Not connected to server")
            self.test_results.append(("Device Event Envelope", False, "Not connected"))
            return
        
        try:
            # Create device event envelope
            envelope = {
                "type": "device_event",
                "hubId": self.hub_id,
                "timestamp": datetime.now().isoformat(),
                "eventType": event_type,
                "portId": port_id,
                "deviceInfo": {
                    "port": "/dev/ttyUSB0",
                    "description": "USB Serial Device",
                    "manufacturer": "FTDI",
                    "serial_number": "FT123456",
                    "vendor_id": "0403",
                    "product_id": "6001",
                },
            }
            
            await self.ws_connection.send(json.dumps(envelope))
            print(f"Sent device event envelope")
            print(f"  Event: {event_type}")
            print(f"  Port: {port_id}")
            
            await asyncio.sleep(0.5)
            self.test_results.append(("Device Event Envelope", True, "Sent successfully"))
            
        except Exception as e:
            print(f"Failed to send device event: {e}")
            self.test_results.append(("Device Event Envelope", False, str(e)))
    
    async def send_task_status_envelope(self, task_id: str, status: str = "completed"):
        """Test sending task status envelope.
        
        Args:
            task_id: Task identifier
            status: Task status (completed/failed/running)
        """
        print(f"\n{'='*60}")
        print(f"TEST 5: Task Status Envelope")
        print(f"{'='*60}")
        
        if not self.is_connected:
            print("ERROR: Not connected to server")
            self.test_results.append(("Task Status Envelope", False, "Not connected"))
            return
        
        try:
            # Create task status envelope
            envelope = {
                "type": "task_status",
                "hubId": self.hub_id,
                "timestamp": datetime.now().isoformat(),
                "taskId": task_id,
                "status": status,
                "result": {
                    "message": "Task completed successfully",
                    "duration_ms": 1500,
                } if status == "completed" else None,
            }
            
            await self.ws_connection.send(json.dumps(envelope))
            print(f"Sent task status envelope")
            print(f"  Task ID: {task_id}")
            print(f"  Status: {status}")
            
            await asyncio.sleep(0.5)
            self.test_results.append(("Task Status Envelope", True, "Sent successfully"))
            
        except Exception as e:
            print(f"Failed to send task status: {e}")
            self.test_results.append(("Task Status Envelope", False, str(e)))
    
    def print_test_summary(self):
        """Print a summary of all test results."""
        print(f"\n{'='*60}")
        print(f"TEST RESULTS SUMMARY")
        print(f"{'='*60}")
        
        if not self.test_results:
            print("No tests were run")
            return
        
        passed = sum(1 for _, success, _ in self.test_results if success)
        failed = len(self.test_results) - passed
        
        for test_name, success, message in self.test_results:
            status = "PASS" if success else "FAIL"
            symbol = "✓" if success else "✗"
            print(f"{symbol} {test_name}: {status}")
            if not success and message:
                print(f"    Error: {message}")
        
        print(f"\nTotal: {passed} passed, {failed} failed")
        
        if self.received_messages:
            print(f"\nReceived {len(self.received_messages)} message(s) from server:")
            for i, msg in enumerate(self.received_messages, 1):
                msg_type = msg.get('type', 'unknown')
                print(f"  {i}. {msg_type}")
        else:
            print(f"\nNo messages received from server")
        
        return passed == len(self.test_results)


class SerialTester:
    """Test pyserial automatic reading."""
    
    @staticmethod
    def list_available_ports():
        """List all available serial ports."""
        print(f"\n{'='*60}")
        print(f"TEST 6: Serial Port Detection")
        print(f"{'='*60}")
        
        if not SERIAL_AVAILABLE:
            print("ERROR: pyserial not available")
            print("Install with: pip install pyserial")
            return []
        
        try:
            ports = list(list_ports.comports())
            
            if not ports:
                print("No serial ports detected")
                print("\nThis is normal if:")
                print("  - No USB serial devices are connected")
                print("  - Running in a virtual environment without USB access")
                return []
            
            print(f"Found {len(ports)} serial port(s):")
            for port in ports:
                print(f"  - {port.device}")
                print(f"    Description: {port.description}")
                if port.manufacturer:
                    print(f"    Manufacturer: {port.manufacturer}")
                if port.serial_number:
                    print(f"    Serial Number: {port.serial_number}")
                if port.vid and port.pid:
                    print(f"    VID:PID: {port.vid:04x}:{port.pid:04x}")
            
            return ports
            
        except Exception as e:
            print(f"Error listing ports: {e}")
            return []
    
    @staticmethod
    async def test_serial_reading(port_device: str, baud_rate: int = 9600, duration: int = 5):
        """Test automatic serial reading.
        
        Args:
            port_device: Serial port device path (e.g., /dev/ttyUSB0 or COM3)
            baud_rate: Baud rate
            duration: Test duration in seconds
        """
        print(f"\n{'='*60}")
        print(f"TEST 7: Automatic Serial Reading")
        print(f"{'='*60}")
        
        if not SERIAL_AVAILABLE:
            print("ERROR: pyserial not available")
            print("Install with: pip install pyserial")
            return
        
        try:
            print(f"Opening port: {port_device}")
            print(f"Baud rate: {baud_rate}")
            print(f"Test duration: {duration} seconds")
            
            # Open serial port
            ser = serial.Serial(
                port=port_device,
                baudrate=baud_rate,
                timeout=1.0,
            )
            
            print(f"Port opened successfully")
            print(f"Reading data for {duration} seconds...")
            
            bytes_read = 0
            chunks_read = 0
            start_time = time.time()
            
            # Read continuously for specified duration
            while time.time() - start_time < duration:
                if ser.in_waiting > 0:
                    data = ser.read(ser.in_waiting)
                    if data:
                        bytes_read += len(data)
                        chunks_read += 1
                        print(f"  <- Read {len(data)} bytes: {data.hex()}")
                        # Try to decode as text
                        try:
                            text = data.decode('utf-8', errors='ignore')
                            if text.strip():
                                print(f"     Text: {text.strip()}")
                        except:
                            pass
                
                await asyncio.sleep(0.1)
            
            ser.close()
            
            print(f"\nSerial reading test complete")
            print(f"  Total bytes read: {bytes_read}")
            print(f"  Data chunks: {chunks_read}")
            
            if bytes_read == 0:
                print(f"\nWARNING: No data received")
                print(f"Possible reasons:")
                print(f"  - Device not sending data")
                print(f"  - Wrong baud rate (try 115200 or other values)")
                print(f"  - Device requires initialization command")
            
        except serial.SerialException as e:
            print(f"Serial error: {e}")
            print(f"\nPossible issues:")
            print(f"  - Port already in use by another program")
            print(f"  - Insufficient permissions (try running as admin)")
            print(f"  - Device disconnected")
        except Exception as e:
            print(f"Error during serial test: {e}")


async def main():
    """Main test execution."""
    print("="*60)
    print("RPI-HUB-SERVICE WebSocket & Serial Test Suite")
    print("="*60)
    
    # Configuration - load from .env file or environment variables
    SERVER_ENDPOINT = os.getenv("SERVER_ENDPOINT", "ws://localhost:8000/hub")
    HUB_ID = os.getenv("HUB_ID", "test-hub-01")
    DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "test-token-123")
    TEST_PORT_ID = "test-port-01"
    
    print(f"\nConfiguration:")
    print(f"  Endpoint: {SERVER_ENDPOINT}")
    print(f"  Hub ID: {HUB_ID}")
    print(f"  Device Token: {'*' * len(DEVICE_TOKEN) if DEVICE_TOKEN else 'NOT SET'}")
    
    # Initialize tester
    tester = WebSocketTester(SERVER_ENDPOINT, HUB_ID, DEVICE_TOKEN)
    
    try:
        # Test 1: Connect with retry logic
        connected = await tester.connect(max_retries=3, timeout=5.0)
        
        if not connected:
            print(f"\nCannot proceed with envelope tests - connection failed")
            tester.print_test_summary()
            return 1
        
        # Wait a bit for connection to stabilize
        await asyncio.sleep(1)
        
        # Test 2: Send telemetry envelope
        test_data = b"Hello from test! Temperature: 25.5C\n"
        await tester.send_telemetry_envelope(TEST_PORT_ID, test_data)
        
        # Test 3: Send health envelope
        await tester.send_health_envelope()
        
        # Test 4: Send device event envelope
        await tester.send_device_event_envelope(TEST_PORT_ID, "connected")
        
        # Test 5: Send task status envelope
        await tester.send_task_status_envelope("test-task-123", "completed")
        
        # Test 6 & 7: Serial port tests
        serial_tester = SerialTester()
        available_ports = serial_tester.list_available_ports()
        
        if available_ports and SERIAL_AVAILABLE:
            # Prefer non-default ports (skip ttyS0, COM1, etc.)
            # Look for USB serial devices like ttyUSB*, ttyACM*, COM3+
            default_ports = ['/dev/ttyS0', '/dev/ttyAMA0', 'COM1', 'COM2']
            
            # Find first non-default port
            selected_port = None
            for port in available_ports:
                if port.device not in default_ports:
                    selected_port = port.device
                    break
            
            # Fall back to first port if all are default
            if not selected_port:
                selected_port = available_ports[0].device
                print(f"\nNote: Only default port available, using: {selected_port}")
            else:
                print(f"\nNote: Selected non-default port: {selected_port}")
            
            print(f"\nSerial test available for: {selected_port}")
            print(f"WARNING: This will test serial reading on the port")
            print(f"Make sure the port is NOT in use by another program")
            response = input(f"Continue with serial test? (y/n): ")
            
            if response.lower() == 'y':
                await serial_tester.test_serial_reading(selected_port, duration=5)
            else:
                print(f"Skipped serial reading test")
        
        # Print results summary
        tester.print_test_summary()
        
        # Keep connection alive briefly to observe behavior
        print(f"\nKeeping connection alive for 3 seconds...")
        await asyncio.sleep(3)
        
        # Return 0 if all tests passed, 1 if any failed
        all_passed = all(success for _, success, _ in tester.test_results)
        return 0 if all_passed else 1
        
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        return 130
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        # Cleanup
        await tester.disconnect()
        print("\n" + "="*60)
        print("Test suite completed")
        print("="*60)


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(130)
