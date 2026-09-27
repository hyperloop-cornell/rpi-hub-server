"""Pytest configuration and shared fixtures."""

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio
import websockets

# Hardware diagnostics under tests/manual are run by hand, not by pytest
collect_ignore_glob = ["manual/*"]


@pytest.fixture
def mock_serial_port():
    """Mock pyserial Serial object."""
    from unittest.mock import MagicMock

    port = MagicMock()
    port.is_open = True
    port.read.return_value = b"test data"
    port.write.return_value = 9
    return port


class FakeCloud:
    """A minimal stand-in for cloud-services' hub endpoint.

    Records every message a hub sends, acknowledges handshakes, and lets tests push commands
    or close the connection with a specific code.
    """

    def __init__(self, reject_with: Optional[int] = None):
        self.reject_with = reject_with
        self.messages: List[Dict[str, Any]] = []
        self.handshakes: List[Dict[str, Any]] = []
        self.connections: List[Any] = []
        self.connected = asyncio.Event()
        self.server = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/hub"

    async def start(self) -> None:
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = next(iter(self.server.sockets)).getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handler(self, ws, *_):
        handshake = json.loads(await ws.recv())
        self.handshakes.append(handshake)
        if self.reject_with:
            await ws.close(code=self.reject_with, reason="Invalid device token")
            return
        await ws.send(json.dumps({"type": "hub_connected", "hubId": handshake.get("hubId"), "timestamp": "t"}))
        self.connections.append(ws)
        self.connected.set()
        try:
            async for raw in ws:
                self.messages.append(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if ws in self.connections:
                self.connections.remove(ws)
            if not self.connections:
                self.connected.clear()

    async def send_command(self, command: Dict[str, Any]) -> None:
        await self.connections[-1].send(json.dumps({"type": "command", "command": command}))

    async def drop_connections(self) -> None:
        for ws in list(self.connections):
            await ws.close(code=1011, reason="test drop")

    def of_type(self, message_type: str) -> List[Dict[str, Any]]:
        return [m for m in self.messages if m.get("type") == message_type]

    async def wait_for(self, predicate, timeout: float = 5.0):
        """Wait until predicate(self) is truthy; returns its value."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            result = predicate(self)
            if result:
                return result
            if loop.time() > deadline:
                raise AssertionError("condition not met; messages: " + json.dumps(self.messages)[-2000:])
            await asyncio.sleep(0.02)


@pytest_asyncio.fixture
async def fake_cloud():
    cloud = FakeCloud()
    await cloud.start()
    yield cloud
    await cloud.stop()
