#!/usr/bin/env python3
"""Mock RPi Hub Simulator - Simulates an rpi-hub-server for testing."""

import asyncio
import json
import random
import sys
from datetime import datetime
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed


class MockHubSimulator:
    """Simulates an RPi hub connecting to the cloud service."""

    def __init__(
        self,
        hub_id: str = "rpi-bridge-01",
        server_url: str = "ws://localhost:8080/hub",
        device_token: str = "dev-token-rpi-bridge-01",
    ):
        """Initialize mock hub simulator.
        
        Args:
            hub_id: Unique hub identifier
            server_url: WebSocket server URL
            device_token: Device authentication token
        """
        self.hub_id = hub_id
        self.server_url = server_url
        self.device_token = device_token
        self.ws_connection: Optional[websockets.WebSocketClientProtocol] = None
        self.running = False
        self.mock_port_id = "COM3"
        self.mock_session_id = "mock-session-001"

    async def connect(self) -> bool:
        """Connect to the WebSocket server and perform handshake."""
        try:
            print(f"[INFO] Connecting to {self.server_url}...")
            self.ws_connection = await websockets.connect(
                self.server_url,
                ping_interval=20,
                ping_timeout=10,
            )
            
            # Send handshake
            handshake = {
                "type": "hub_connect",
                "hubId": self.hub_id,
                "deviceToken": self.device_token,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "version": "1.0.0"
            }
            
            print(f"[INFO] Sending handshake: {json.dumps(handshake, indent=2)}")
            await self.ws_connection.send(json.dumps(handshake))
            
            print(f"[SUCCESS] Connected to server as hub: {self.hub_id}")
            return True
            
        except Exception as e:
            print(f"[ERROR] Failed to connect: {e}")
            return False

    async def send_telemetry(self, data: str) -> None:
        """Send telemetry (serial data) message.
        
        Args:
            data: Data to send (will be base64 encoded if needed)
        """
        if not self.ws_connection:
            print("[ERROR] Not connected to server")
            return
        
        # Simple base64 encoding for the data
        import base64
        encoded_data = base64.b64encode(data.encode()).decode()
        
        message = {
            "type": "telemetry",
            "hubId": self.hub_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "portId": self.mock_port_id,
            "sessionId": self.mock_session_id,
            "data": encoded_data
        }
        
        await self.ws_connection.send(json.dumps(message))
        print(f"[TELEMETRY] Sent: {data}")

    async def send_device_event(self, event_type: str, port_id: str) -> None:
        """Send device event (connected/disconnected).
        
        Args:
            event_type: Event type (connected, disconnected)
            port_id: Port identifier
        """
        if not self.ws_connection:
            print("[ERROR] Not connected to server")
            return
        
        message = {
            "type": "device_event",
            "hubId": self.hub_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "eventType": event_type,
            "portId": port_id,
            "deviceInfo": {
                "vid": "2341",
                "pid": "0043",
                "manufacturer": "Arduino",
                "product": "Arduino Uno"
            } if event_type == "connected" else None
        }
        
        await self.ws_connection.send(json.dumps(message))
        print(f"[EVENT] Device {event_type} on {port_id}")

    async def send_health(self) -> None:
        """Send health status message."""
        if not self.ws_connection:
            print("[ERROR] Not connected to server")
            return
        
        message = {
            "type": "health",
            "hubId": self.hub_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "uptime_seconds": random.randint(100, 10000),
            "system": {
                "cpu_percent": random.uniform(10, 50),
                "memory_percent": random.uniform(20, 60),
                "disk_percent": random.uniform(30, 70),
                "temperature": random.uniform(40, 65)
            },
            "service": {
                "active_connections": 1,
                "total_commands_received": random.randint(0, 100),
                "total_commands_completed": random.randint(0, 95)
            },
            "errors": {
                "serial_errors": 0,
                "command_errors": 0,
                "connection_errors": 0
            }
        }
        
        await self.ws_connection.send(json.dumps(message))
        print(f"[HEALTH] Sent health update")

    async def listen_for_commands(self) -> None:
        """Listen for incoming commands from server."""
        try:
            while self.running and self.ws_connection:
                try:
                    message_text = await asyncio.wait_for(
                        self.ws_connection.recv(),
                        timeout=1.0
                    )
                    message = json.loads(message_text)
                    
                    print(f"[COMMAND] Received: {json.dumps(message, indent=2)}")
                    
                    # Handle different command types
                    if message.get("type") == "command":
                        command = message.get("command", {})
                        command_id = command.get("commandId")
                        command_type = command.get("commandType")
                        
                        print(f"[COMMAND] Processing {command_type} (ID: {command_id})")
                        
                        # Simulate command processing
                        await asyncio.sleep(0.5)
                        
                        # Send task status response
                        status_message = {
                            "type": "task_status",
                            "hubId": self.hub_id,
                            "timestamp": datetime.utcnow().isoformat() + "Z",
                            "taskId": command_id,
                            "status": "completed",
                            "progress": 100,
                            "result": {"success": True},
                            "error": None
                        }
                        
                        await self.ws_connection.send(json.dumps(status_message))
                        print(f"[TASK] Completed: {command_id}")
                        
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed:
                    print("[WARNING] Connection closed by server")
                    break
                    
        except Exception as e:
            print(f"[ERROR] Error in command listener: {e}")

    async def send_mock_sensor_data(self, interval: float = 2.0) -> None:
        """Send mock sensor data periodically.
        
        Args:
            interval: Interval between messages in seconds
        """
        
        while self.running:
            try:
                # Generate all sensor readings
                temperature = round(random.uniform(20.0, 30.0), 2)
                humidity = round(random.uniform(40.0, 70.0), 2)
                pressure = round(random.uniform(980.0, 1020.0), 2)
                
                # Format as BME280 CSV format (temp,humidity,pressure)
                # This matches the BME280 pattern in sensor-mappings.json
                data = f"{temperature},{humidity},{pressure}\n"
                await self.send_telemetry(data)
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                print(f"[ERROR] Error sending sensor data: {e}")
                break

    async def run(self, duration: Optional[int] = None) -> None:
        """Run the mock hub simulator.
        
        Args:
            duration: Duration to run in seconds (None for indefinite)
        """
        # Connect to server
        if not await self.connect():
            return
        
        self.running = True
        
        try:
            # Send initial device connection event
            await self.send_device_event("connected", self.mock_port_id)
            await asyncio.sleep(0.5)
            
            # Send initial health update
            await self.send_health()
            await asyncio.sleep(0.5)
            
            # Start background tasks
            command_listener = asyncio.create_task(self.listen_for_commands())
            sensor_data_sender = asyncio.create_task(self.send_mock_sensor_data(interval=2.0))
            
            # Periodic health updates
            async def send_health_periodic():
                while self.running:
                    await asyncio.sleep(30)
                    await self.send_health()
            
            health_sender = asyncio.create_task(send_health_periodic())
            
            # Wait for duration or until interrupted
            if duration:
                print(f"[INFO] Running for {duration} seconds...")
                await asyncio.sleep(duration)
                self.running = False
            else:
                print("[INFO] Running indefinitely (press Ctrl+C to stop)...")
                # Wait for tasks
                await asyncio.gather(
                    command_listener,
                    sensor_data_sender,
                    health_sender,
                    return_exceptions=True
                )
            
        except KeyboardInterrupt:
            print("\n[INFO] Interrupted by user")
        except Exception as e:
            print(f"[ERROR] Error during execution: {e}")
        finally:
            self.running = False
            
            # Send disconnect event
            try:
                await self.send_device_event("disconnected", self.mock_port_id)
            except:
                pass
            
            # Close connection
            if self.ws_connection:
                await self.ws_connection.close()
                print("[INFO] Disconnected from server")


async def main():
    """Main entry point."""
    # Parse command line arguments
    import argparse
    import inspect
    
    # Get default values from MockHubSimulator.__init__
    sig = inspect.signature(MockHubSimulator.__init__)
    default_hub_id = sig.parameters['hub_id'].default
    default_server_url = sig.parameters['server_url'].default
    default_device_token = sig.parameters['device_token'].default
    
    parser = argparse.ArgumentParser(description="Mock RPi Hub Simulator")
    parser.add_argument(
        "--hub-id",
        default=default_hub_id,
        help=f"Hub identifier (default: {default_hub_id})"
    )
    parser.add_argument(
        "--server",
        default=default_server_url,
        help=f"WebSocket server URL (default: {default_server_url})"
    )
    parser.add_argument(
        "--token",
        default=default_device_token,
        help=f"Device authentication token (default: {default_device_token})"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Duration to run in seconds (default: indefinite)"
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("Mock RPi Hub Simulator")
    print("=" * 60)
    print(f"Hub ID: {args.hub_id}")
    print(f"Server: {args.server}")
    print(f"Token: {args.token}")
    print("=" * 60)
    print()
    
    # Create and run simulator
    simulator = MockHubSimulator(
        hub_id=args.hub_id,
        server_url=args.server,
        device_token=args.token
    )
    
    try:
        await simulator.run(duration=args.duration)
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down...")


# comment 
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Shutdown complete")
        sys.exit(0)
