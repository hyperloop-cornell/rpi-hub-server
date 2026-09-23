"""CommandHandler + tasks against the simulated backend."""

import asyncio
import base64

import pytest
import pytest_asyncio

from src.bench.command_handler import MAX_FINISHED_TASKS, CommandHandler
from src.sim import SimBenchBackend, SimDeviceSpec

HEX = ":100000000C9434000C943E000C943E000C943E0082\n:00000001FF\n"
INO = "void setup() { Serial.begin(9600); }\nvoid loop() {}\n"


class Recorder:
    """Collects backend events and task statuses."""

    def __init__(self):
        self.statuses = []
        self.events = []
        self.data = []

    def status(self, update):
        self.statuses.append(update)

    def final(self, task_id):
        for update in reversed(self.statuses):
            if update["task_id"] == task_id and update["status"] in ("completed", "failed", "cancelled"):
                return update
        return None

    async def on_device_added(self, device):
        self.events.append(("added", device.port_id))

    async def on_device_removed(self, device):
        self.events.append(("removed", device.port_id))

    async def on_connection_opened(self, connection, device):
        self.events.append(("opened", device.port_id, connection.session_id))

    async def on_connection_lost(self, port_id, session_id, reason):
        self.events.append(("lost", port_id))

    def on_data(self, port_id, session_id, data):
        self.data.append((port_id, data))


@pytest_asyncio.fixture
async def bench():
    recorder = Recorder()
    backend = SimBenchBackend(
        hub_id="test-hub",
        specs=[SimDeviceSpec(name="uno", interval_ms=20, flash_seconds=0.2), SimDeviceSpec(name="mega", interval_ms=20)],
        speedup=50,
    )
    backend.set_listener(recorder)
    await backend.start()
    for device in backend.devices():
        await backend.open(device.port_id)
    handler = CommandHandler(backend=backend, task_status_callback=recorder.status, max_concurrent_tasks=3)
    await handler.start()
    yield backend, handler, recorder
    await handler.stop()
    await backend.stop()


async def wait_final(recorder, task_id, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        final = recorder.final(task_id)
        if final:
            return final
        await asyncio.sleep(0.01)
    raise AssertionError(f"no final status for {task_id}: {recorder.statuses}")


def command(command_id, command_type, port_id, **params):
    return {"commandId": command_id, "commandType": command_type, "portId": port_id, "params": params, "priority": 5}


@pytest.mark.asyncio
async def test_serial_write_reports_bytes(bench):
    backend, handler, recorder = bench
    port = backend.devices()[0].port_id
    await handler.handle_command(command("w1", "serial_write", port, data="hello\n"))
    final = await wait_final(recorder, "w1")
    assert final["status"] == "completed"
    assert final["result"]["bytes_written"] == 6
    assert final["command_type"] == "serial_write"
    assert final["port_id"] == port
    assert final["timestamp"].endswith("Z")
    # The sim echoes writes back as telemetry
    assert any(b"ECHO: hello" in data for _, data in recorder.data)


@pytest.mark.asyncio
async def test_base64_write(bench):
    backend, handler, recorder = bench
    port = backend.devices()[0].port_id
    await handler.handle_command(command("w2", "serial_write", port, data=base64.b64encode(b"\x01\x02").decode(), encoding="base64"))
    assert (await wait_final(recorder, "w2"))["result"]["bytes_written"] == 2


@pytest.mark.asyncio
async def test_restart_opens_new_session(bench):
    backend, handler, recorder = bench
    port = backend.devices()[0].port_id
    before = backend.get_connection(port).session_id
    await handler.handle_command(command("r1", "restart", port))
    final = await wait_final(recorder, "r1")
    assert final["status"] == "completed"
    assert final["result"]["session_id"] != before
    assert backend.get_connection(port).session_id == final["result"]["session_id"]


@pytest.mark.asyncio
async def test_close_returns_result(bench):
    backend, handler, recorder = bench
    port = backend.devices()[0].port_id
    await handler.handle_command(command("c1", "close_connection", port))
    final = await wait_final(recorder, "c1")
    assert final["status"] == "completed"
    assert final["result"] == {"port_id": port, "status": "closed"}
    assert backend.get_connection(port) is None

    await handler.handle_command(command("c2", "close_connection", port))
    assert (await wait_final(recorder, "c2"))["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "firmware,params,expected_status,expected_format",
    [
        (HEX, {}, "completed", "hex"),
        (INO, {"boardFqbn": "arduino:avr:uno"}, "completed", "ino"),
        (INO, {}, "failed", None),  # .ino needs an FQBN
        ("\x00garbage", {}, "failed", None),
    ],
)
async def test_flash(bench, firmware, params, expected_status, expected_format):
    backend, handler, recorder = bench
    port = backend.devices()[0].port_id
    data = base64.b64encode(firmware.encode("latin-1")).decode()
    await handler.handle_command(command("f1", "flash", port, firmwareData=data, **params))
    final = await wait_final(recorder, "f1")
    assert final["status"] == expected_status, final
    if expected_format:
        assert final["result"]["artifact_format"] == expected_format
        assert backend.get_connection(port) is not None  # session reopened after flashing


@pytest.mark.asyncio
async def test_flash_binary_requires_declared_format(bench):
    backend, handler, recorder = bench
    port = backend.devices()[0].port_id
    data = base64.b64encode(b"\x00\x01\x02\xff").decode()
    await handler.handle_command(command("b1", "flash", port, firmwareData=data))
    assert "artifactFormat" in (await wait_final(recorder, "b1"))["error"]
    await handler.handle_command(command("b2", "flash", port, firmwareData=data, artifactFormat="bin"))
    assert (await wait_final(recorder, "b2"))["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"commandType": "restart", "portId": "p"},
        {"commandId": "x1", "portId": "p"},
        {"commandId": "x2", "commandType": "restart"},
        {"commandId": "x3", "commandType": "teleport", "portId": "p"},
        {"commandId": "x4", "commandType": "serial_write", "portId": "p", "params": {}},
        {"commandId": "x5", "commandType": "flash", "portId": "p", "params": {"firmwareData": "***"}},
    ],
)
async def test_invalid_commands_reported_failed(bench, bad):
    _, handler, recorder = bench
    with pytest.raises(ValueError):
        await handler.handle_command(bad)
    if bad.get("commandId"):
        final = recorder.final(bad["commandId"])
        assert final["status"] == "failed"
        assert final["error"]


@pytest.mark.asyncio
async def test_unknown_port_fails_task(bench):
    _, handler, recorder = bench
    await handler.handle_command(command("u1", "restart", "port_missing"))
    final = await wait_final(recorder, "u1")
    assert final["status"] == "failed"
    assert "Port not found" in final["error"]


@pytest.mark.asyncio
async def test_same_port_tasks_run_in_order(bench):
    """A write queued behind a flash on the same port waits for the flash to finish."""
    backend, handler, recorder = bench
    port = backend.devices()[0].port_id
    await handler.handle_command(command("f1", "flash", port, firmwareData=base64.b64encode(HEX.encode()).decode()))
    await handler.handle_command(command("w1", "serial_write", port, data="after\n"))
    flash = await wait_final(recorder, "f1")
    write = await wait_final(recorder, "w1")
    assert flash["status"] == "completed"
    assert write["status"] == "completed"
    order = [s["task_id"] for s in recorder.statuses if s["status"] == "completed"]
    assert order.index("f1") < order.index("w1")


@pytest.mark.asyncio
async def test_finished_tasks_are_pruned(bench):
    backend, handler, recorder = bench
    port = backend.devices()[1].port_id
    for i in range(MAX_FINISHED_TASKS + 20):
        await handler.handle_command(command(f"t{i}", "serial_write", port, data="x"))
        if i % 50 == 0:
            await wait_final(recorder, f"t{i}")
    await wait_final(recorder, f"t{MAX_FINISHED_TASKS + 19}")
    await handler.handle_command(command("last", "serial_write", port, data="x"))
    assert len(handler.get_all_tasks()) <= MAX_FINISHED_TASKS + 2


@pytest.mark.asyncio
async def test_task_dict_omits_firmware(bench):
    backend, handler, recorder = bench
    port = backend.devices()[0].port_id
    await handler.handle_command(command("f9", "flash", port, firmwareData=base64.b64encode(HEX.encode()).decode()))
    await wait_final(recorder, "f9")
    task = handler.get_task("f9").to_dict()
    assert "params" not in task
    assert "firmwareData" not in str(task)
