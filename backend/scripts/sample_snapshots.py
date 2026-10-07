"""Poll /api/snapshot once a second and print one compact row per sample.

    python scripts/sample_snapshots.py --seconds 30
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def _f(v: object, spec: str = ".0f", none: str = "-") -> str:
    return none if v is None else format(v, spec)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--url", default="http://127.0.0.1:8765/api/snapshot")
    args = parser.parse_args()

    print("  t  load pkgT maxT  pkgW  PL1  PL2 Pclk | throttle(active/type/conf/source) reasons || GPU T/hot/mem  W/limit clk | fans")
    start = time.monotonic()
    for i in range(args.seconds):
        try:
            with urllib.request.urlopen(args.url, timeout=3) as r:
                s = json.load(r)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"{i:>3}  request failed: {exc}")
            time.sleep(1)
            continue
        cpu, th, gpu = s["cpu"], s["throttle"]["cpu"], s.get("gpu") or {}
        pclk = [c["clock_mhz"] for c in cpu["cores"] if c["kind"] == "P" and c["clock_mhz"] is not None]
        print(
            f"{i:>3} {_f(cpu['total_load_pct']):>5} {_f(cpu['package_temp_c']):>4} {_f(cpu['max_core_temp_c']):>4} "
            f"{_f(cpu['package_power_w'], '.1f'):>5} {_f(cpu['pl1_w']):>4} {_f(cpu['pl2_w']):>4} "
            f"{_f(sum(pclk) / len(pclk) if pclk else None):>5} | "
            f"{th['active']}/{th['type']}/{th['confidence']}/{th['source']} {th['reasons']} || "
            f"{_f(gpu.get('temp_c'))}/{_f(gpu.get('temp_hotspot_c'))}/{_f(gpu.get('temp_memory_c'))} "
            f"{_f(gpu.get('power_draw_w'), '.0f')}/{_f(gpu.get('power_limit_w'), '.0f')} "
            f"{_f(gpu.get('core_clock_mhz'))} {gpu.get('throttle_reasons')} | "
            f"{ {f['name']: f['rpm'] for f in s['fans']} or '-' }"
        )
        time.sleep(max(0.0, (i + 1) - (time.monotonic() - start)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
