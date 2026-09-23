#!/usr/bin/env python3
"""
Test WebSocket connectivity to cloud service.
Run this before starting the RPI Hub Service to verify configuration.
"""

import asyncio
import json
import sys
import os
from urllib.parse import urlparse

try:
    import websockets
except ImportError:
    print("❌ Error: websockets library not installed")
    print("   Install it with: pip install websockets")
    sys.exit(1)


def load_env():
    """Load .env file if it exists."""
    env_file = os.path.join(os.path.dirname(__file__), '.env')
    env_vars = {}
    
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip()
    
    return env_vars


async def test_connection(endpoint: str, hub_id: str, token: str):
    """Test WebSocket connection to cloud service."""
    
    print(f"\n{'='*60}")
    print(f"Testing WebSocket Connection")
    print(f"{'='*60}\n")
    
    # Parse endpoint
    parsed = urlparse(endpoint)
    print(f"📡 Endpoint: {endpoint}")
    print(f"   Scheme: {parsed.scheme}")
    print(f"   Host: {parsed.hostname}")
    print(f"   Port: {parsed.port or (443 if parsed.scheme == 'wss' else 80)}")
    print(f"   Path: {parsed.path}")
    print(f"\n🆔 Hub ID: {hub_id}")
    print(f"🔐 Token: {'*' * len(token) if token else '(not set)'}")
    print()
    
    # Test DNS resolution
    if parsed.hostname and parsed.hostname not in ['localhost', '127.0.0.1']:
        print("🔍 Testing DNS resolution...")
        try:
            import socket
            ip = socket.gethostbyname(parsed.hostname)
            print(f"   ✅ Resolved {parsed.hostname} → {ip}")
        except socket.gaierror as e:
            print(f"   ❌ DNS resolution failed: {e}")
            return False
        print()
    
    # Test connection
    print("🔌 Attempting WebSocket connection...")
    try:
        async with websockets.connect(
            endpoint,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            print("   ✅ WebSocket connected!")
            
            # Send handshake
            print("\n📤 Sending handshake...")
            handshake = {
                "type": "hub_connect",
                "hubId": hub_id,
                "deviceToken": token,
                "timestamp": "2026-01-05T00:00:00",
                "version": "1.0.0-test",
            }
            await ws.send(json.dumps(handshake))
            print("   ✅ Handshake sent")
            
            # Wait for response
            print("\n📥 Waiting for server response...")
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(response)
                print(f"   ✅ Received: {json.dumps(data, indent=2)}")
                
                if data.get('type') == 'hub_connected':
                    print(f"\n{'='*60}")
                    print("✅ CONNECTION TEST SUCCESSFUL!")
                    print(f"{'='*60}")
                    print("\nYour RPI Hub Service should be able to connect.")
                    return True
                else:
                    print(f"\n⚠️  Unexpected response type: {data.get('type')}")
                    return False
                    
            except asyncio.TimeoutError:
                print("   ⏱️  Timeout waiting for response (5s)")
                print("   The connection worked but server didn't respond.")
                print("   This might be okay - check server logs.")
                return True
                
    except ConnectionRefusedError:
        print("   ❌ Connection refused (errno 111)")
        print("\n💡 Troubleshooting:")
        print("   1. Check that the cloud service is running")
        print("   2. Verify the SERVER_ENDPOINT in .env is correct")
        print("   3. Ensure no firewall is blocking the port")
        print("   4. If cloud service is on another machine, use its IP not 'localhost'")
        return False
        
    except OSError as e:
        if e.errno == 113:
            print(f"   ❌ No route to host")
            print("\n💡 Network is unreachable. Check your network connection.")
        elif e.errno == 101:
            print(f"   ❌ Network unreachable")
            print("\n💡 Check your network connection and routing.")
        else:
            print(f"   ❌ OS Error: {e} (errno: {e.errno})")
        return False
        
    except asyncio.TimeoutError:
        print("   ❌ Connection timeout")
        print("\n💡 The server is not responding. Check:")
        print("   1. Server is running and accessible")
        print("   2. Firewall allows the connection")
        print("   3. Network route exists between machines")
        return False
        
    except Exception as e:
        print(f"   ❌ Error: {e}")
        print(f"   Error type: {type(e).__name__}")
        return False


async def main():
    """Main test function."""
    
    # Load environment
    env = load_env()
    
    # Get configuration
    endpoint = env.get('SERVER_ENDPOINT', 'ws://localhost:8080/hub')
    hub_id = env.get('HUB_ID', 'rpi-bridge-01')
    token = env.get('DEVICE_TOKEN', '')
    
    # Allow command line override
    if len(sys.argv) > 1:
        endpoint = sys.argv[1]
    if len(sys.argv) > 2:
        hub_id = sys.argv[2]
    if len(sys.argv) > 3:
        token = sys.argv[3]
    
    # Validate configuration
    if not token:
        print("⚠️  Warning: DEVICE_TOKEN not set in .env or command line")
        print("   The handshake will likely fail without a valid token\n")
    
    # Run test
    success = await test_connection(endpoint, hub_id, token)
    
    # Exit code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⏸️  Test interrupted by user")
        sys.exit(1)
