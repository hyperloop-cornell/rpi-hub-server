"""Board registry, OpenOCD flasher and board-aware flashing in the USB backend."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bench.backend import BenchError, FlashRequest
from src.bench.boards import BoardProfile, BoardRegistry
from src.bench.flashing import OpenOcdFlasher
from src.bench.flashing.tools import ToolResult
from src.bench.usb_backend import UsbBenchBackend, default_flasher
from src.bench.usb_port_mapper import DeviceInfo

REGISTRY_PATH = Path(__file__).parent.parent / "config" / "boards.yaml"
HEX = b":100000000C9434000C943E000C943E000C943E0082\n:00000001FF\n"


@pytest.fixture(scope="module")
def registry():
    return BoardRegistry.load(REGISTRY_PATH)


@pytest.mark.parametrize(
    "vid,pid,board_id",
    [
        ("2341", "0043", "uno_r3"),
        ("2341", "0042", "mega2560"),
        ("1a86", "7523", "nano_ch340"),
        ("2341", "0069", "uno_r4_minima"),
        ("2341", "0369", "uno_r4_minima_bootloader"),
        ("2341", "1002", "uno_r4_wifi"),
        ("0483", "374b", "disco_f407vg"),
        ("0483", "374B", "disco_f407vg"),  # case-insensitive
        ("2341", "8036", "generic_arduino"),  # vendor-wide fallback
        ("10c4", "ea60", "usb_serial_cp210x"),
    ],
)
def test_registry_matches(registry, vid, pid, board_id):
    assert registry.match(vid, pid).id == board_id


@pytest.mark.parametrize("vid,pid", [("1e0e", "9001"), ("2c7c", "0125"), ("1199", "9071"), (None, None)])
def test_modems_and_unknown_devices_do_not_match(registry, vid, pid):
    assert registry.match(vid, pid) is None


def test_registry_profiles_are_consistent(registry):
    for board in registry.boards:
        assert set(board.artifacts) <= {"ino", "hex", "bin", "elf"}, board.id
        if board.flasher == "openocd":
            assert board.openocd_config, board.id
        if "ino" in board.artifacts and board.flasher != "none" and not board.id.startswith(("generic", "usb_serial")):
            assert board.fqbn, board.id
    assert registry.artifact_formats() == ["ino", "hex", "bin", "elf"]


def test_bootloader_modes_never_auto_connect(registry):
    assert registry.get("uno_r4_minima_bootloader").auto_connect is False
    assert registry.baud_policy(SimpleNamespace(vendor_id="2341", product_id="0369")) == 0


def test_baud_policy(registry):
    assert registry.baud_policy(SimpleNamespace(vendor_id="2341", product_id="0069")) == 115200
    assert registry.baud_policy(SimpleNamespace(vendor_id="2341", product_id="0043")) is None  # probe
    assert registry.baud_policy(SimpleNamespace(vendor_id="1e0e", product_id="9001")) == 0  # never probe


def test_summary_for_cloud(registry):
    assert registry.get("disco_f407vg").summary() == {
        "id": "disco_f407vg",
        "name": "STM32F407G-DISC1",
        "fqbn": "STMicroelectronics:stm32:Disco:pnum=DISCO_F407VG",
        "artifacts": ["ino", "bin", "elf", "hex"],
    }


def test_unknown_fields_rejected(tmp_path):
    path = tmp_path / "boards.yaml"
    path.write_text("boards:\n  - id: x\n    name: X\n    fqbm: typo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fqbm"):
        BoardRegistry.load(path)


def test_duplicate_ids_rejected(tmp_path):
    path = tmp_path / "boards.yaml"
    path.write_text("boards:\n  - {id: x, name: X}\n  - {id: x, name: Y}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        BoardRegistry.load(path)


# ----------------------------------------------------------------------------- OpenOCD


@pytest.fixture
def openocd(monkeypatch):
    monkeypatch.setattr("src.bench.flashing.openocd.find_executable", lambda name, env: "/usr/bin/openocd")
    return OpenOcdFlasher("board/stm32f4discovery.cfg", adapter_serial="066DFF")


def test_openocd_program_args(openocd):
    assert openocd.program_args("/tmp/w/firmware.elf", "elf") == [
        "/usr/bin/openocd", "-f", "board/stm32f4discovery.cfg",
        "-c", "adapter serial 066DFF",
        "-c", "program {/tmp/w/firmware.elf} verify reset exit",
    ]
    bin_args = openocd.program_args("C:\\t\\fw.bin", "bin")
    assert bin_args[-1] == "program {C:/t/fw.bin} 0x08000000 verify reset exit"


def test_openocd_reset_args(openocd):
    assert openocd.reset_args()[-6:] == ["-c", "init", "-c", "reset run", "-c", "shutdown"]


@pytest.mark.asyncio
async def test_openocd_compiles_ino_and_programs_elf(openocd, tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    (build / "firmware.ino.elf").write_bytes(b"\x7fELF")
    (build / "firmware.ino.bin").write_bytes(b"\x00")
    openocd.compiler = MagicMock()
    openocd.compiler.compile = AsyncMock(return_value=str(build))

    calls = []

    async def fake_run(args, timeout, cwd=None):
        calls.append(args)
        return ToolResult(0, "", "** Programming Finished **", 10)

    with patch("src.bench.flashing.openocd.run_tool", new=fake_run):
        result = await openocd.flash("/dev/ttyACM0", b"void setup(){}", "ino", "STMicroelectronics:stm32:Disco:pnum=DISCO_F407VG")

    openocd.compiler.compile.assert_awaited_once()
    assert calls[0][-1].endswith("firmware.ino.elf} verify reset exit")
    assert result["flasher"] == "openocd"
    assert "Programming Finished" in result["output"]


def test_default_flasher_selection(registry):
    device = SimpleNamespace(serial_number="066DFF")
    stm = default_flasher(registry.get("disco_f407vg"), device)
    assert isinstance(stm, OpenOcdFlasher)
    assert stm.adapter_serial == "066DFF"
    assert stm.compiler.compile_timeout == 900
    assert type(default_flasher(registry.get("uno_r4_minima"), device)).__name__ == "ArduinoCliFlasher"
    assert type(default_flasher(None, device)).__name__ == "ArduinoCliFlasher"


# ----------------------------------------------------------------------------- USB backend


class FakeFlasher:
    def __init__(self):
        self.calls = []

    async def flash(self, port_path, firmware, artifact_format, fqbn):
        self.calls.append((port_path, artifact_format, fqbn))
        return {"flash_duration_ms": 1, "output": "ok"}


def usb_backend(registry, vid, pid, flasher):
    info = DeviceInfo(port_id="port_1", device_path="/dev/ttyACM0", vendor_id=vid, product_id=pid,
                      serial_number="SN1", location="1-1.2")
    mapper = MagicMock()
    mapper.port_id_to_device_info = {"port_1": info}
    mapper.suppress.return_value = {"loc:1-1.2"}
    mapper.release = AsyncMock()
    mapper.refresh = AsyncMock()
    serial_manager = MagicMock()
    serial_manager.get_connection.return_value = None
    serial_manager.open_connection = AsyncMock(return_value=False)
    backend = UsbBenchBackend(
        hub_id="h", mapper=mapper, serial_manager=serial_manager, registry=registry,
        flasher_factory=lambda board, device: flasher,
    )
    return backend, mapper


@pytest.fixture(autouse=True)
def no_settle_delay(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr("src.bench.usb_backend.asyncio.sleep", fast_sleep)


@pytest.mark.asyncio
async def test_usb_flash_uses_detected_board_fqbn(registry):
    flasher = FakeFlasher()
    backend, mapper = usb_backend(registry, "2341", "0069", flasher)
    result = await backend.flash("port_1", FlashRequest(firmware=b"\x00\x01", artifact_format="bin"))
    assert flasher.calls == [("/dev/ttyACM0", "bin", "arduino:renesas_uno:minima")]
    assert result["board_profile"] == "uno_r4_minima"
    mapper.suppress.assert_called_once()
    mapper.release.assert_awaited_once_with({"loc:1-1.2"})


@pytest.mark.asyncio
async def test_usb_flash_rejects_format_board_cannot_take(registry):
    backend, _ = usb_backend(registry, "2341", "0069", FakeFlasher())
    with pytest.raises(BenchError, match="does not accept .hex"):
        await backend.flash("port_1", FlashRequest(firmware=HEX, artifact_format="hex"))


@pytest.mark.asyncio
async def test_usb_flash_board_profile_override(registry):
    """A generic USB-serial device flashed as the board the user picked in the GUI."""
    flasher = FakeFlasher()
    backend, _ = usb_backend(registry, "10c4", "ea60", flasher)
    await backend.flash("port_1", FlashRequest(firmware=HEX, artifact_format="hex", board_profile="uno_r3"))
    assert flasher.calls[0][2] == "arduino:avr:uno"

    with pytest.raises(BenchError, match="Unknown board profile"):
        await backend.flash("port_1", FlashRequest(firmware=HEX, artifact_format="hex", board_profile="nope"))


@pytest.mark.asyncio
async def test_usb_flash_bootloader_mode_refused(registry):
    backend, _ = usb_backend(registry, "2341", "0369", FakeFlasher())
    with pytest.raises(BenchError, match="current mode"):
        await backend.flash("port_1", FlashRequest(firmware=b"\x00", artifact_format="bin"))


@pytest.mark.asyncio
async def test_usb_restart_uses_openocd_for_stm32(registry, monkeypatch):
    backend, _ = usb_backend(registry, "0483", "374b", FakeFlasher())
    resets = []

    async def fake_reset(self):
        resets.append((self.config, self.adapter_serial))

    monkeypatch.setattr(OpenOcdFlasher, "reset", fake_reset)
    with pytest.raises(BenchError):  # reopening fails in this fake, after the reset
        await backend.restart("port_1")
    assert resets == [("board/stm32f4discovery.cfg", "SN1")]
    assert backend.serial.open_connection.await_args.kwargs["reset_on_open"] is False


def test_usb_backend_capabilities(registry):
    backend, _ = usb_backend(registry, "2341", "0043", FakeFlasher())
    assert backend.capabilities() == ["bench", "flash:ino", "flash:hex", "flash:bin", "flash:elf"]
