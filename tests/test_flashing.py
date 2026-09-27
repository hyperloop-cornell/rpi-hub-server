"""Firmware format detection and arduino-cli invocation."""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from src.bench.flashing import ArduinoCliFlasher, FirmwareFormatError, ToolError, detect_format
from src.bench.flashing.tools import ToolResult

HEX = b":100000000C9434000C943E000C943E000C943E0082\n:00000001FF\n"
INO = b"void setup() { pinMode(13, OUTPUT); }\nvoid loop() {}\n"


@pytest.mark.parametrize(
    "firmware,declared,expected",
    [
        (HEX, None, "hex"),
        (INO, None, "ino"),
        (HEX, "hex", "hex"),
        (INO, ".ino", "ino"),
        (b"\x00\x01\x02", "bin", "bin"),
        (b"\x7fELF\x01\x01", "elf", "elf"),
    ],
)
def test_detect_format(firmware, declared, expected):
    assert detect_format(firmware, declared) == expected


@pytest.mark.parametrize(
    "firmware,declared",
    [
        (b"", None),
        (b"\x00\xff binary", None),  # binary must be declared
        (b"hello world", None),
        (b"\x00\x01", "elf"),  # not an ELF file
        (INO, "hex"),
        (b"\xff\xfe", "ino"),
        (HEX, "exe"),
    ],
)
def test_detect_format_rejects(firmware, declared):
    with pytest.raises(FirmwareFormatError):
        detect_format(firmware, declared)


def ok(stdout=""):
    return ToolResult(returncode=0, stdout=stdout, stderr="", duration_ms=5)


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr("src.bench.flashing.arduino_cli.find_executable", lambda name, env: "/usr/bin/arduino-cli")
    return ArduinoCliFlasher(compile_timeout=10, upload_timeout=10)


@pytest.mark.asyncio
async def test_ino_compiles_then_uploads_build_dir(cli):
    calls = []

    async def fake_run(args, timeout, cwd=None):
        calls.append(args)
        if args[1] == "compile":
            sketch = args[-1]
            assert open(os.path.join(sketch, "firmware.ino"), "rb").read() == INO
        return ok()

    with patch("src.bench.flashing.arduino_cli.run_tool", new=fake_run):
        result = await cli.flash("/dev/ttyACM0", INO, "ino", "arduino:renesas_uno:minima")

    compile_args, upload_args = calls
    assert compile_args[:4] == ["/usr/bin/arduino-cli", "compile", "--fqbn", "arduino:renesas_uno:minima"]
    assert upload_args[1:6] == ["upload", "-p", "/dev/ttyACM0", "-b", "arduino:renesas_uno:minima"]
    assert "--input-dir" in upload_args
    assert upload_args[upload_args.index("--input-dir") + 1] == compile_args[compile_args.index("--output-dir") + 1]
    assert result["board_fqbn"] == "arduino:renesas_uno:minima"


@pytest.mark.asyncio
async def test_prebuilt_image_named_for_arduino_cli(cli):
    seen = {}

    async def fake_run(args, timeout, cwd=None):
        build_dir = args[args.index("--input-dir") + 1]
        seen["files"] = os.listdir(build_dir)
        return ok()

    with patch("src.bench.flashing.arduino_cli.run_tool", new=fake_run):
        await cli.flash("/dev/ttyUSB0", HEX, "hex", "arduino:avr:uno")
    assert seen["files"] == ["firmware.ino.hex"]


@pytest.mark.asyncio
async def test_ino_without_fqbn_fails(cli):
    with pytest.raises(ToolError, match="FQBN"):
        await cli.flash("/dev/ttyUSB0", INO, "ino", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "board_list",
    [
        # arduino-cli >= 0.35
        {"detected_ports": [{"port": {"address": "/dev/ttyUSB0"}, "matching_boards": [{"fqbn": "arduino:avr:uno"}]}]},
        # older releases
        [{"port": {"address": "/dev/ttyUSB0"}, "matching_boards": [{"fqbn": "arduino:avr:uno"}]}],
    ],
)
async def test_detect_fqbn_handles_both_output_shapes(cli, board_list):
    async def fake_run(args, timeout, cwd=None):
        return ok(json.dumps(board_list))

    with patch("src.bench.flashing.arduino_cli.run_tool", new=fake_run):
        assert await cli.detect_fqbn("/dev/ttyUSB0") == "arduino:avr:uno"


@pytest.mark.asyncio
async def test_detect_fqbn_unknown_port(cli):
    async def fake_run(args, timeout, cwd=None):
        return ok(json.dumps({"detected_ports": []}))

    with patch("src.bench.flashing.arduino_cli.run_tool", new=fake_run):
        with pytest.raises(ToolError, match="select the board"):
            await cli.detect_fqbn("/dev/ttyUSB9")


@pytest.mark.asyncio
async def test_run_tool_reports_failures(tmp_path):
    import sys

    from src.bench.flashing.tools import run_tool

    with pytest.raises(ToolError, match="exit 3"):
        await run_tool([sys.executable, "-c", "import sys; sys.stderr.write('nope'); sys.exit(3)"], timeout=10)
    with pytest.raises(ToolError, match="timed out"):
        await run_tool([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    result = await run_tool([sys.executable, "-c", "print('hi')"], timeout=10)
    assert result.stdout.strip() == "hi"
