"""Program and reset STM32 boards through an ST-LINK with OpenOCD."""

import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

from src.logging_config import get_logger

from .arduino_cli import ArduinoCliFlasher
from .tools import ToolError, find_executable, run_tool

logger = get_logger(__name__)

# Where raw .bin images are written (start of STM32 internal flash)
STM32_FLASH_BASE = "0x08000000"


def _tcl_path(path: str) -> str:
    """OpenOCD parses commands as Tcl: forward slashes and braces survive spaces."""
    return "{" + path.replace("\\", "/") + "}"


class OpenOcdFlasher:
    """Flashes .elf/.hex/.bin images (compiling .ino with arduino-cli first) via OpenOCD."""

    def __init__(
        self,
        config: str,
        adapter_serial: Optional[str] = None,
        compiler: Optional[ArduinoCliFlasher] = None,
        timeout: float = 120.0,
    ):
        """
        Args:
            config: OpenOCD board or target script, e.g. board/stm32f4discovery.cfg
            adapter_serial: ST-LINK serial number, so the right probe is used when several
                boards are attached (the ST-LINK virtual COM port reports the same serial)
            compiler: Used to compile .ino sources into an .elf
        """
        self.config = config
        self.adapter_serial = adapter_serial
        self.compiler = compiler or ArduinoCliFlasher()
        self.timeout = timeout

    def _base_args(self) -> List[str]:
        args = [find_executable("openocd", "OPENOCD_PATH"), "-f", self.config]
        if self.adapter_serial:
            args += ["-c", f"adapter serial {self.adapter_serial}"]
        return args

    def program_args(self, image_path: str, artifact_format: str) -> List[str]:
        address = f" {STM32_FLASH_BASE}" if artifact_format == "bin" else ""
        return self._base_args() + ["-c", f"program {_tcl_path(image_path)}{address} verify reset exit"]

    def reset_args(self) -> List[str]:
        return self._base_args() + ["-c", "init", "-c", "reset run", "-c", "shutdown"]

    async def flash(
        self, port_path: str, firmware: bytes, artifact_format: str, fqbn: Optional[str]
    ) -> Dict[str, Any]:
        """Same signature as ArduinoCliFlasher.flash; port_path is unused (OpenOCD talks to the ST-LINK)."""
        work_dir = tempfile.mkdtemp(prefix="rpi-hub-openocd-")
        try:
            if artifact_format == "ino":
                if not fqbn:
                    raise ToolError("Board FQBN is required for compiling .ino source files")
                build_dir = await self.compiler.compile(firmware, fqbn, work_dir)
                elf = next((f for f in os.listdir(build_dir) if f.endswith(".elf")), None)
                if elf is None:
                    raise ToolError("Compilation produced no .elf image")
                image_path, image_format = os.path.join(build_dir, elf), "elf"
            else:
                image_path, image_format = os.path.join(work_dir, f"firmware.{artifact_format}"), artifact_format
                with open(image_path, "wb") as f:
                    f.write(firmware)

            result = await run_tool(self.program_args(image_path, image_format), timeout=self.timeout)
            # OpenOCD logs to stderr
            output = (result.stderr or result.stdout).strip()[-2000:]
            return {"board_fqbn": fqbn, "flash_duration_ms": result.duration_ms, "output": output, "flasher": "openocd"}
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def reset(self) -> None:
        await run_tool(self.reset_args(), timeout=30)
