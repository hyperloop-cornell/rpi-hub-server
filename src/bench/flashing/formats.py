"""Firmware artifact formats: detection and validation."""

from typing import Optional

TEXT_FORMATS = {"ino", "hex"}
BINARY_FORMATS = {"bin", "elf"}
ALL_FORMATS = TEXT_FORMATS | BINARY_FORMATS

ELF_MAGIC = b"\x7fELF"


class FirmwareFormatError(ValueError):
    """The firmware payload does not match its declared or detectable format."""


def _decode_text(firmware: bytes) -> Optional[str]:
    try:
        return firmware.decode("utf-8")
    except UnicodeDecodeError:
        return None


def is_intel_hex(content: str) -> bool:
    """Strict Intel HEX check: every non-empty line is ':' followed by at least 10 hex digits."""
    lines = [line.strip() for line in content.strip().splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        hex_part = line[1:]
        if not line.startswith(":") or len(hex_part) < 10:
            return False
        if not all(c in "0123456789ABCDEFabcdef" for c in hex_part):
            return False
    return True


def looks_like_ino(content: str) -> bool:
    """Heuristic check that text is Arduino/C++ source."""
    lowered = content.lower()
    keywords = ("void setup", "void loop", "digitalwrite", "digitalread", "analogwrite", "analogread", "serial.", "pinmode")
    if any(keyword in lowered for keyword in keywords):
        return True
    first_lines = [line.strip() for line in content.strip().splitlines()[:5] if line.strip()]
    looks_like_hex = bool(first_lines) and all(line.startswith(":") for line in first_lines)
    return not looks_like_hex and any(ch in content for ch in "{};")


def detect_format(firmware: bytes, declared: Optional[str] = None) -> str:
    """Return the firmware format, validating `declared` when given.

    Without a declaration only text formats are inferred (ino/hex), matching what older GUIs
    send; binary images must be declared explicitly.
    """
    if not firmware:
        raise FirmwareFormatError("Firmware is empty")

    if declared:
        declared = declared.lower().lstrip(".")
        if declared not in ALL_FORMATS:
            raise FirmwareFormatError(f"Unsupported firmware format: {declared}")
        if declared == "elf" and not firmware.startswith(ELF_MAGIC):
            raise FirmwareFormatError("Firmware declared as .elf is not an ELF file")
        if declared in TEXT_FORMATS:
            text = _decode_text(firmware)
            if text is None:
                raise FirmwareFormatError(f"Firmware declared as .{declared} is not text")
            if declared == "hex" and not is_intel_hex(text):
                raise FirmwareFormatError("Firmware declared as .hex is not valid Intel HEX")
            if declared == "ino" and not looks_like_ino(text):
                raise FirmwareFormatError("Firmware declared as .ino does not look like Arduino source")
        return declared

    text = _decode_text(firmware)
    if text is None:
        raise FirmwareFormatError("Binary firmware must declare artifactFormat (bin or elf)")
    if is_intel_hex(text):
        return "hex"
    if looks_like_ino(text):
        return "ino"
    raise FirmwareFormatError("Invalid firmware format. Must be Intel HEX (.hex) or Arduino source (.ino)")
