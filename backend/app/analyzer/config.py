"""Tunable thresholds for the rules. The Settings page (packaging phase) overrides a subset of these."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Thresholds:
    # --- time windows -----------------------------------------------------------------------
    window_s: float = 180.0  # history kept for the rules (spec: 2-5 minutes)
    recent_s: float = 60.0  # "what is happening now" slice used by steady-state rules
    min_samples: int = 20  # below this the engine reports "warming up"
    eval_interval_s: float = 5.0
    confirm_s: float = 15.0  # a condition must hold this long before it is reported (no flicker)
    linger_s: float = 30.0  # ...and stays visible this long after it clears
    resolved_keep_s: float = 600.0

    # --- temperatures (deg C) -------------------------------------------------------------------
    cpu_idle_hot_c: float = 70.0  # package temp that is too warm for an idle machine
    cpu_hot_c: float = 85.0
    default_tjmax_c: float = 100.0
    near_limit_margin_c: float = 5.0  # "near the limit" = within this of TjMax / GPU slowdown temp
    safe_margin_c: float = 12.0  # "comfortably below the limit" (power-limit hypothesis)
    gpu_idle_hot_c: float = 65.0
    gpu_hot_c: float = 80.0
    default_gpu_slowdown_c: float = 91.0
    shared_cpu_hot_c: float = 80.0  # CPU+GPU heatsink saturation
    shared_gpu_hot_c: float = 72.0

    # --- load (%) -------------------------------------------------------------------------------
    low_load_cpu_pct: float = 25.0
    low_load_gpu_pct: float = 15.0
    high_load_pct: float = 60.0

    # --- process attribution --------------------------------------------------------------------
    bg_process_min_pct: float = 3.0  # of the WHOLE CPU (28 logical cores: 3% ~ 0.8 core)
    dominant_share: float = 0.5  # one process must account for this share of attributed CPU
    dominant_min_pct: float = 8.0
    gpu_dominant_share: float = 0.6
    gpu_dominant_min_pct: float = 30.0
    ignore_process_names: frozenset[str] = frozenset(
        {"system", "registry", "secure system", "thermalsense.exe", "thermalsense-backend.exe"}
    )

    # --- clocks / throttling ----------------------------------------------------------------------
    clock_drop_fraction: float = 0.10

    # --- spike at load start (rule 3) -------------------------------------------------------------
    idle_load_pct: float = 30.0
    idle_run_samples: int = 5
    spike_temp_c: float = 90.0
    spike_within_s: float = 30.0
    spike_min_rise_c: float = 25.0
    spike_min_rate_c_per_s: float = 1.5  # averaged over the first 10 s after load starts
    boost_power_w: float = 120.0  # above this a fast rise is partly just boost behaviour

    # --- fans (rule 4) --------------------------------------------------------------------------
    fan_full_ratio: float = 0.93  # rpm / highest rpm ever seen
    fan_full_fraction: float = 0.8  # of recent samples
    fan_learn_min_span: float = 0.30  # fan must have been seen varying by >= 30% before "max" means anything
    cooling_slope_c_per_s: float = -0.05  # temperature falling faster than this = it is recovering

