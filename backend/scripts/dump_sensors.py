"""Print every sensor LibreHardwareMonitor can see. Useful for bug reports and for checking which
sensors your CPU/laptop actually exposes.

Run from the backend folder (elevated for full results)::

    .venv\\Scripts\\python.exe scripts\\dump_sensors.py
    .venv\\Scripts\\python.exe scripts\\dump_sensors.py --parsed   # also show what ThermalSense derives
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors.lhm import LhmSource, LhmUnavailable  # noqa: E402
from app.collectors.parsing import build_cpu_snapshot, build_fans  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.winutil import is_admin, pawnio_installed  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--parsed", action="store_true", help="also print the parsed CPU/fan snapshot")
    args = parser.parse_args()

    print(f"admin={is_admin()}  pawnio_installed={pawnio_installed()}")
    source = LhmSource(get_settings().libs_dir)
    try:
        source.start()
        source.read_raw()  # first pass primes load counters
        import time

        time.sleep(1.0)
        raw = source.read_raw()
    except LhmUnavailable as exc:
        print(f"LibreHardwareMonitor unavailable: {exc}", file=sys.stderr)
        return 1
    finally:
        source.close()

    last = ""
    for s in sorted(raw, key=lambda r: (r.hardware_type, r.hardware_name, r.sensor_type, r.identifier)):
        header = f"[{s.hardware_type}] {s.hardware_name}"
        if header != last:
            print(f"\n{header}")
            last = header
        value = "null" if s.value is None else f"{s.value:.2f}"
        print(f"  {s.sensor_type:<12} {s.name:<34} {value:>10}   {s.identifier}")

    print(f"\n{len(raw)} sensors, {sum(1 for s in raw if s.value is None)} reporting null")
    if args.parsed:
        print("\n--- parsed ---")
        print(build_cpu_snapshot(raw).model_dump_json(indent=2))
        print(build_fans.__name__, [f.model_dump() for f in build_fans(raw)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
