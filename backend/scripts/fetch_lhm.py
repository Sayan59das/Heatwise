"""Fetch LibreHardwareMonitorLib and its managed dependencies from NuGet into ``backend/libs``.

Standard library only. The DLLs are resolved for the .NET Framework 4.7.2 runtime, which is what
``pythonnet`` hosts by default on Windows, so no extra .NET install is needed.

Usage::

    python scripts/fetch_lhm.py            # latest tested version
    python scripts/fetch_lhm.py --version 0.9.6
    python scripts/fetch_lhm.py --force    # re-download everything
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Final

DEFAULT_VERSION: Final = "0.9.6"
ROOT_PACKAGE: Final = "LibreHardwareMonitorLib"
LIBS_DIR: Final = Path(__file__).resolve().parent.parent / "libs"
FLAT_URL: Final = "https://api.nuget.org/v3-flatcontainer"

# Highest preference first. Everything here loads on .NET Framework 4.7.2+.
LIB_TFMS: Final = (
    "net481", "net48", "net472", "net471", "net47", "net462", "net461", "net46",
    "net452", "net451", "net45", "netstandard2.0", "netstandard1.6", "netstandard1.3",
)
DEP_GROUPS: Final = (
    ".NETFramework4.8.1", ".NETFramework4.8", ".NETFramework4.7.2", ".NETFramework4.7.1",
    ".NETFramework4.7", ".NETFramework4.6.2", ".NETFramework4.6.1", ".NETFramework4.6",
    ".NETFramework4.5.2", ".NETFramework4.5.1", ".NETFramework4.5",
    ".NETStandard2.0", ".NETStandard1.6", ".NETStandard1.3", "",
)
# Packages whose functionality is built into .NET Framework itself; the NuGet copy is a
# platform stub that must not shadow the in-box assembly.
SKIP_PACKAGES: Final = {"system.management"}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "thermalsense-setup"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https host
        return resp.read()


def _lower_bound(version_range: str) -> str:
    """'1.2.3' | '[1.2.3,)' | '(,2.0)' -> the minimum version string."""
    m = re.search(r"\d+(?:\.\d+){1,3}(?:-[\w.]+)?", version_range)
    if not m:
        raise ValueError(f"cannot parse version range {version_range!r}")
    return m.group(0)


def _read_nupkg(pkg_id: str, version: str) -> zipfile.ZipFile:
    pid = pkg_id.lower()
    data = _get(f"{FLAT_URL}/{pid}/{version}/{pid}.{version}.nupkg")
    return zipfile.ZipFile(io.BytesIO(data))


def _dependencies(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    nuspec = next(n for n in z.namelist() if n.endswith(".nuspec"))
    root = ET.fromstring(z.read(nuspec))
    ns = {"n": root.tag.split("}")[0].strip("{")}
    groups = {
        (g.get("targetFramework") or ""): [
            (d.get("id") or "", _lower_bound(d.get("version") or "0.0"))
            for d in g.findall("n:dependency", ns)
        ]
        for g in root.findall(".//n:dependencies/n:group", ns)
    }
    for tfm in DEP_GROUPS:
        if tfm in groups:
            return groups[tfm]
    return []


def _pick_dll(z: zipfile.ZipFile, pkg_id: str) -> tuple[str, bytes] | None:
    """Choose the best managed DLL for the package, preferring runtime-specific builds."""
    names = {n.lower(): n for n in z.namelist()}
    stem = pkg_id.lower()
    for tfm in LIB_TFMS:
        for prefix in ("runtimes/win-x64/lib", "runtimes/win/lib", "lib"):
            folder = f"{prefix}/{tfm}/"
            matches = [n for low, n in names.items() if low.startswith(folder) and low.endswith(".dll")]
            if matches:
                # a package can ship several DLLs; keep the one named after the package if present
                best = next((m for m in matches if Path(m).stem.lower() == stem), matches[0])
                return Path(best).name, z.read(best)
    return None


def fetch(version: str, force: bool) -> None:
    LIBS_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, str]] = {}
    queue: list[tuple[str, str]] = [(ROOT_PACKAGE, version)]
    seen: set[str] = set()

    while queue:
        pkg_id, ver = queue.pop(0)
        key = pkg_id.lower()
        if key in seen:
            continue
        seen.add(key)
        if key in SKIP_PACKAGES:
            print(f"  skip   {pkg_id} (in-box on .NET Framework)")
            continue

        print(f"  fetch  {pkg_id} {ver}")
        z = _read_nupkg(pkg_id, ver)
        picked = _pick_dll(z, pkg_id)
        if picked is None:
            print(f"  warn   {pkg_id}: no compatible managed DLL found", file=sys.stderr)
        else:
            dll_name, blob = picked
            target = LIBS_DIR / dll_name
            if force or not target.exists():
                target.write_bytes(blob)
            manifest[dll_name] = {
                "package": pkg_id,
                "version": ver,
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        queue.extend(_dependencies(z))

    (LIBS_DIR / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n{len(manifest)} assemblies in {LIBS_DIR}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", default=DEFAULT_VERSION, help="LibreHardwareMonitorLib version")
    parser.add_argument("--force", action="store_true", help="overwrite existing DLLs")
    args = parser.parse_args()
    try:
        fetch(args.version, args.force)
    except OSError as exc:
        print(f"network or filesystem error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
