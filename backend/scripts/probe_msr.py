"""Read the Intel throttle/power MSRs once (or repeatedly) and show them decoded. Read-only.

Needs an elevated shell and the PawnIO driver. Handy for checking what ThermalSense will see::

    .venv\\Scripts\\python.exe scripts\\probe_msr.py
    .venv\\Scripts\\python.exe scripts\\probe_msr.py --watch 30     # sample at 1 Hz for 30 s (try under load)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors.intel_msr import IntelMsrReader, MsrUnavailable  # noqa: E402
from app.collectors.throttle import decode_intel_throttle, decode_power_limits  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.winutil import is_admin, pawnio_installed  # noqa: E402


def _hex(v: int | None) -> str:
    return "unreadable" if v is None else f"0x{v:016x}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS", help="sample at 1 Hz for this long")
    args = parser.parse_args()

    print(f"admin={is_admin()}  pawnio_installed={pawnio_installed()}")
    reader = IntelMsrReader(get_settings().libs_dir)
    try:
        reader.start()
    except MsrUnavailable as exc:
        print(f"MSR access unavailable: {exc}", file=sys.stderr)
        return 1

    try:
        for i in range(max(1, args.watch)):
            s = reader.read()
            verdict = decode_intel_throttle(s.perf_limit_reasons, s.package_therm_status)
            pl1, pl2 = decode_power_limits(s.pkg_power_limit, s.power_unit)
            print(
                f"[{i:>3}] 0x64F={_hex(s.perf_limit_reasons)}  0x1B1={_hex(s.package_therm_status)}  "
                f"0x610={_hex(s.pkg_power_limit)}  0x606={_hex(s.power_unit)}"
            )
            print(f"      PL1={pl1} W  PL2={pl2} W  ->  active={verdict.active} type={verdict.type} "
                  f"source={verdict.source} reasons={verdict.reasons}")
            if args.watch:
                time.sleep(1.0)
    finally:
        reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
