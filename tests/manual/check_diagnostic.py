"""
Diagnostic script for rpi-hub-service.

This script helps identify issues with:
1. Serial data not being forwarded
2. Health envelopes not being sent
3. WebSocket connection problems
4. Serial manager callback issues
"""

import asyncio
import json
import time
from datetime import datetime
from typing import Optional

import websockets


class HubServiceDiagnostic:
    """Diagnostic tool for rpi-hub-service."""
    
    def __init__(self, endpoint: str):
        """Initialize diagnostic tool.
        
        Args:
            endpoint: WebSocket endpoint (e.g., ws://localhost:8000/hub)
        """
        self.endpoint = endpoint
        self.ws_connection: Optional[websockets.WebSocketClientProtocol] = None
        self.is_connected = False
        self.messages_received = {
            "telemetry": [],
            "health": [],
            "device_event": [],
            "task_status": [],
            "other": [],
        }
        self.connection_time = None
        
    async def connect(self, hub_id: str, device_token: str) -> bool:
        """Connect to hub service.
        
        Args:
            hub_id: Hub identifier
            device_token: Device token
            
        Returns:
            True if connected
        """
        try:
            print(f"Connecting to {self.endpoint}...")
            
            self.ws_connection = await websockets.connect(
                self.endpoint,
                ping_interval=20,
                ping_timeout=10,
            )
            
            # Send handshake
            handshake = {
                "type": "hub_connect",
                "hubId": hub_id,
                "deviceToken": device_token,
                "timestamp": datetime.now().isoformat(),
                "version": "1.0.0",
            }
            
            await self.ws_connection.send(json.dumps(handshake))
            self.is_connected = True
            self.connection_time = time.time()
            
            print(f"✓ Connected successfully")
            return True
            
        except Exception as e:
            print(f"✗ Connection failed: {e}")
            return False
    
    async def monitor(self, duration: int = 60):
        """Monitor messages from hub service.
        
        Args:
            duration: Monitoring duration in seconds
        """
        if not self.is_connected:
            print("Not connected")
            return
        
        print(f"\nMonitoring for {duration} seconds...")
        print(f"Waiting for messages from hub service...\n")
        
        start_time = time.time()
        last_report = start_time
        
        try:
            while time.time() - start_time < duration:
                try:
                    # Check for messages with timeout
                    message = await asyncio.wait_for(
                        self.ws_connection.recv(),
                        timeout=1.0
                    )
                    
                    # Parse message
                    data = json.loads(message)
                    msg_type = data.get("type", "unknown")
                    
                    # Categorize message
                    if msg_type in self.messages_received:
                        self.messages_received[msg_type].append({
                            "timestamp": time.time(),
                            "data": data,
                        })
                    else:
                        self.messages_received["other"].append({
                            "timestamp": time.time(),
                            "data": data,
                        })
                    
                    # Log message
                    elapsed = time.time() - start_time
                    print(f"[{elapsed:6.1f}s] Received: {msg_type}")
                    
                    # Print details based on type
                    if msg_type == "telemetry":
                        port_id = data.get("portId", "unknown")
                        data_len = len(data.get("data", ""))
                        print(f"         Port: {port_id}, Data length: {data_len}")
                    elif msg_type == "health":
                        cpu = data.get("system", {}).get("cpu", {}).get("percent", "?")
                        mem = data.get("system", {}).get("memory", {}).get("percent", "?")
                        print(f"         CPU: {cpu}%, Memory: {mem}%")
                    elif msg_type == "device_event":
                        event_type = data.get("eventType", "unknown")
                        port_id = data.get("portId", "unknown")
                        print(f"         Event: {event_type}, Port: {port_id}")
                    
                except asyncio.TimeoutError:
                    # No message received in timeout period
                    pass
                
                # Periodic status report
                if time.time() - last_report >= 10:
                    self._print_status(time.time() - start_time)
                    last_report = time.time()
                
        except Exception as e:
            print(f"\n✗ Monitoring error: {e}")
        
        # Final report
        print(f"\n{'='*60}")
        print("MONITORING COMPLETE")
        print(f"{'='*60}")
        self._print_final_report(time.time() - start_time)
    
    def _print_status(self, elapsed: float):
        """Print current status.
        
        Args:
            elapsed: Elapsed time in seconds
        """
        print(f"\n--- Status at {elapsed:.0f}s ---")
        print(f"Telemetry messages: {len(self.messages_received['telemetry'])}")
        print(f"Health messages: {len(self.messages_received['health'])}")
        print(f"Device events: {len(self.messages_received['device_event'])}")
        print(f"Task status: {len(self.messages_received['task_status'])}")
        print(f"Other messages: {len(self.messages_received['other'])}")
        print()
    
    def _print_final_report(self, total_time: float):
        """Print final diagnostic report.
        
        Args:
            total_time: Total monitoring time in seconds
        """
        total_messages = sum(len(msgs) for msgs in self.messages_received.values())
        
        print(f"\nTotal monitoring time: {total_time:.1f} seconds")
        print(f"Total messages received: {total_messages}\n")
        
        # Breakdown by type
        print("Message breakdown:")
        for msg_type, messages in self.messages_received.items():
            count = len(messages)
            if count > 0:
                rate = count / total_time * 60  # messages per minute
                print(f"  {msg_type:15s}: {count:4d} messages ({rate:.1f}/min)")
        
        # Diagnostic checks
        print(f"\n{'='*60}")
        print("DIAGNOSTIC RESULTS")
        print(f"{'='*60}\n")
        
        # Check 1: Telemetry
        telemetry_count = len(self.messages_received['telemetry'])
        if telemetry_count > 0:
            print(f"✓ Telemetry: WORKING ({telemetry_count} messages)")
            # Show sample
            if telemetry_count > 0:
                sample = self.messages_received['telemetry'][0]['data']
                print(f"  Sample: Port {sample.get('portId')}, {len(sample.get('data', ''))} bytes")
        else:
            print(f"✗ Telemetry: NO MESSAGES RECEIVED")
            print(f"  Possible issues:")
            print(f"  - No serial connections active")
            print(f"  - Serial devices not sending data")
            print(f"  - Serial manager data callback not configured")
            print(f"  - Hub agent not forwarding serial data")
        
        # Check 2: Health
        health_count = len(self.messages_received['health'])
        if health_count > 0:
            print(f"\n✓ Health: WORKING ({health_count} messages)")
            rate = health_count / total_time * 60
            print(f"  Rate: {rate:.1f} messages/minute")
            # Show sample
            sample = self.messages_received['health'][0]['data']
            print(f"  Sample: Uptime {sample.get('uptime_seconds')}s")
        else:
            print(f"\n✗ Health: NO MESSAGES RECEIVED")
            print(f"  Possible issues:")
            print(f"  - Health reporter not started")
            print(f"  - Health reporter callback not set")
            print(f"  - Hub agent not sending health data to buffer")
            print(f"  - Report interval too long (default: 30s)")
        
        # Check 3: Device events
        device_event_count = len(self.messages_received['device_event'])
        if device_event_count > 0:
            print(f"\n✓ Device Events: WORKING ({device_event_count} messages)")
        else:
            print(f"\n⚠ Device Events: NO MESSAGES")
            print(f"  This is normal if no devices were connected/disconnected")
        
        # Check 4: Connection stability
        if total_time >= 30 and total_messages == 0:
            print(f"\n✗ CRITICAL: No messages received in {total_time:.0f} seconds")
            print(f"  The hub service may not be properly configured:")
            print(f"  1. Check that hub_agent is started in main.py")
            print(f"  2. Verify hub_agent.connect_to_server() is called")
            print(f"  3. Check buffer_manager is initialized")
            print(f"  4. Verify sender task (_send_loop) is running")
        
        # Recommendations
        print(f"\n{'='*60}")
        print("RECOMMENDATIONS")
        print(f"{'='*60}\n")
        
        if health_count == 0:
            print("1. Check health reporter initialization:")
            print("   - Verify initialize_health_reporter() is called")
            print("   - Check health_callback is set to hub_agent.send_health_status")
            print("   - Confirm await health_reporter.start() is called\n")
        
        if telemetry_count == 0:
            print("2. Check serial data forwarding:")
            print("   - Verify serial_manager.set_data_callback() is called")
            print("   - Check callback forwards to hub_agent.send_telemetry()")
            print("   - Open a serial connection and verify device is sending data\n")
        
        if total_messages == 0:
            print("3. Check hub agent sender loop:")
            print("   - Verify _send_loop task is created and running")
            print("   - Check buffer_manager.pop_message() is working")
            print("   - Verify WebSocket connection stays open\n")
    
    async def close(self):
        """Close connection."""
        if self.ws_connection:
            await self.ws_connection.close()
            self.is_connected = False


async def main():
    """Main diagnostic execution."""
    import os
    import sys
    
    print("="*60)
    print("RPI-HUB-SERVICE DIAGNOSTIC TOOL")
    print("="*60)
    
    # Configuration
    endpoint = os.getenv("SERVER_ENDPOINT", "ws://localhost:8000/hub")
    hub_id = os.getenv("HUB_ID", "diagnostic-hub")
    device_token = os.getenv("DEVICE_TOKEN", "diagnostic-token")
    
    # Allow command line override
    if len(sys.argv) > 1:
        endpoint = sys.argv[1]
    if len(sys.argv) > 2:
        hub_id = sys.argv[2]
    if len(sys.argv) > 3:
        device_token = sys.argv[3]
    
    print(f"\nConfiguration:")
    print(f"  Endpoint: {endpoint}")
    print(f"  Hub ID: {hub_id}")
    print(f"  Token: {'*' * len(device_token)}")
    print()
    
    # Create diagnostic tool
    diagnostic = HubServiceDiagnostic(endpoint)
    
    try:
        # Connect
        connected = await diagnostic.connect(hub_id, device_token)
        
        if not connected:
            print("\nCannot proceed - connection failed")
            print("\nTroubleshooting:")
            print("1. Is the hub service running?")
            print("2. Is the endpoint correct?")
            print("3. Check firewall settings")
            return
        
        # Monitor for messages
        duration = int(os.getenv("MONITOR_DURATION", "60"))
        await diagnostic.monitor(duration=duration)
        
    except KeyboardInterrupt:
        print("\n\n⚠ Interrupted by user")
    except Exception as e:
        print(f"\n✗ Diagnostic error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await diagnostic.close()
        print("\nDiagnostic complete")


if __name__ == "__main__":
    import sys
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)
