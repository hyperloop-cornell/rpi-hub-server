"""Configuration: config/config.yaml, an optional profile overlay, and environment variables.

Every Pi runs the same code; what differs is the profile selected with HUB_PROFILE:

    HUB_PROFILE=lab-hub        -> config/profiles/lab-hub.yaml
    HUB_PROFILE=cellular-hub   -> config/profiles/cellular-hub.yaml
    HUB_PROFILE=/path/x.yaml   -> that file

The profile is deep-merged over config/config.yaml. Values of the form ${VAR:default} in
either file are replaced from the environment (and .env). Secrets such as DEVICE_TOKEN stay
in .env, never in profiles.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Load .env file at module import
load_dotenv()

CONFIG_DIR = Path("config")
PROFILES_DIR = CONFIG_DIR / "profiles"

_ENV_PATTERN = re.compile(r"\$\{([^:}]+):([^}]*)\}")


class HubConfig(BaseModel):
    """Hub identity and cloud connection."""

    hub_id: str = Field(default="rpi-bridge-01", description="Unique hub identifier")
    server_endpoint: str = Field(default="ws://localhost:8080/hub", description="WebSocket server endpoint")
    device_token: str = Field(default="", description="Authentication token for hub")
    reconnect_interval: float = Field(default=5, description="Base reconnect delay in seconds")
    max_reconnect_attempts: int = Field(default=0, description="Consecutive failures before giving up; 0 = never")
    mode: Literal["bench", "pod"] = Field(default="bench", description="bench = USB MCUs; pod = EtherCAT master")
    profile_name: str = Field(default="", description="Name of the selected profile (set automatically)")

    @field_validator("mode")
    @classmethod
    def pod_mode_not_available(cls, v: str) -> str:
        if v == "pod":
            raise ValueError("hub.mode 'pod' (EtherCAT master) is not implemented yet; use 'bench'")
        return v


class SerialConfig(BaseModel):
    """Serial communication configuration."""

    default_baud_rate: int = Field(default=9600, description="Default baud rate")
    scan_interval: int = Field(default=2, description="Port scan interval in seconds")
    default_timeout: float = Field(default=1.0, description="Default serial timeout in seconds")
    max_connections: int = Field(default=10, description="Maximum simultaneous connections")
    task_queue_size: int = Field(default=100, description="Unused; kept for existing config files")
    connection_retry_attempts: int = Field(default=3, description="Connection retry attempts")
    auto_connect: bool = Field(default=True, description="Automatically open detected devices")


class BenchConfig(BaseModel):
    """Where bench devices come from and which ones are opened automatically."""

    source: Literal["usb", "sim"] = Field(default="usb", description="usb = real devices, sim = simulated")
    auto_connect: Literal["known_boards", "all", "none"] = Field(
        default="all",
        description="known_boards = only devices in the board registry; all = every USB serial device",
    )
    sim_devices: List[Dict[str, Any]] = Field(default_factory=list, description="Simulated devices (source: sim)")
    sim_speedup: float = Field(default=1.0, description="Speeds up simulated restarts/flashes (tests)")


class BufferConfig(BaseModel):
    """Buffer configuration."""

    size_mb: float = Field(default=10, description="Buffer size in megabytes")
    warn_threshold: float = Field(default=0.8, description="Warning threshold (0.0-1.0)")
    telemetry_while_disconnected: bool = Field(default=True, description="Keep telemetry during cloud outages")


class HealthConfig(BaseModel):
    """Health reporter configuration."""

    report_interval: int = Field(default=30, description="Health report interval in seconds")
    cpu_threshold: float = Field(default=80.0, description="CPU usage alert threshold")
    memory_threshold: float = Field(default=85.0, description="Memory usage alert threshold")


class UplinkConfig(BaseModel):
    """Status file written by the uplink manager (only present on the cellular hub)."""

    status_file: str = Field(default="/run/hyperloop-uplink/status.json")
    stale_after_seconds: float = Field(default=30.0)


class StorageConfig(BaseModel):
    """Storage paths configuration."""

    artifact_cache_path: str = Field(default="/tmp/rpi-hub-artifacts", description="Artifact cache directory")
    artifact_retention_hours: int = Field(default=24, description="Artifact retention in hours")
    port_mapping_file: str = Field(default="/var/lib/rpi-hub/port_mappings.json", description="Port mapping file")
    log_directory: str = Field(default="/var/log/rpi-hub", description="Log directory")


class APIConfig(BaseModel):
    """Local HTTP API (debugging and local tools; the GUI goes through the cloud)."""

    host: str = Field(default="127.0.0.1", description="API server host")
    port: int = Field(default=8000, description="API server port")
    cors_origins: List[str] = Field(default=["*"], description="CORS allowed origins")


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = Field(default="INFO", description="Log level")
    format: str = Field(default="json", description="Log format (json or text)")


class Settings(BaseModel):
    """Main application settings."""

    hub: HubConfig = Field(default_factory=HubConfig)
    serial: SerialConfig = Field(default_factory=SerialConfig)
    bench: BenchConfig = Field(default_factory=BenchConfig)
    buffer: BufferConfig = Field(default_factory=BufferConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    uplink: UplinkConfig = Field(default_factory=UplinkConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def auto_connect_policy(self) -> str:
        """Effective auto-connect policy (serial.auto_connect: false turns it off entirely)."""
        return self.bench.auto_connect if self.serial.auto_connect else "none"

    @classmethod
    def load_from_yaml(cls, yaml_path: Optional[Path] = None, profile: Optional[str] = None) -> "Settings":
        """Load config.yaml, overlay the profile (HUB_PROFILE when not given), substitute env vars."""
        yaml_path = Path(yaml_path) if yaml_path else CONFIG_DIR / "config.yaml"
        config: Dict[str, Any] = _load_yaml(yaml_path) if yaml_path.exists() else {}

        profile = profile if profile is not None else os.getenv("HUB_PROFILE", "").strip()
        if profile:
            profile_path = resolve_profile_path(profile, yaml_path.parent / "profiles")
            config = deep_merge(config, _load_yaml(profile_path))
            config.setdefault("hub", {})["profile_name"] = profile_path.stem

        return cls(**config)


def resolve_profile_path(profile: str, profiles_dir: Path = PROFILES_DIR) -> Path:
    candidate = Path(profile)
    if candidate.suffix in (".yaml", ".yml"):
        path = candidate
    else:
        path = profiles_dir / f"{profile}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in profiles_dir.glob("*.yaml")) if profiles_dir.exists() else []
        raise FileNotFoundError(f"Hub profile not found: {path} (available: {', '.join(available) or 'none'})")
    return path


def _substitute_env(text: str) -> str:
    def replace(match: re.Match) -> str:
        value = os.getenv(match.group(1))
        return value if value is not None else match.group(2)

    return _ENV_PATTERN.sub(replace, text)


def _load_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(_substitute_env(path.read_text(encoding="utf-8")))
    return data or {}


def deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge dicts; overlay wins. Lists are replaced, not merged."""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load_from_yaml()
    return _settings


def reload_settings(yaml_path: Optional[Path] = None, profile: Optional[str] = None) -> Settings:
    global _settings
    _settings = Settings.load_from_yaml(yaml_path, profile)
    return _settings
