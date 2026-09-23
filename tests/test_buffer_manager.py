"""Test Buffer Manager."""

import asyncio

import pytest

from src.uplink.buffer_manager import BufferManager, BufferedMessage


@pytest.fixture
def buffer_manager():
    """Create Buffer Manager instance."""
    return BufferManager(size_mb=0.001, warn_threshold=0.8)  # 1KB for testing


@pytest.mark.asyncio
async def test_buffer_manager_initialization(buffer_manager):
    """Test buffer manager initialization."""
    assert buffer_manager.size_bytes == int(0.001 * 1024 * 1024)
    assert buffer_manager.warn_threshold == 0.8
    assert buffer_manager.current_size_bytes == 0
    assert len(buffer_manager.buffer) == 0


@pytest.mark.asyncio
async def test_add_message(buffer_manager):
    """Test adding message to buffer."""
    payload = {"port_id": "port_0", "data": "test"}

    result = buffer_manager.add_message("telemetry", payload)

    assert result is True
    assert len(buffer_manager.buffer) == 1
    assert buffer_manager.current_size_bytes > 0


@pytest.mark.asyncio
async def test_add_multiple_messages(buffer_manager):
    """Test adding multiple messages."""
    for i in range(5):
        payload = {"port_id": f"port_{i}", "data": f"test_{i}"}
        buffer_manager.add_message("telemetry", payload)

    assert len(buffer_manager.buffer) == 5


@pytest.mark.asyncio
async def test_buffer_overflow_drops_oldest(buffer_manager):
    """Test that buffer drops oldest messages when full."""
    # Add messages until buffer overflows
    for i in range(100):
        payload = {"port_id": f"port_{i}", "data": "x" * 100}
        buffer_manager.add_message("telemetry", payload)

    # Buffer should have dropped some messages
    assert buffer_manager._total_drops > 0
    assert buffer_manager.current_size_bytes <= buffer_manager.size_bytes


@pytest.mark.asyncio
async def test_threshold_warning(buffer_manager):
    """Test warning when crossing threshold."""
    # Fill buffer past 80% threshold
    for i in range(20):
        payload = {"port_id": f"port_{i}", "data": "x" * 50}
        buffer_manager.add_message("telemetry", payload)

    # Should trigger threshold warning
    assert buffer_manager.is_above_threshold() or buffer_manager._total_drops > 0


@pytest.mark.asyncio
async def test_threshold_crossing_up_and_down(buffer_manager):
    """Test threshold crossing in both directions."""
    # Fill past threshold
    for i in range(20):
        payload = {"data": "x" * 50}
        buffer_manager.add_message("telemetry", payload)

    was_above = buffer_manager.is_above_threshold()

    # Clear buffer
    buffer_manager.clear()

    # Should be below threshold now
    assert not buffer_manager.is_above_threshold()


@pytest.mark.asyncio
async def test_get_messages(buffer_manager):
    """Test getting messages without removing."""
    for i in range(3):
        payload = {"id": i}
        buffer_manager.add_message("telemetry", payload)

    messages = await buffer_manager.get_messages()

    assert len(messages) == 3
    assert len(buffer_manager.buffer) == 3  # Still in buffer


@pytest.mark.asyncio
async def test_get_messages_with_limit(buffer_manager):
    """Test getting messages with limit."""
    for i in range(5):
        payload = {"id": i}
        buffer_manager.add_message("telemetry", payload)

    messages = await buffer_manager.get_messages(max_messages=2)

    assert len(messages) == 2


@pytest.mark.asyncio
async def test_pop_message(buffer_manager):
    """Test popping message from buffer."""
    payload = {"data": "test"}
    buffer_manager.add_message("telemetry", payload)

    initial_size = buffer_manager.current_size_bytes
    message = await buffer_manager.pop_message()

    assert message is not None
    assert message.message_type == "telemetry"
    assert len(buffer_manager.buffer) == 0
    assert buffer_manager.current_size_bytes < initial_size


@pytest.mark.asyncio
async def test_pop_message_empty_buffer(buffer_manager):
    """Test popping from empty buffer."""
    message = await buffer_manager.pop_message()
    assert message is None


@pytest.mark.asyncio
async def test_pop_messages_multiple(buffer_manager):
    """Test popping multiple messages."""
    for i in range(5):
        buffer_manager.add_message("telemetry", {"id": i})

    messages = await buffer_manager.pop_messages(3)

    assert len(messages) == 3
    assert len(buffer_manager.buffer) == 2


@pytest.mark.asyncio
async def test_pop_messages_more_than_available(buffer_manager):
    """Test popping more messages than available."""
    for i in range(3):
        buffer_manager.add_message("telemetry", {"id": i})

    messages = await buffer_manager.pop_messages(10)

    assert len(messages) == 3
    assert len(buffer_manager.buffer) == 0


@pytest.mark.asyncio
async def test_clear_buffer(buffer_manager):
    """Test clearing buffer."""
    for i in range(5):
        buffer_manager.add_message("telemetry", {"id": i})

    buffer_manager.clear()

    assert len(buffer_manager.buffer) == 0
    assert buffer_manager.current_size_bytes == 0
    assert not buffer_manager.is_above_threshold()


@pytest.mark.asyncio
async def test_get_stats(buffer_manager):
    """Test getting buffer statistics."""
    for i in range(3):
        buffer_manager.add_message("telemetry", {"id": i})

    stats = buffer_manager.get_stats()

    assert "size_mb" in stats
    assert "used_mb" in stats
    assert "utilization_percent" in stats
    assert "message_count" in stats
    assert stats["message_count"] == 3
    assert stats["total_messages"] == 3


@pytest.mark.asyncio
async def test_get_utilization(buffer_manager):
    """Test getting utilization."""
    utilization = buffer_manager.get_utilization()
    assert 0 <= utilization <= 1

    # Add some messages
    for i in range(5):
        buffer_manager.add_message("telemetry", {"data": "x" * 50})

    utilization = buffer_manager.get_utilization()
    assert utilization > 0


@pytest.mark.asyncio
async def test_get_message_count(buffer_manager):
    """Test getting message count."""
    assert buffer_manager.get_message_count() == 0

    buffer_manager.add_message("telemetry", {"data": "test"})
    assert buffer_manager.get_message_count() == 1


@pytest.mark.asyncio
async def test_reset_drop_count(buffer_manager):
    """Test resetting drop count."""
    # Force some drops
    for i in range(100):
        buffer_manager.add_message("telemetry", {"data": "x" * 100})

    drops = buffer_manager.reset_drop_count()
    assert drops >= 0
    assert buffer_manager._drop_count == 0


@pytest.mark.asyncio
async def test_fifo_ordering(buffer_manager):
    """Test FIFO ordering of messages."""
    for i in range(5):
        buffer_manager.add_message("telemetry", {"id": i})

    # Pop messages and verify order
    msg1 = await buffer_manager.pop_message()
    msg2 = await buffer_manager.pop_message()

    assert msg1.payload["id"] == 0  # First in
    assert msg2.payload["id"] == 1  # Second in


@pytest.mark.asyncio
async def test_message_size_estimation(buffer_manager):
    """Test message size estimation."""
    small_payload = {"data": "x"}
    large_payload = {"data": "x" * 1000}

    buffer_manager.add_message("telemetry", small_payload)
    size_after_small = buffer_manager.current_size_bytes

    buffer_manager.add_message("telemetry", large_payload)
    size_after_large = buffer_manager.current_size_bytes

    # Large message should increase size more
    assert size_after_large > size_after_small


@pytest.mark.asyncio
async def test_buffered_message_dataclass():
    """Test BufferedMessage dataclass."""
    from datetime import datetime

    msg = BufferedMessage(
        message_type="telemetry",
        payload={"test": "data"},
        timestamp=datetime.now(),
        size_bytes=100,
    )

    assert msg.message_type == "telemetry"
    assert msg.payload["test"] == "data"
    assert msg.size_bytes == 100


@pytest.mark.asyncio
async def test_overflow_drops_telemetry_before_other_messages():
    """Task results and device events survive a long outage; telemetry is dropped first."""
    buffer = BufferManager(size_mb=0.001)
    buffer.add_message("task_status", {"taskId": "t1", "status": "completed"})
    for i in range(50):
        buffer.add_message("telemetry", {"data": "x" * 50, "i": i})
    types = [m.message_type for m in buffer.buffer]
    assert types[0] == "task_status"
    assert buffer.get_stats()["total_drops"] > 0
    assert buffer.current_size_bytes <= buffer.size_bytes


@pytest.mark.asyncio
async def test_requeue_front_preserves_order():
    buffer = BufferManager(size_mb=1)
    buffer.add_message("telemetry", {"i": 1})
    buffer.add_message("telemetry", {"i": 2})
    first = await buffer.pop_message()
    buffer.requeue_front(first)
    assert [m.payload["i"] for m in buffer.buffer] == [1, 2]


@pytest.mark.asyncio
async def test_wait_for_message_wakes_on_add():
    buffer = BufferManager(size_mb=1)
    waiter = asyncio.create_task(buffer.wait_for_message(timeout=2))
    await asyncio.sleep(0)
    buffer.add_message("health", {"ok": True})
    assert await waiter is True
    assert await buffer.wait_for_message(timeout=0.01) is True
    await buffer.pop_message()
    assert await buffer.wait_for_message(timeout=0.01) is False


@pytest.mark.asyncio
async def test_oversized_message_rejected():
    buffer = BufferManager(size_mb=0.001)
    assert buffer.add_message("telemetry", {"data": "x" * 5000}) is False
    assert buffer.get_message_count() == 0
