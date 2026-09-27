# RPi Hub Service

Runs on each Raspberry Pi hub. It detects USB serial MCUs (Arduino, STM32), streams their serial
output to the cloud service, and executes commands from the GUI (serial write, restart, flash).

Every Pi runs this same code. What differs per Pi is a **profile** (`HUB_PROFILE`) and its `.env`:

| Profile | Use |
|---|---|
| `lab-hub` | USB MCUs on lab Wi-Fi |
| `cellular-hub` | USB MCUs; Wi-Fi preferred, cellular fallback handled by the uplink manager (`wifi-fallback-relay` repo) |
| `dev-sim` | Simulated boards on a laptop, no hardware |

Setting up a Pi from scratch: `.claude/rpi-hub-setup.md` in the hyperloop-gui repository.

## Architecture

```
src/
  main.py            FastAPI app; starts/stops the runtime
  runtime.py         Composition root: builds and wires every component from settings
  config.py          config/config.yaml + profile overlay + ${ENV:default} substitution
  uplink/            HubAgent (persistent WebSocket to the cloud) and its outbound buffer
  bench/             Bench backend interface, USB backend, serial manager, USB mapper,
                     command handler, tasks, flashing (arduino-cli)
  sim/               Simulated bench backend (same interface as the USB backend)
  health/            Health reporter and the uplink status reader
  api/               Local debugging API
```

- The **backend** (`bench/backend.py`) is the only thing that knows where devices come from.
  `UsbBenchBackend` talks to real hardware; `SimBenchBackend` fakes it. The runtime, command
  handler, tasks and API only use the interface, so a simulated hub exercises exactly the same
  uplink and protocol code as a real one.
- The **uplink** reconnects forever with capped backoff and keeps telemetry in a bounded buffer
  while the cloud is unreachable (oldest telemetry is dropped first; task results and device events
  are kept). After every reconnect the hub re-announces its open devices (`device_snapshot`).
- Commands on the same port run one at a time; different ports run concurrently.

## Running locally without hardware

Start cloud-services (see its README), then:

```bash
cd rpi-hub-server
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
HUB_PROFILE=dev-sim python -m src.main
```

With no `.env`, the dev-sim profile connects to `ws://localhost:8080/hub` as `rpi-bridge-01`
with the cloud's development token. Run a second simulated hub with
`HUB_ID=rpi-bridge-02 DEVICE_TOKEN=dev-token-rpi-bridge-02 API_PORT=8001`.
The simulated boards stream sensor lines, echo serial writes, and complete restart/flash commands.

## Configuration

`config/config.yaml` holds the defaults; `config/profiles/<name>.yaml` is merged over it when
`HUB_PROFILE=<name>` (or a path to a YAML file). `${VAR:default}` values come from the environment
or `.env`. Secrets such as `DEVICE_TOKEN` belong in `.env` only.

Notable settings:

| Key | Default | Meaning |
|---|---|---|
| `hub.mode` | `bench` | `pod` (EtherCAT master) is reserved and rejected until implemented |
| `hub.max_reconnect_attempts` | `0` | `0` = never stop reconnecting |
| `bench.source` | `usb` | `sim` for simulated boards |
| `bench.auto_connect` | `known_boards` | Only open boards in `config/boards.yaml`; `all` opens every USB serial device |
| `api.host` | `127.0.0.1` | The local API can flash MCUs and has no auth; keep it on loopback |
| `uplink.status_file` | `/run/hyperloop-uplink/status.json` | Written by the uplink manager on the cellular hub |

## Supported boards

`config/boards.yaml` maps USB vendor/product ids to boards: FQBN, accepted firmware formats,
flashing tool, default baud and reset method. Shipped entries:

| Board | Firmware | Flashed with | Restart |
|---|---|---|---|
| Arduino Uno R3, Mega 2560, Nano (CH340) | `.ino`, `.hex` | arduino-cli | DTR |
| Arduino Uno R4 Minima / WiFi | `.ino`, `.bin` | arduino-cli | reopen only (Minima) / DTR (WiFi) |
| STM32F407G-DISC1 | `.ino`, `.bin`, `.elf`, `.hex` | OpenOCD over the on-board ST-LINK | OpenOCD reset |
| Other Arduino / CP210x / FTDI bridges | `.ino`, `.hex` | arduino-cli (pick the board type in the GUI) | DTR |

With `bench.auto_connect: known_boards` (the default) only devices in this file are opened, which
keeps the cellular modem's serial ports untouched. To add a board, add an entry with its `lsusb`
ids; entries with a product id take precedence over vendor-wide ones. Boards are reported to the
cloud in `device_event.deviceInfo.board_profile`, and the hub advertises the formats it can flash
as `flash:<format>` capabilities.

Flashing a board re-enumerates it (Uno R4 enters its bootloader on a new USB id); the hub holds
back hotplug handling for that board until the flash finishes, then reopens its session.

## Local API

For debugging on the Pi (`curl http://127.0.0.1:8000/...`). The GUI goes through the cloud.

- `GET /health`, `GET /status` - liveness; full health report plus uplink agent state
- `GET /ports`, `POST /ports/scan`, `GET /ports/{portId}` - detected devices
- `GET /connections`, `POST /connections`, `DELETE /connections/{portId}` - serial sessions
- `POST /tasks/write|flash|restart`, `GET /tasks`, `GET /tasks/{taskId}` - same commands the cloud sends

## WebSocket protocol (hub side)

Handshake (hub -> cloud), first message after connecting:

```json
{
  "type": "hub_connect",
  "hubId": "rpi-bridge-01",
  "deviceToken": "...",
  "timestamp": "2026-09-23T12:00:00Z",
  "version": "1.1.0",
  "capabilities": ["bench", "flash:ino", "flash:hex", "device_snapshot"],
  "profile": {"name": "lab-hub", "mode": "bench", "uplink": null}
}
```

Hub -> cloud: `telemetry`, `health`, `device_event`, `task_status`. Cloud -> hub: `command`
(`serial_write`, `flash`, `restart`, `close_connection`). The authoritative schemas are in
`cloud-services/contracts/openapi.json`.

## Tests

```bash
pytest tests/ -v
```

`tests/manual/` holds hardware diagnostics that are run by hand (not collected by pytest).

## Troubleshooting

| Symptom | Check |
|---|---|
| Hub never shows online | `journalctl -u rpi-hub-server -f`; look for `ws_rejected` (wrong `HUB_ID`/`DEVICE_TOKEN`) or connection errors (`SERVER_ENDPOINT`, network) |
| Board detected but no data | `curl 127.0.0.1:8000/ports` shows it but `/connections` does not: auto-connect policy skipped it, or it failed to open (permissions: user must be in `dialout`) |
| Flash fails | Task error in the GUI carries the arduino-cli output; check the core is installed (`arduino-cli core list`) |

## License

MIT
