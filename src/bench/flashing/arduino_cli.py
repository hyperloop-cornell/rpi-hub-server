"""Compile and upload firmware with arduino-cli."""

import json
import os
import shutil
import tempfile
from typing import Any, Dict, Optional

from src.logging_config import get_logger

from .tools import ToolError, find_executable, run_tool

logger = get_logger(__name__)

SKETCH_NAME = "firmware"


class ArduinoCliFlasher:
    """Compiles .ino sources and uploads build output or prebuilt images via arduino-cli."""

    def __init__(self, compile_timeout: float = 300.0, upload_timeout: float = 120.0):
        self.compile_timeout = compile_timeout
        self.upload_timeout = upload_timeout

    def _cli(self) -> str:
        return find_executable("arduino-cli", "ARDUINO_CLI_PATH")

    async def detect_fqbn(self, port_path: str) -> str:
        """Ask arduino-cli which board is on `port_path`."""
        result = await run_tool([self._cli(), "board", "list", "--format", "json"], timeout=30)
        data = json.loads(result.stdout or "[]")
        # arduino-cli >= 0.35 wraps the list in {"detected_ports": [...]}
        ports = data.get("detected_ports", []) if isinstance(data, dict) else data
        for entry in ports:
            if entry.get("port", {}).get("address") == port_path:
                matching = entry.get("matching_boards") or []
                if matching and matching[0].get("fqbn"):
                    return matching[0]["fqbn"]
        raise ToolError(f"No board detected on {port_path}; select the board type explicitly")

    async def compile(self, source: bytes, fqbn: str, work_dir: str) -> str:
        """Compile a single-file sketch; returns the directory holding the build output."""
        sketch_dir = os.path.join(work_dir, SKETCH_NAME)
        output_dir = os.path.join(work_dir, "build")
        os.makedirs(sketch_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(sketch_dir, f"{SKETCH_NAME}.ino"), "wb") as f:
            f.write(source)

        await run_tool(
            [self._cli(), "compile", "--fqbn", fqbn, "--output-dir", output_dir, sketch_dir],
            timeout=self.compile_timeout,
        )
        return output_dir

    async def upload_dir(self, port_path: str, fqbn: str, build_dir: str) -> Dict[str, Any]:
        """Upload whatever the core's upload recipe expects from a build directory."""
        result = await run_tool(
            [self._cli(), "upload", "-p", port_path, "-b", fqbn, "--input-dir", build_dir, "--verify"],
            timeout=self.upload_timeout,
        )
        return {"flash_duration_ms": result.duration_ms, "output": result.stdout.strip()[-2000:]}

    async def flash(
        self,
        port_path: str,
        firmware: bytes,
        artifact_format: str,
        fqbn: Optional[str],
    ) -> Dict[str, Any]:
        """Compile (for .ino) and upload. Returns details for the task result."""
        work_dir = tempfile.mkdtemp(prefix="rpi-hub-flash-")
        try:
            if artifact_format == "ino":
                if not fqbn:
                    raise ToolError("Board FQBN is required for compiling .ino source files")
                build_dir = await self.compile(firmware, fqbn, work_dir)
            else:
                if not fqbn:
                    fqbn = await self.detect_fqbn(port_path)
                build_dir = os.path.join(work_dir, "build")
                os.makedirs(build_dir)
                # arduino-cli derives the project name from files named <name>.ino.<ext>
                with open(os.path.join(build_dir, f"{SKETCH_NAME}.ino.{artifact_format}"), "wb") as f:
                    f.write(firmware)

            result = await self.upload_dir(port_path, fqbn, build_dir)
            return {"board_fqbn": fqbn, **result}
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
