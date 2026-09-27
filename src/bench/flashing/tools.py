"""Running external flashing tools (arduino-cli, OpenOCD) as subprocesses."""

import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import List, Optional

from src.logging_config import get_logger

logger = get_logger(__name__)


class ToolError(RuntimeError):
    """An external tool is missing, failed or timed out."""


@dataclass
class ToolResult:
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int


def find_executable(name: str, env_var: str) -> str:
    """Resolve a tool from an env var (file or directory) or PATH."""
    configured = os.environ.get(env_var)
    if configured:
        candidate = os.path.join(configured, name) if os.path.isdir(configured) else configured
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
        logger.warning(f"{env_var} is set but does not point to an executable", extra={env_var: configured})

    found = shutil.which(name)
    if found:
        return found
    raise ToolError(f"{name} not found. Install it and ensure it is on PATH, or set {env_var} to its full path.")


async def run_tool(args: List[str], timeout: float, cwd: Optional[str] = None) -> ToolResult:
    """Run a tool and return its output; raises ToolError on timeout or non-zero exit."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    logger.info("Running tool", extra={"command": " ".join(args)})

    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise ToolError(f"{os.path.basename(args[0])} timed out after {int(timeout)}s")

    result = ToolResult(
        returncode=process.returncode,
        stdout=stdout.decode(errors="replace") if stdout else "",
        stderr=stderr.decode(errors="replace") if stderr else "",
        duration_ms=int((loop.time() - started) * 1000),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise ToolError(f"{os.path.basename(args[0])} failed (exit {result.returncode}): {detail}")
    return result
