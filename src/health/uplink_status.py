"""Reads the network uplink status written by the uplink manager (wifi-fallback-relay repo).

The uplink manager runs only on the cellular hub. It atomically rewrites a JSON file such as:

    {
      "active": "wifi" | "cellular" | "none",
      "since": "2026-09-23T12:00:00Z",
      "updated_at": "2026-09-23T12:00:05Z",
      "wifi": {"link": true, "rssi_dbm": -55, "rtt_ms": 32.1},
      "cellular": {"link": true, "rssi_dbm": -81, "rtt_ms": 95.0, "access_tech": "lte"}
    }

The hub never changes networking itself; it only reports this state in health messages.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.logging_config import get_logger

logger = get_logger(__name__)


class UplinkStatusReader:
    """Returns the latest uplink status, or None when no uplink manager runs on this hub."""

    def __init__(self, path: str, stale_after_seconds: float = 30.0):
        self.path = Path(path) if path else None
        self.stale_after_seconds = stale_after_seconds
        self._warned = False

    def read(self) -> Optional[Dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return None
        try:
            status = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            if not self._warned:
                logger.warning(f"Could not read uplink status {self.path}: {e}")
                self._warned = True
            return None
        if not isinstance(status, dict):
            return None

        self._warned = False
        status["stale"] = self._is_stale(status.get("updated_at"))
        return status

    def active_uplink(self) -> Optional[str]:
        status = self.read()
        if not status or status.get("stale"):
            return None
        active = status.get("active")
        return active if active in ("wifi", "cellular") else None

    def _is_stale(self, updated_at: Any) -> bool:
        if not isinstance(updated_at, str):
            return True
        try:
            moment = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - moment).total_seconds() > self.stale_after_seconds
