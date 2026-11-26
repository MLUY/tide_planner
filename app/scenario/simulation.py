"""
Monte Carlo simulation of the planning with variance on the phase durations.

Every run draws, for every cycle and every phase, a duration from a triangular distribution
(minimum = nominal x (1 - minus %), most likely = nominal, maximum = nominal x (1 + plus %)) and
plans the nett planning and - when weather downtime is applied - the gross planning with the
same draws. Optionally the start moment is drawn as well (whole hours within +- N days).
The most likely (P50) run is the run closest to the median in both project duration and tidal delay.

Pure Python (no VIKTOR imports) so it can be tested on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .planning import Phase, PlanningError, plan_installation

METRICS = ("tidal", "restriction", "weather", "total", "duration", "tidal_pct")   # hours; tidal_pct in %
_EPOCH = datetime(2000, 1, 1)


@dataclass
class SimulationResult:
    runs: list                     # per run: start, cycle_hours, nett {...}, gross {...}
    basis: str                     # "gross" (weather applied) or "nett"
    n_runs: int
    seed: int
    p50_index: int
    p50_plan: object               # PlanResult of the P50 run (planning basis)
    stats: dict = field(default_factory=dict)   # stats[basis][metric] = dict(mean, p10, p50, p90, min, max)
    finish: dict = field(default_factory=dict)  # finish[basis] = dict(p10, p50, p90) datetimes
    p50_plans: dict = field(default_factory=dict)    # most likely planning per basis: nett / gross
    p50_indices: dict = field(default_factory=dict)


def draw_cycle_hours(rng: np.random.Generator, phases: list[Phase], minus_pct: list[float],
                     plus_pct: list[float], n_cycles: int) -> list[list[float]]:
    """Durations per cycle and phase from triangular distributions around the nominal duration."""
    out = np.empty((n_cycles, len(phases)))
    for k, (ph, m, p) in enumerate(zip(phases, minus_pct, plus_pct)):
        lo, mode, hi = ph.hours * (1 - m / 100), ph.hours, ph.hours * (1 + p / 100)
        out[:, k] = mode if hi - lo < 1e-12 else rng.triangular(lo, mode, hi, size=n_cycles)
    return np.round(out * 60) / 60       # whole minutes, like the rest of the planning


def _summary(res) -> dict:
    duration = (res.finish - res.start).total_seconds() / 3600
    return dict(tidal=res.total_tidal_wait_h, restriction=res.total_restriction_wait_h,
                weather=res.total_weather_h, total=res.total_wait_h, duration=duration,
                # tidal delay as % of the project duration without weather downtime, so nett and gross
                # are compared on the same (operational) time
                tidal_pct=(100 * res.total_tidal_wait_h / (duration - res.total_weather_h)
                           if duration - res.total_weather_h > 0 else 0.0),
                start=res.start, finish=res.finish)


def _stats(values: np.ndarray) -> dict:
    return dict(mean=float(values.mean()), p10=float(np.percentile(values, 10)),
                p50=float(np.percentile(values, 50)), p90=float(np.percentile(values, 90)),
                min=float(values.min()), max=float(values.max()))


def simulate(*, start: datetime, phases: list[Phase], minus_pct: list[float], plus_pct: list[float], tide,
             rule, n_cycles: int, restrictions=None, weather=None, n_runs: int = 300, seed: int = 1,
             start_days: int = 0, entry_rule=None) -> SimulationResult:
    """Run the planning n_runs times with drawn durations (and optionally a drawn start moment)."""
    if n_runs < 1:
        raise PlanningError("The number of simulation runs must be at least 1.")
    for ph, m, p in zip(phases, minus_pct, plus_pct):
        if m < 0 or p < 0:
            raise PlanningError(f"Phase '{ph.name}': the variance percentages cannot be negative.")
        if m >= 100:
            raise PlanningError(f"Phase '{ph.name}': the minus variance must be less than 100 %.")
    rng = np.random.default_rng(int(seed))
    runs = []
    for _ in range(n_runs):
        hours = draw_cycle_hours(rng, phases, minus_pct, plus_pct, n_cycles).tolist()
        shift_h = int(rng.integers(-24 * start_days, 24 * start_days + 1)) if start_days > 0 else 0
        run_start = start + timedelta(hours=shift_h)
        common = dict(start=run_start, phases=phases, tide=tide, rule=rule, n_cycles=n_cycles,
                      restrictions=restrictions, cycle_hours=hours, entry_rule=entry_rule)
        nett = plan_installation(**common)
        gross = plan_installation(**common, weather=weather) if weather is not None else nett
        runs.append(dict(start=run_start, cycle_hours=hours, nett=_summary(nett), gross=_summary(gross)))

    basis = "gross" if weather is not None else "nett"
    stats, finish = {}, {}
    for b in ("nett", "gross"):
        stats[b] = {m: _stats(np.array([r[b][m] for r in runs])) for m in METRICS}
        # finish dates as hours since a fixed naive reference (independent of the computer's time zone)
        ends = np.array([(r[b]["finish"] - _EPOCH).total_seconds() / 3600 for r in runs])
        finish[b] = {q: _EPOCH + timedelta(hours=float(np.percentile(ends, int(q[1:]))))
                     for q in ("p10", "p50", "p90")}
    # most likely planning per basis: the run closest to the median in project duration AND tidal delay
    # (each scaled by its spread), so the planning shown is typical for both and not a 'lucky' or 'unlucky' run
    p50_index, p50_plans = {}, {}
    for b in ("nett", "gross"):
        score = np.zeros(len(runs))
        for m in ("duration", "tidal"):
            v = np.array([r[b][m] for r in runs])
            spread = np.percentile(v, 75) - np.percentile(v, 25)
            score += np.abs(v - np.percentile(v, 50)) / (spread if spread > 1e-9 else 1.0)
        idx = int(np.argmin(score))
        r = runs[idx]
        p50_index[b] = idx
        p50_plans[b] = plan_installation(start=r["start"], phases=phases, tide=tide, rule=rule, n_cycles=n_cycles,
                                         restrictions=restrictions, cycle_hours=r["cycle_hours"],
                                         weather=weather if b == "gross" else None, entry_rule=entry_rule)
    return SimulationResult(runs=runs, basis=basis, n_runs=n_runs, seed=int(seed), p50_index=p50_index[basis],
                            p50_plan=p50_plans[basis], stats=stats, finish=finish,
                            p50_plans=p50_plans, p50_indices=p50_index)
