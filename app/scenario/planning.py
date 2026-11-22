"""
Planning engine for a tide-restricted monopile installation cycle.

Pure Python (no VIKTOR imports) so it can be tested on its own.

Logic
-----
* The vessel repeats a fixed cycle of six phases until all monopiles are installed:
  port entry/loading/departure > channel outbound > to site > installation > to channel > channel inbound.
* Only the two channel transits depend on the tide. Before each one the vessel waits until the tide stays
  at or above the required level for the whole transit. The vessel can berth in port at any tide.
* Required tide (cm) = (required depth + keel clearance - channel level) * 100.
* Tide between the hourly points is linearly interpolated.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# The six phases of one installation cycle (fixed order). tidal=True means the phase passes the access channel.
PHASES = [
    dict(key="port", name="Port entry / loading / departure", category="Port", tidal=False),
    dict(key="ch_out", name="Sailing through tidal channel - outbound", category="Channel transit", tidal=True),
    dict(key="to_site", name="Sailing to site", category="Sailing", tidal=False),
    dict(key="install", name="Installation of monopiles", category="Installation", tidal=False),
    dict(key="to_ch", name="Sailing to tidal channel", category="Sailing", tidal=False),
    dict(key="ch_in", name="Sailing through tidal channel - inbound", category="Channel transit", tidal=True),
]
DEFAULT_HOURS = [8.0, 2.0, 4.0, 8.0, 4.0, 2.0]
CAT_WAIT = "Tidal wait"

TIME_STEP_H = 1 / 12     # 5 min resolution for the window search
MAX_WAIT_H = 24 * 14     # stop searching after two weeks


class PlanningError(Exception):
    """Input problem the user can fix."""


@dataclass
class PlanResult:
    segments: list
    delay_points: list
    cycles: int
    start: datetime
    finish: datetime
    total_wait_h: float
    nominal_cycle_h: float
    tide: object = None
    required_cm: float = 0.0


class TideCurve:
    def __init__(self, df: pd.DataFrame):
        self.t0 = df["time"].iloc[0].to_pydatetime()
        self.hours = ((df["time"] - df["time"].iloc[0]) / pd.Timedelta(hours=1)).to_numpy(float)
        self.cm = df["cm"].to_numpy(float)

    @property
    def end(self) -> datetime:
        return self.t0 + timedelta(hours=float(self.hours[-1]))

    def to_hours(self, ts: datetime) -> float:
        return (ts - self.t0) / timedelta(hours=1)

    def level_cm(self, hours):
        return np.interp(hours, self.hours, self.cm)


def load_tide(data: bytes) -> TideCurve:
    """Read csv: first column date-time, second column tide level in cm."""
    try:
        df = pd.read_csv(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise PlanningError(f"Tidal data file could not be read as csv: {exc}")
    if df.shape[1] < 2:
        raise PlanningError("Tidal data needs two columns: date-time and tide level (cm).")
    df = df.iloc[:, :2].copy()
    df.columns = ["time", "cm"]
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df["cm"] = pd.to_numeric(df["cm"], errors="coerce")
    df = df.dropna().drop_duplicates("time").sort_values("time").reset_index(drop=True)
    if len(df) < 2:
        raise PlanningError("Tidal data has fewer than 2 valid rows (check date format and numbers).")
    return TideCurve(df)


def _wait_for_window(tide: TideCurve, t: datetime, off_a: float, off_b: float, required_cm: float) -> float:
    """Smallest wait (h) so that tide >= required_cm during [t+w+off_a, t+w+off_b]."""
    if off_b - off_a <= 1e-9:
        return 0.0
    h0 = tide.to_hours(t)
    if h0 < 0:
        raise PlanningError(f"Tidal data starts {tide.t0:%Y-%m-%d %H:%M}, after the planned start "
                            f"{t:%Y-%m-%d %H:%M}. Use a later start date or a tidal file that covers it.")
    offs = np.linspace(off_a, off_b, max(int(math.ceil((off_b - off_a) / TIME_STEP_H)) + 1, 2))
    w = 0.0
    while w <= MAX_WAIT_H:
        if h0 + w + off_b > tide.hours[-1]:
            raise PlanningError(f"Tidal data ends {tide.end:%Y-%m-%d %H:%M} but the schedule runs beyond it. "
                                f"Use an earlier start date, fewer monopiles or a longer tidal data file.")
        if tide.level_cm(h0 + w + offs).min() >= required_cm:
            return w
        w += TIME_STEP_H
    raise PlanningError("No tidal window long enough for the channel transit within 14 days. "
                        "Check required depth, channel level and sailing times.")


def plan_installation(*, n_mps: int, mps_per_cycle: int, start: datetime, hours: list,
                      tide: TideCurve, required_tide_cm: float) -> PlanResult:
    """hours: durations of the six PHASES, in order. Cycle repeats until all monopiles are installed."""
    if len(hours) != len(PHASES):
        raise PlanningError(f"Expected {len(PHASES)} phase durations, got {len(hours)}.")
    cycles = int(math.ceil(n_mps / mps_per_cycle))
    t = start
    cum = 0.0
    segments, points = [], [(start, 0.0)]

    def add(cycle, activity, category, dur, detail=""):
        nonlocal t
        fin = t + timedelta(hours=dur)
        segments.append(dict(cycle=cycle, activity=activity, category=category, detail=detail,
                             start=t, finish=fin, hours=dur))
        t = fin

    for cycle in range(1, cycles + 1):
        for ph, dur in zip(PHASES, hours):
            if ph["tidal"] and dur > 0:
                wait = _wait_for_window(tide, t, 0.0, dur, required_tide_cm)
                if wait > 1e-9:
                    where = "outbound" if ph["key"] == "ch_out" else "inbound"
                    t_begin = t
                    add(cycle, f"Tidal wait - {where}", CAT_WAIT, wait,
                        f"Waiting for tide >= {required_tide_cm:.0f} cm before entering the channel")
                    points.append((t_begin, cum))
                    cum += wait
                    points.append((t, cum))
            add(cycle, ph["name"], ph["category"], dur)

    points.append((t, cum))
    return PlanResult(segments=segments, delay_points=points, cycles=cycles, start=start, finish=t,
                      total_wait_h=cum, nominal_cycle_h=float(sum(hours)),
                      tide=tide, required_cm=required_tide_cm)
