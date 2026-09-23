"""HubRuntime end to end: simulated devices -> runtime -> uplink -> fake cloud, and back."""

import asyncio
import base64
import json

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from src.config import Settings
from src.runtime import HubRuntime
from src.sim import SimBenchBackend, SimDeviceSpec
from src.uplink import hub_agent as hub_agent_module


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(hub_agent_module, "MIN_RECONNECT_DELAY_SECONDS", 0.05)
    monkeypatch.setattr("src.runtime.LOST_CONNECTION_RETRY_SECONDS", 0.05)


def settings_for(cloud_url, tmp_path, **bench):
    return Settings(
        hub={
            "hub_id": "rpi-bridge-01",
            "server_endpoint": cloud_url,
            "device_token": "token",
            "reconnect_interval": 0.05,
            "profile_name": "dev-sim",
        },
        bench={"source": "sim", "sim_speedup": 50, **bench},
        health={"report_interval": 3600},
        uplink={"status_file": str(tmp_path / "uplink.json")},
    )


@pytest_asyncio.fixture
async def runtime(fake_cloud, tmp_path):
    backend = SimBenchBackend(
        hub_id="rpi-bridge-01",
        specs=[SimDeviceSpec(name="uno", interval_ms=20, flash_seconds=0.1), SimDeviceSpec(name="mega", interval_ms=20)],
        speedup=50,
    )
    rt = HubRuntime(settings_for(fake_cloud.url, tmp_path), backend=backend)
    await rt.start()
    yield rt
    await rt.stop()


@pytest.mark.asyncio
async def test_handshake_advertises_capabilities(fake_cloud, runtime):
    await fake_cloud.wait_for(lambda c: c.handshakes)
    handshake = fake_cloud.handshakes[0]
    assert "device_snapshot" in handshake["capabilities"]
    assert {"bench", "flash:ino", "flash:hex", "flash:bin"} <= set(handshake["capabilities"])
    assert handshake["profile"] == {"name": "dev-sim", "mode": "bench", "uplink": None}


@pytest.mark.asyncio
async def test_devices_announced_and_telemetry_flows(fake_cloud, runtime):
    events = await fake_cloud.wait_for(lambda c: len({e["portId"] for e in c.of_type("device_event")}) == 2 and c.of_type("device_event"))
    info = events[0]["deviceInfo"]
    assert info["session_id"].startswith("session-")
    assert info["vendor_id"] == "2341"

    telemetry = await fake_cloud.wait_for(lambda c: c.of_type("telemetry"))
    assert b"TEMP:" in base64.b64decode(telemetry[0]["data"])


@pytest.mark.asyncio
async def test_snapshot_resent_after_reconnect(fake_cloud, runtime):
    await fake_cloud.wait_for(lambda c: len(c.of_type("device_event")) >= 2)
    fake_cloud.messages.clear()
    await fake_cloud.drop_connections()
    await fake_cloud.wait_for(lambda c: len(c.handshakes) >= 2, timeout=10)
    events = await fake_cloud.wait_for(lambda c: len({e["portId"] for e in c.of_type("device_event")}) == 2 and c.of_type("device_event"))
    assert all(e["eventType"] == "connected" for e in events)


@pytest.mark.asyncio
async def test_command_round_trip(fake_cloud, runtime):
    await asyncio.wait_for(fake_cloud.connected.wait(), 5)
    port = runtime.backend.devices()[0].port_id
    await fake_cloud.send_command({"commandId": "cmd-1", "commandType": "restart", "portId": port, "params": {}})
    final = await fake_cloud.wait_for(
        lambda c: [m for m in c.of_type("task_status") if m["taskId"] == "cmd-1" and m["status"] == "completed"]
    )
    assert final[0]["commandType"] == "restart"
    assert final[0]["portId"] == port
    # A restart opens a new session, announced to the cloud
    await fake_cloud.wait_for(lambda c: [e for e in c.of_type("device_event") if e["deviceInfo"]["session_id"] == final[0]["result"]["session_id"]])


@pytest.mark.asyncio
async def test_invalid_command_reported(fake_cloud, runtime):
    await asyncio.wait_for(fake_cloud.connected.wait(), 5)
    await fake_cloud.send_command({"commandId": "bad-1", "commandType": "teleport", "portId": "p", "params": {}})
    failed = await fake_cloud.wait_for(lambda c: [m for m in c.of_type("task_status") if m["taskId"] == "bad-1"])
    assert failed[0]["status"] == "failed"
    assert "Unsupported command type" in failed[0]["error"]


@pytest.mark.asyncio
async def test_unplug_and_replug(fake_cloud, runtime):
    await fake_cloud.wait_for(lambda c: len(c.of_type("device_event")) >= 2)
    port = runtime.backend.devices()[0].port_id
    spec = runtime.backend._specs_by_port[port]
    await runtime.backend.unplug(port)
    await fake_cloud.wait_for(lambda c: [e for e in c.of_type("device_event") if e["eventType"] == "disconnected" and e["portId"] == port])
    await runtime.backend.plug(spec, 0)
    await fake_cloud.wait_for(
        lambda c: [e for e in c.of_type("device_event") if e["eventType"] == "connected" and e["portId"] == port][1:]
    )


@pytest.mark.asyncio
async def test_lost_connection_reopened(fake_cloud, runtime):
    await fake_cloud.wait_for(lambda c: len(c.of_type("device_event")) >= 2)
    port = runtime.backend.devices()[0].port_id
    old = runtime.backend.get_connection(port).session_id
    await runtime.backend._close(port)
    await runtime.on_connection_lost(port, old, "serial_error")
    for _ in range(100):
        conn = runtime.backend.get_connection(port)
        if conn and conn.session_id != old:
            break
        await asyncio.sleep(0.02)
    assert runtime.backend.get_connection(port).session_id != old


@pytest.mark.asyncio
async def test_known_boards_policy_skips_unknown(fake_cloud, tmp_path):
    backend = SimBenchBackend(hub_id="h", specs=[SimDeviceSpec(name="unknown")], speedup=50)
    rt = HubRuntime(settings_for(fake_cloud.url, tmp_path, auto_connect="known_boards"), backend=backend)
    await rt.start()
    try:
        await asyncio.sleep(0.2)
        assert backend.connections() == []
        assert len(backend.devices()) == 1
    finally:
        await rt.stop()


@pytest.mark.asyncio
async def test_uplink_status_in_profile_and_health(fake_cloud, tmp_path):
    from datetime import datetime, timezone

    status = tmp_path / "uplink.json"
    status.write_text(
        json.dumps({"active": "cellular", "updated_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8"
    )
    rt = HubRuntime(settings_for(fake_cloud.url, tmp_path), backend=SimBenchBackend(hub_id="h", specs=[], speedup=50))
    assert rt.profile()["uplink"] == "cellular"
    metrics = await rt.health.collect_health_metrics()
    assert metrics["uplink"]["active"] == "cellular"
    assert metrics["profile"]["mode"] == "bench"
    assert metrics["service"]["uplink_agent"]["connected"] is False


def test_app_serves_local_api_with_sim_profile(monkeypatch, tmp_path):
    """The FastAPI app starts the runtime and the local API answers from it."""
    import src.main as main

    settings = settings_for("ws://127.0.0.1:9/hub", tmp_path)
    monkeypatch.setattr(main, "settings", settings)
    with TestClient(main.app) as client:
        health = client.get("/health").json()
        assert health["hub_id"] == "rpi-bridge-01"
        assert health["uplink_connected"] is False

        for _ in range(50):
            ports = client.get("/ports").json()
            if ports["count"] == 3:
                break
        assert ports["count"] == 3
        port = ports["ports"][0]["port_id"]
        assert client.get(f"/ports/{port}").json()["connection_status"] == "connected"
        assert client.get("/ports/nope").status_code == 404

        task = client.post("/tasks/write", json={"port_id": port, "data": "hi"}).json()
        assert task["queued"] is True
        assert client.post("/tasks/write", json={"port_id": "nope", "data": "hi"}).status_code == 404

        assert client.delete(f"/connections/{port}").status_code == 200
        assert client.delete(f"/connections/{port}").status_code == 404
        reopened = client.post("/connections", json={"port_id": port}).json()
        assert reopened["session_id"].startswith("session-")

        status = client.get("/status").json()
        assert status["uplink_agent"]["is_connected"] is False
        assert status["profile"]["mode"] == "bench"
