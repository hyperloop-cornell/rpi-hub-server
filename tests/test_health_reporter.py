"""HealthReporter and UplinkStatusReader."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.health.health_reporter import HealthReporter
from src.health.uplink_status import UplinkStatusReader


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_collects_system_service_profile_and_uplink():
    reporter = HealthReporter(
        service_metrics=lambda: {"serial": {"active_connections": 2}},
        profile=lambda: {"name": "cellular-hub", "mode": "bench"},
        uplink=lambda: {"active": "cellular"},
    )
    metrics = await reporter.collect_health_metrics()
    assert metrics["timestamp"].endswith("Z")
    assert set(metrics["system"]) >= {"cpu", "memory", "disk"}
    assert metrics["service"] == {"serial": {"active_connections": 2}}
    assert metrics["profile"] == {"name": "cellular-hub", "mode": "bench"}
    assert metrics["uplink"] == {"active": "cellular"}


@pytest.mark.asyncio
async def test_optional_sections_omitted_and_provider_errors_contained():
    def broken():
        raise RuntimeError("boom")

    reporter = HealthReporter(service_metrics=broken, uplink=lambda: None)
    metrics = await reporter.collect_health_metrics()
    assert metrics["service"] == {}
    assert "uplink" not in metrics
    assert "profile" not in metrics


@pytest.mark.asyncio
async def test_reports_periodically_via_async_callback():
    reports = asyncio.Queue()
    reporter = HealthReporter(report_interval=0.05, health_callback=reports.put)
    await reporter.start()
    try:
        first = await asyncio.wait_for(reports.get(), 2)
        second = await asyncio.wait_for(reports.get(), 2)
        assert first["uptime_seconds"] <= second["uptime_seconds"]
    finally:
        await reporter.stop()


def test_port_error_tracking():
    reporter = HealthReporter()
    reporter.record_port_error("p1", "read")
    reporter.record_port_error("p1", "write")
    reporter.record_port_error("p2")
    assert reporter.get_error_count() == 3
    assert reporter.get_error_count("p1") == 2
    reporter.reset_port_errors("p1")
    assert reporter.get_error_count() == 1


def test_uplink_status_missing_file(tmp_path):
    reader = UplinkStatusReader(str(tmp_path / "missing.json"))
    assert reader.read() is None
    assert reader.active_uplink() is None


def test_uplink_status_fresh_and_stale(tmp_path):
    path = tmp_path / "status.json"
    now = datetime.now(timezone.utc)
    path.write_text(json.dumps({"active": "cellular", "updated_at": iso(now)}), encoding="utf-8")
    reader = UplinkStatusReader(str(path), stale_after_seconds=30)
    assert reader.read()["stale"] is False
    assert reader.active_uplink() == "cellular"

    path.write_text(json.dumps({"active": "wifi", "updated_at": iso(now - timedelta(minutes=5))}), encoding="utf-8")
    assert reader.read()["stale"] is True
    assert reader.active_uplink() is None


def test_uplink_status_garbage(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("{not json", encoding="utf-8")
    assert UplinkStatusReader(str(path)).read() is None
    path.write_text(json.dumps(["list"]), encoding="utf-8")
    assert UplinkStatusReader(str(path)).read() is None
