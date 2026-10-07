# ThermalSense

A background desktop app that finds the **root cause** of high CPU/GPU temperatures. It shows live per-core
temperatures, clocks and power, detects throttling and its reason (thermal or power limit), and gives
evidence-based diagnoses with suggested fixes. Built with Next.js, FastAPI, Electron, NVML and LibreHardwareMonitor.

> **Honesty rule.** A value ThermalSense cannot read is shown as `n/a`, never `0`. A verdict read from a hardware
> flag is labelled **detected**; one derived from clocks/temperatures/power is labelled **inferred** and phrased as a
> hypothesis. Every diagnosis carries the raw numbers it is based on.

## Status

| Phase | What | State |
|---|---|---|
| 1 | Sensor collector (LibreHardwareMonitor, psutil), `GET /api/snapshot`, `WS /ws/live` | Done. Verified on real hardware (elevated, PawnIO). |
| 2 | NVIDIA via NVML, throttle detection (`throttle` object), Intel MSR flags, AMD/fallback inference | NVML verified live. **Intel MSR flags and Acer fan decoding are implemented and unit-tested but not yet verified on elevated hardware** (see below). |
| 3 | SQLite storage, 7-day pruning, `GET /api/history` with downsampling | Done and verified. |
| 4 | Root-cause engine (7 rules), `GET /api/diagnosis` | Done. Verified live non-elevated; full elevated check pending. |
| 5 | Next.js dashboard (Overview, Per-core, GPU, History, Diagnosis) | Done. Verified in a browser, including a phone-width layout. |
| 6 | Electron shell (tray, notifications, auto-start, elevated sidecar) | **Not implemented.** Only `electron/package.json` with Electron + electron-builder installed. |
| 7 | PyInstaller + electron-builder packaging, Settings page, log files | **Not implemented.** |

## Requirements

* Windows 10/11, Intel or AMD CPU, NVIDIA GPU (optional).
* Python 3.12 (the spec says 3.11; the code is 3.11-compatible) and Node.js 20+.
* **Administrator rights + the PawnIO driver** for CPU temperatures, power and MSR flags
  (`winget install namazso.PawnIO`). Without them those values are `n/a` and the UI says why.

## Run it (development)

```powershell
# backend (use an elevated PowerShell for CPU sensors)
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scripts\fetch_lhm.py      # downloads LibreHardwareMonitorLib + dependencies from NuGet
.venv\Scripts\python run.py                    # http://127.0.0.1:8765  (live JSON viewer at /)

# frontend
cd ..\frontend
npm install
npm run dev                                    # http://127.0.0.1:3000
npm run build                                  # static export -> frontend/out
```

## Tests and checks

```powershell
cd backend;  .venv\Scripts\python -m unittest discover -s tests -t .     # 193 tests, no hardware needed
cd frontend; npm run typecheck; npm run lint
```

Hardware checks (read-only; the first two need an elevated shell):

* `backend\scripts\verify_live.ps1` runs the MSR probe, the Acer firmware probe, then the real server under a short
  bounded CPU load (default 25 s) and writes `backend\logs\verify_live.txt`. Self-elevates (one UAC prompt).
* `backend\scripts\dump_sensors.py`, `probe_msr.py`, `probe_acer_wmi.py` print what the machine exposes.

## How it works

```
LibreHardwareMonitor ─┐                         ┌─ REST  /api/snapshot /api/history /api/diagnosis
NVML (pynvml) ────────┤  SensorHub (1 Hz) ──────┤─ WS    /ws/live
Intel MSRs (PawnIO) ──┤   ├─ Recorder → SQLite   └─ control /api/control/shutdown (token-gated)
Acer EC fans (WMI) ───┤   └─ DiagnosisEngine (rolling window, 7 rules, hysteresis)
process table ────────┘
```

* `backend/app/collectors` sensor sources, throttle decoding, per-process attribution.
* `backend/app/analyzer` the rules; each returns `{diagnosis, severity, confidence, evidence[], suggested_fix}`, and a
  rule that lacks inputs is listed under "checks that could not run" instead of silently passing.
* `backend/app/db` SQLite schema, recorder (batched writes), history queries.
* `frontend/` static-export dashboard; one WebSocket shared by all pages.

## Security and safety notes

* The API binds to `127.0.0.1` only and allows a fixed set of CORS origins.
* MSR access is **read-only**. The Acer firmware interface accepts a hard allowlist of read commands
  (`GetGamingSysInfo` only); no `Set*` method is reachable.
* The shutdown endpoint does not exist unless the desktop shell supplies a one-time token.
* The kernel driver (PawnIO, signed) is installed separately; ThermalSense ships no kernel code.

## Known limitations

* **Unverified on elevated hardware:** Intel throttle flags (`MSR 0x64F`/`0x1B1`), PL1/PL2 decoding and the Acer fan/EC
  decoding follow the Intel SDM and the Linux `acer-wmi` driver and are unit-tested, but have not been run elevated.
  Run `verify_live.ps1` to check them on your machine.
* Fan duty on laptops is **estimated** from RPM relative to the highest RPM ever seen, and only after the fan has been
  seen at clearly different speeds.
* GPU fan speed is not exposed by NVIDIA drivers on laptops (the embedded controller owns the fans).
* `npm audit` reports advisories for Next.js 14 self-hosted-server features and dev tooling. The app ships as a static
  export loaded from disk, so no Next.js server runs in production; they are not fixed because the stack pins Next 14.

## Dependencies added beyond the stack list

`websockets` (Uvicorn's WebSocket transport) and shadcn/ui's own dependency set (`class-variance-authority`, `clsx`,
`tailwind-merge`, `lucide-react`, `tailwindcss-animate`, Radix primitives). LibreHardwareMonitorLib is MPL-2.0 and is
fetched, not vendored.

## License

MIT, see `LICENSE`.
