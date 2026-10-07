"""Small Windows-only helpers. Every function degrades to a harmless default off-Windows."""

from __future__ import annotations

import ctypes
import sys


def is_admin() -> bool:
    """True when the process runs elevated (required for MSR / kernel-driver sensor access)."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def pawnio_installed() -> bool | None:
    """Whether the PawnIO kernel driver service is registered (LibreHardwareMonitor >= 0.9.5 needs it).

    Returns ``None`` when the question cannot be answered (non-Windows).
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\PawnIO"):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def system_manufacturer() -> str | None:
    """BIOS-reported system manufacturer (e.g. "Acer"), or None when unknown / not Windows."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as key:
            value, _ = winreg.QueryValueEx(key, "SystemManufacturer")
            return str(value).strip() or None
    except OSError:
        return None
