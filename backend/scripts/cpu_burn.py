"""Bounded all-core CPU load, for exercising throttle detection. Stops by itself.

    python scripts/cpu_burn.py --seconds 25
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time


def _spin(deadline: float) -> None:
    x = 0
    while time.monotonic() < deadline:
        for i in range(50_000):
            x = (x * 1103515245 + i) & 0xFFFFFFFF


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=25.0, help="how long to load the CPU (default 25)")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()

    seconds = max(1.0, min(args.seconds, 300.0))  # hard cap: this heats a laptop
    deadline = time.monotonic() + seconds
    procs = [mp.Process(target=_spin, args=(deadline,)) for _ in range(args.workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()


if __name__ == "__main__":
    mp.freeze_support()
    main()
