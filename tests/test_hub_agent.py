"""HubAgent against a local WebSocket server: handshake, buffering, reconnects."""

import asyncio

import pytest

from tests.conftest import FakeCloud
from src.uplink import hub_agent as hub_agent_module
from src.uplink.buffer_manager import BufferManager
from src.uplink.hub_agent import HubAgent


@pytest.fixture(autouse=True)
def fast_reconnect(monkeypatch):
    monkeypatch.setattr(hub_agent_module, "MIN_RECONNECT_DELAY_SECONDS", 0.05)
    monkeypatch.setattr(hub_agent_module.random, "uniform", lambda a, b: 1.0)


def make_agent(url, **kwargs):
    defaults = dict(
        hub_id="rpi-bridge-01",
        server_endpoint=url,
        device_token="token-1",
        buffer_manager=BufferManager(size_mb=1),
        reconnect_interval=0.05,
    )
    defaults.update(kwargs)
    return HubAgent(**defaults)


@pytest.mark.asyncio
async def test_handshake_includes_capabilities_and_profile(fake_cloud):
    agent = make_agent(
        fake_cloud.url,
        capabilities=["bench", "flash:ino", "device_snapshot"],
        profile=lambda: {"name": "lab-hub", "mode": "bench", "uplink": "wifi"},
    )
    await agent.start()
    try:
        await asyncio.wait_for(fake_cloud.connected.wait(), 5)
        handshake = fake_cloud.handshakes[0]
        assert handshake["type"] == "hub_connect"
        assert handshake["hubId"] == "rpi-bridge-01"
        assert handshake["deviceToken"] == "token-1"
        assert handshake["capabilities"] == ["bench", "flash:ino", "device_snapshot"]
        assert handshake["profile"] == {"name": "lab-hub", "mode": "bench", "uplink": "wifi"}
        assert handshake["timestamp"].endswith("Z")
        await fake_cloud.wait_for(lambda c: agent.is_connected)
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_messages_sent_with_envelope(fake_cloud):
    agent = make_agent(fake_cloud.url)
    await agent.start()
    try:
        await asyncio.wait_for(fake_cloud.connected.wait(), 5)
        agent.queue_telemetry("port-1", "s1", b"TEMP: 21\n")
        agent.send_task_status_update({"task_id": "t1", "status": "completed", "command_type": "restart", "port_id": "port-1"})
        await fake_cloud.wait_for(lambda c: c.of_type("task_status"))

        telemetry = fake_cloud.of_type("telemetry")[0]
        assert telemetry["hubId"] == "rpi-bridge-01"
        assert telemetry["portId"] == "port-1"
        assert telemetry["data"] == "VEVNUDogMjEK"
        assert telemetry["timestamp"].endswith("Z")

        task = fake_cloud.of_type("task_status")[0]
        assert task == {**task, "taskId": "t1", "status": "completed", "commandType": "restart", "portId": "port-1"}
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_buffers_while_disconnected_and_drains_after_connect():
    cloud = FakeCloud()
    await cloud.start()
    port = cloud.port
    await cloud.stop()  # nothing listening yet

    agent = make_agent(f"ws://127.0.0.1:{port}/hub")
    await agent.start()
    try:
        agent.queue_telemetry("port-1", "s1", b"early\n")
        for _ in range(200):  # refused connects can take ~2s on Windows
            if agent.reconnect_attempts >= 1:
                break
            await asyncio.sleep(0.05)
        assert not agent.is_connected
        assert agent.reconnect_attempts >= 1

        cloud = FakeCloud()
        cloud.server = await __import__("websockets").serve(cloud._handler, "127.0.0.1", port)
        cloud.port = port
        await cloud.wait_for(lambda c: c.of_type("telemetry"), timeout=10)
        assert cloud.of_type("telemetry")[0]["data"] == "ZWFybHkK"
    finally:
        await agent.stop()
        await cloud.stop()


@pytest.mark.asyncio
async def test_reconnects_after_server_drop(fake_cloud):
    connects = []

    async def on_connect():
        connects.append(True)

    agent = make_agent(fake_cloud.url)
    agent.add_connected_callback(on_connect)
    await agent.start()
    try:
        await fake_cloud.wait_for(lambda c: len(c.handshakes) == 1)
        await fake_cloud.drop_connections()
        await fake_cloud.wait_for(lambda c: len(c.handshakes) == 2, timeout=10)
        await fake_cloud.wait_for(lambda c: agent.is_connected)
        assert len(connects) == 2
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_rejected_hub_backs_off():
    cloud = FakeCloud(reject_with=1008)
    await cloud.start()
    agent = make_agent(cloud.url)
    await agent.start()
    try:
        await cloud.wait_for(lambda c: len(c.handshakes) >= 2, timeout=10)
        assert "Rejected by server" in (agent.last_error or "")
        # Short-lived connections count as failures, so the delay grows
        assert agent.reconnect_attempts >= 1
    finally:
        await agent.stop()
        await cloud.stop()


@pytest.mark.asyncio
async def test_max_reconnect_attempts_stops(monkeypatch):
    cloud = FakeCloud()
    await cloud.start()
    port = cloud.port
    await cloud.stop()

    agent = make_agent(f"ws://127.0.0.1:{port}/hub", max_reconnect_attempts=2)
    await agent.start()
    try:
        for _ in range(100):
            if not agent._running:
                break
            await asyncio.sleep(0.05)
        assert agent._running is False
        assert agent.reconnect_attempts == 2
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_commands_routed_to_callback(fake_cloud):
    received = asyncio.Queue()
    agent = make_agent(fake_cloud.url)
    agent.set_command_callback(received.put)
    await agent.start()
    try:
        await asyncio.wait_for(fake_cloud.connected.wait(), 5)
        await fake_cloud.send_command({"commandId": "c1", "commandType": "restart", "portId": "p", "params": {}})
        envelope = await asyncio.wait_for(received.get(), 5)
        assert envelope["command"]["commandId"] == "c1"
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_telemetry_dropped_while_disconnected_when_disabled():
    agent = make_agent("ws://127.0.0.1:9/hub", buffer_telemetry_while_disconnected=False)
    agent.queue_telemetry("p", "s", b"x")
    assert agent.buffer_manager.get_message_count() == 0
    agent.send_task_status_update({"task_id": "t", "status": "failed"})
    assert agent.buffer_manager.get_message_count() == 1
