"""Read the Acer embedded-controller fans/temperatures once a second and cross-check them. Read-only.

Needs an elevated shell on an Acer gaming laptop::

    .venv\\Scripts\\python.exe scripts\\probe_acer_wmi.py --seconds 5

The EC's CPU/GPU temperatures are printed next to LibreHardwareMonitor's and NVML's own readings.
If the decoding is right they track each other (the EC reads a different point, so expect a few
degrees of difference, not an exact match).
"""

from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors.acer_wmi import AcerGamingWmi, AcerWmiUnavailable, sensor_supported  # noqa: E402
from app.collectors.lhm import LhmCollector  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.winutil import is_admin, system_manufacturer  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=int, default=5)
    args = parser.parse_args()

    libs = get_settings().libs_dir
    print(f"admin={is_admin()}  manufacturer={system_manufacturer()}")
    acer = AcerGamingWmi(libs)
    try:
        acer.start()
    except AcerWmiUnavailable as exc:
        print(f"Acer WMI unavailable: {exc}", file=sys.stderr)
        return 1
    ids = {0x01: "CPU temp", 0x02: "CPU fan", 0x03: "ext temp 2", 0x06: "GPU fan", 0x0A: "GPU temp"}
    print(f"supported mask = 0x{acer.supported_mask:04x} ->",
          ", ".join(f"{n}={'yes' if sensor_supported(acer.supported_mask, i) else 'no'}" for i, n in ids.items()))

    lhm = LhmCollector(libs)
    lhm.start()
    try:
        import pynvml  # noqa: PLC0415

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    except Exception:  # noqa: BLE001
        pynvml, handle = None, None

    try:
        for i in range(args.seconds):
            fans = {f.name: f.rpm for f in acer.read()}
            temps = acer.read_temps()
            cpu = lhm.read().cpu
            gpu_t = pynvml.nvmlDeviceGetTemperature(handle, 0) if pynvml and handle else None
            print(
                f"[{i}] fans(RPM) {fans}  | EC CPU={temps.get('EC CPU temp')} vs LHM pkg={cpu.package_temp_c} "
                f"max-core={cpu.max_core_temp_c}  | EC GPU={temps.get('EC GPU temp')} vs NVML={gpu_t}  "
                f"| EC ext2={temps.get('EC external temp 2')}"
            )
            time.sleep(1)
    finally:
        acer.close()
        lhm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
