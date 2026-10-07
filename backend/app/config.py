"""Runtime settings. Defaults are safe for local use; every value can be overridden via env vars."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _bundle_root() -> Path:
    """Directory that holds ``libs/`` - the backend folder in dev, the PyInstaller bundle when frozen."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _default_data_dir() -> Path:
    override = os.environ.get("THERMALSENSE_DATA")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "ThermalSense" if base else Path.home() / ".thermalsense"


@dataclass(frozen=True)
class Settings:
    # The API only ever binds to loopback: sensor data must not be reachable from the LAN.
    host: str = field(default_factory=lambda: os.environ.get("THERMALSENSE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("THERMALSENSE_PORT", 8765))
    poll_interval_s: float = field(default_factory=lambda: max(0.25, _env_float("THERMALSENSE_POLL_S", 1.0)))
    libs_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("THERMALSENSE_LIBS", _bundle_root() / "libs"))
    )
    log_level: str = field(default_factory=lambda: os.environ.get("THERMALSENSE_LOG_LEVEL", "INFO"))
    data_dir: Path = field(default_factory=_default_data_dir)
    retention_days: int = field(default_factory=lambda: max(1, _env_int("THERMALSENSE_RETENTION_DAYS", 7)))
    flush_interval_s: float = field(default_factory=lambda: max(0.5, _env_float("THERMALSENSE_FLUSH_S", 5.0)))
    # Origins allowed to call the API from a browser (Next dev server; Electron file/app origins).
    cors_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "null",  # Electron loading the static export from file://
        "app://thermalsense",  # Electron's custom protocol serving the static export
    )
    # One-time secret handed to us by the desktop shell. Without it the control endpoints do not exist.
    control_token: str | None = field(default_factory=lambda: os.environ.get("THERMALSENSE_TOKEN") or None)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "thermalsense.db"


def get_settings() -> Settings:
    return Settings()
