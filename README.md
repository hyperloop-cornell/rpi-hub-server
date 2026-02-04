# RPi Hub Service

FastAPI server for Raspberry Pi that bridges Arduino serial data to laptop over WiFi for Cornell Hyperloop electrical systems.

## Features

- Automatic USB device detection with arduino-cli
- WebSocket communication with server
- Multiple simultaneous serial connections
- Comprehensive JSON structured logging
- 10MB circular buffer for backpressure handling
- Health monitoring with system metrics
- Flash firmware to connected devices (supports .ino compilation)
- Hotplug device detection

## Prerequisites

- Python 3.11+
- arduino-cli (for board detection and sketch compilation)

### Installing arduino-cli

```bash
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
```

Add to PATH:
```bash
export PATH=$PATH:$HOME/bin
```

Install required Arduino cores for compilation:
```bash
arduino-cli core update-index
arduino-cli core install arduino:avr  # For Uno, Mega, Nano
arduino-cli core install esp32:esp32  # For ESP32 (requires additional board manager URL)
```

For ESP32 support, add the board manager URL first:
```bash
arduino-cli config init
arduino-cli config add board_manager.additional_urls https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32
```

## Installation

```bash
git clone <repository-url>
cd rpi-hub-service
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

1. Copy environment template:
```bash
cp .env.example .env
```

2. Edit `.env` with your settings:
```env
HUB_ID=rpi-bridge-01
SERVER_ENDPOINT=ws://YOUR_CLOUD_SERVER_IP:8080/hub
DEVICE_TOKEN=dev-token-rpi-bridge-01
```

**Important:** Replace `YOUR_CLOUD_SERVER_IP` with:
- Your cloud service machine's IP address (e.g., `192.168.1.100`)
- Or `localhost` if running both services on the same machine

3. Adjust `config/config.yaml` as needed.

## Running Locally

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

## Deployment to rpi-bridge-01

```bash
ssh pi@rpi-bridge-01
cd /opt/rpi-hub-service
git pull
source venv/bin/activate
pip install -r requirements.txt
# Restart service (systemd or supervisor)
```

## API Endpoints

### Health
- `GET /health` - Basic health check
- `GET /status` - Detailed status with metrics

### Ports
- `GET /ports` - List all detected serial ports
- `POST /ports/scan` - Trigger manual port scan
- `GET /ports/{portId}` - Get specific port details

### Connections
- `POST /connections` - Open serial connection
  ```json
  {
    "portId": "port-abc123",
    "baudRate": 9600
  }
  ```
- `DELETE /connections/{portId}` - Close connection
- `GET /connections` - List active connections

### Tasks
- `POST /tasks/write` - Write data to serial port
  ```json
  {
    "portId": "port-abc123",
    "data": "Hello Arduino",
    "encoding": "utf-8"
  }
  ```
- `POST /tasks/flash` - Flash firmware to device (accepts .ino source or .hex binary)
  ```json
  {
    "portId": "port-abc123",
    "firmwareData": "base64-encoded-ino-or-hex-file",
    "boardFqbn": "arduino:avr:uno"
  }
  ```
  Note: boardFqbn is required for .ino source, optional for .hex (auto-detected)
- `POST /tasks/restart` - Restart connected device
  ```json
  {
    "portId": "port-abc123"
  }
  ```
- `GET /tasks/{taskId}` - Get task status
- `GET /tasks` - List all tasks

## WebSocket Protocol

Connect to `/ws/hub` with device token authentication.

### Command Messages (Server → Hub)
```json
{
  "type": "command",
  "command": {
    "commandId": "cmd-123",
    "commandType": "serial_write|flash|restart",
    "portId": "port-abc123",
    "params": {
      "data": "...",
      "encoding": "utf-8"
    },
    "priority": 5
  }
}
```

### Task Status Updates (Hub → Server)
```json
{
  "type": "task_status",
  "hubId": "rpi-bridge-01",
  "timestamp": "2024-01-03T12:00:00Z",
  "taskId": "cmd-123",
  "status": "completed|failed|running",
  "result": {...},
  "error": "error message if failed"
}
```

### Telemetry Data (Hub → Server)
```json
{
  "type": "telemetry",
  "hubId": "rpi-bridge-01",
  "timestamp": "2025-01-03T12:00:00Z",
  "portId": "port-abc123",
  "sessionId": "session-xyz",
  "data": "base64-encoded-serial-data"
}
```

### Message Types (Hub → Server)
- `hub_connect` - Initial handshake
- `telemetry` - Serial data from devices
- `health` - System health metrics
- `device_event` - Hotplug events
- `task_status` - Task completion updates

### Message Types (Server → Hub)
- `command` - Execute task (flash, write, restart, etc.)

## JSON Log Format

All logs are JSON structured to stdout:

```json
{
  "timestamp": "2026-01-03T10:00:00Z",
  "level": "INFO",
  "module": "serial_manager",
  "event": "serial_read",
  "port_id": "port_0",
  "bytes": 64
}
```

## Testing

```bash
pytest tests/ -v
pytest tests/ --cov=src --cov-report=html
```

## Troubleshooting

### Arduino not detected
- Verify arduino-cli installation: `arduino-cli version`
- Check USB permissions: `ls -l /dev/ttyUSB*`
- Run port scan: `curl http://localhost:8000/ports/scan`

### Connection fails
- Check baud rate compatibility
- Verify device not in use by another process
- Review logs for retry attempts

### WebSocket disconnects
- Check network connectivity
- Verify SERVER_ENDPOINT and DEVICE_TOKEN
- Monitor reconnection attempts in logs

## License

MIT
