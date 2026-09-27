"""Board registry: recognizes boards by USB VID/PID and describes how to handle them."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import yaml

from src.logging_config import get_logger

logger = get_logger(__name__)

Flasher = Literal["arduino-cli", "openocd", "none"]
ResetMethod = Literal["dtr", "openocd", "none"]


@dataclass(frozen=True)
class BoardProfile:
    id: str
    name: str
    match: Tuple[Tuple[str, Optional[str]], ...] = ()
    fqbn: Optional[str] = None
    artifacts: Tuple[str, ...] = ()
    flasher: Flasher = "arduino-cli"
    openocd_config: Optional[str] = None
    baud: Optional[int] = None
    reset: ResetMethod = "dtr"
    reset_on_open: bool = True
    auto_connect: bool = True
    compile_timeout: float = 300.0
    upload_timeout: float = 120.0

    def summary(self) -> Dict[str, Any]:
        """What the cloud/GUI needs to know about the board (BoardProfileInfo)."""
        return {"id": self.id, "name": self.name, "fqbn": self.fqbn, "artifacts": list(self.artifacts)}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "BoardProfile":
        match = tuple(
            (str(m["vid"]).lower().zfill(4), str(m["pid"]).lower().zfill(4) if m.get("pid") else None)
            for m in raw.get("match", [])
        )
        known = {k for k in cls.__dataclass_fields__} - {"match", "artifacts"}
        values = {k: v for k, v in raw.items() if k in known}
        unknown = set(raw) - known - {"match", "artifacts"}
        if unknown:
            raise ValueError(f"Board {raw.get('id')}: unknown fields {sorted(unknown)}")
        return cls(match=match, artifacts=tuple(raw.get("artifacts", [])), **values)


@dataclass
class BoardRegistry:
    boards: List[BoardProfile] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "BoardRegistry":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        boards = [BoardProfile.from_dict(raw) for raw in data.get("boards", [])]
        ids = [b.id for b in boards]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"Duplicate board ids in {path}: {sorted(duplicates)}")
        logger.info("Loaded board registry", extra={"path": str(path), "boards": len(boards)})
        return cls(boards)

    def get(self, board_id: Optional[str]) -> Optional[BoardProfile]:
        return next((b for b in self.boards if b.id == board_id), None) if board_id else None

    def match(self, vendor_id: Optional[str], product_id: Optional[str]) -> Optional[BoardProfile]:
        """Most specific match: exact VID+PID first, then vendor-wide entries."""
        if not vendor_id:
            return None
        vid = vendor_id.lower().zfill(4)
        pid = product_id.lower().zfill(4) if product_id else None
        for board in self.boards:
            if any(m_vid == vid and m_pid is not None and m_pid == pid for m_vid, m_pid in board.match):
                return board
        for board in self.boards:
            if any(m_vid == vid and m_pid is None for m_vid, m_pid in board.match):
                return board
        return None

    def match_device(self, device: Any) -> Optional[BoardProfile]:
        """Match anything with vendor_id/product_id attributes (DeviceInfo, BenchDevice)."""
        return self.match(getattr(device, "vendor_id", None), getattr(device, "product_id", None))

    def baud_policy(self, device: Any) -> Optional[int]:
        """For USBPortMapper: board baud (no probing), 0 for devices that will not be opened
        (no probing either), None to probe."""
        board = self.match_device(device)
        if board is None or not board.auto_connect:
            return 0
        return board.baud

    def artifact_formats(self) -> List[str]:
        """Every firmware format some registered board accepts."""
        order = ["ino", "hex", "bin", "elf"]
        present = {fmt for b in self.boards if b.flasher != "none" for fmt in b.artifacts}
        return [fmt for fmt in order if fmt in present]
