"""
Planning engine for a tide-restricted monopile installation campaign.

Pure Python (no VIKTOR imports) so it can be tested on its own.

Logic
-----
* The vessel repeats a cycle until all monopiles are installed. Each cycle row has a type:
    - "In port"       : loading etc. (alongside, no tidal restriction)
    - "Sail to site"  : starts with leaving the port  -> port GATE (departure)
    - "At site"       : installation etc.
    - "Sail to port"  : ends with entering the port   -> port GATE (entry)
* A gate is the moment the vessel passes the entrance channel. It is only allowed when the tide is
    - >= threshold_rising  (cm) while the tide is rising (flood), or
    - >= threshold_falling (cm) while the tide is falling (ebb).
  Optionally the condition must hold for a channel transit time after the gate moment.
* Departure: the vessel waits alongside (after loading) until the gate is open.
  Entry: the vessel waits outside the port (at anchor) until the gate is open, then the next
  cycle's port activities start.
* Tide between the data points is linearly interpolated; rising/falling is the sign of the slope.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

TYPE_PORT = "In port"
TYPE_OUT = "Sail to site"
TYPE_SITE = "At site"
TYPE_IN = "Sail to port"
PHASE_TYPES = [TYPE_PORT, TYPE_OUT, TYPE_SITE, TYPE_IN]
TYPE_ORDER = {t: i for i, t in enumerate(PHASE_TYPES)}

CATEGORY = {TYPE_PORT: "Port", TYPE_OUT: "Sailing", TYPE_SITE: "Installation", TYPE_IN: "Sailing"}
CAT_WAIT = "Tidal wait"

# Bokalift 2 default cycle (no tidal restriction)
DEFAULT_CYCLE = [
    dict(phase="Loading", type=TYPE_PORT, hours=12.0),
    dict(phase="Sail to site", type=TYPE_OUT, hours=13.0),
    dict(phase="Installation", type=TYPE_SITE, hours=52.0),
    dict(phase="Sail to loading port", type=TYPE_IN, hours=12.0),
]

TIME_STEP_H = 1 / 12     # 5 min resolution for the window search
MAX_WAIT_H = 24 * 14     # stop searching after two weeks


class PlanningError(Exception):
    """Input problem the user can fix."""


@dataclass
class Phase:
    name: str
    type: str
    hours: float


@dataclass
class GateRule:
    rising_cm: float
    falling_cm: float
    transit_h: float = 0.0   # condition must hold from gate moment until gate + transit_h


@dataclass
class PlanResult:
    segments: list
    gates: list
    delay_points: list
    cycles: int
    start: datetime
    finish: datetime
    total_wait_h: float
    nominal_cycle_h: float
    tide: object = None
    rule: GateRule = None
    cycle_stats: list = field(default_factory=list)


class TideCurve:
    def __init__(self, df: pd.DataFrame):
        self.t0 = df["time"].iloc[0].to_pydatetime()
        self.hours = ((df["time"] - df["time"].iloc[0]) / pd.Timedelta(hours=1)).to_numpy(float)
        self.cm = df["cm"].to_numpy(float)
        self._slope = np.diff(self.cm) / np.diff(self.hours)

    @property
    def end(self) -> datetime:
        return self.t0 + timedelta(hours=float(self.hours[-1]))

    def to_hours(self, ts: datetime) -> float:
        return (ts - self.t0) / timedelta(hours=1)

    def level_cm(self, hours):
        return np.interp(hours, self.hours, self.cm)

    def rising(self, hours):
        """True where the tide is rising (slope of the segment that starts at or before the time)."""
        idx = np.clip(np.searchsorted(self.hours, hours, side="right") - 1, 0, len(self._slope) - 1)
        return self._slope[idx] > 0

    def gate_open(self, hours, rule: GateRule):
        hours = np.asarray(hours, float)
        need = np.where(self.rising(hours), rule.rising_cm, rule.falling_cm)
        return self.level_cm(hours) >= need


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
    raw = df["time"].astype(str)
    df["time"] = pd.to_datetime(raw, errors="coerce", format="mixed", dayfirst="/" in raw.iloc[0])
    df["cm"] = pd.to_numeric(df["cm"], errors="coerce")
    df = df.dropna().drop_duplicates("time").sort_values("time").reset_index(drop=True)
    if len(df) < 2:
        raise PlanningError("Tidal data has fewer than 2 valid rows (check date format and numbers).")
    return TideCurve(df)


def _wait_for_gate(tide: TideCurve, t: datetime, rule: GateRule) -> float:
    """Smallest wait (h) from t until the gate is open (for the whole transit time, if any)."""
    h0 = tide.to_hours(t)
    if h0 < 0:
        raise PlanningError(f"Tidal data starts {tide.t0:%Y-%m-%d %H:%M}, after the planned start "
                            f"{t:%Y-%m-%d %H:%M}. Use a later start date or a tidal file that covers it.")
    n = max(int(math.ceil(rule.transit_h / TIME_STEP_H)) + 1, 1)
    offs = np.linspace(0.0, rule.transit_h, n) if rule.transit_h > 0 else np.array([0.0])
    w = 0.0
    while w <= MAX_WAIT_H:
        if h0 + w + offs[-1] > tide.hours[-1]:
            raise PlanningError(f"Tidal data ends {tide.end:%Y-%m-%d %H:%M} but the schedule runs beyond it. "
                                f"Use an earlier start date, fewer monopiles or a longer tidal data file.")
        if tide.gate_open(h0 + w + offs, rule).all():
            return w
        w += TIME_STEP_H
    raise PlanningError("No tidal window found within 14 days. Check the tide thresholds and the tidal data.")


def validate_phases(phases: list[Phase]) -> None:
    if not phases:
        raise PlanningError("The cycle needs at least one phase.")
    types = [p.type for p in phases]
    for p in phases:
        if p.type not in TYPE_ORDER:
            raise PlanningError(f"Phase '{p.name}' has no valid type.")
        if p.hours < 0:
            raise PlanningError(f"Phase '{p.name}' has a negative duration.")
    if types.count(TYPE_OUT) != 1 or types.count(TYPE_IN) != 1:
        raise PlanningError("The cycle needs exactly one 'Sail to site' and exactly one 'Sail to port' phase.")
    order = [TYPE_ORDER[t] for t in types]
    if order != sorted(order):
        raise PlanningError("Keep the phase order: 'In port' rows, 'Sail to site', 'At site' rows, 'Sail to port'.")


def plan_installation(*, n_mps: int, mps_per_cycle: int, start: datetime, phases: list[Phase],
                      tide: TideCurve, rule: GateRule) -> PlanResult:
    validate_phases(phases)
    cycles = int(math.ceil(n_mps / mps_per_cycle))
    t = start
    cum = 0.0
    segments, gates, points, stats = [], [], [(start, 0.0)], []

    def add(cycle, activity, category, dur, detail=""):
        nonlocal t
        fin = t + timedelta(hours=dur)
        segments.append(dict(cycle=cycle, activity=activity, category=category, detail=detail,
                             start=t, finish=fin, hours=dur))
        t = fin

    def gate(cycle, direction):
        nonlocal cum
        wait = _wait_for_gate(tide, t, rule)
        if wait > 1e-9:
            where = "alongside in port" if direction == "Departure" else "outside the port"
            points.append((t, cum))
            add(cycle, f"Tidal wait - {direction.lower()}", CAT_WAIT, wait,
                f"Waiting {where} for the tidal window")
            cum += wait
            points.append((t, cum))
        h = tide.to_hours(t)
        gates.append(dict(cycle=cycle, direction=direction, time=t, wait_h=wait,
                          level_cm=float(tide.level_cm(h)),
                          tide="rising" if bool(tide.rising(np.array([h]))[0]) else "falling"))
        return wait

    for cycle in range(1, cycles + 1):
        c_start, c_wait = t, 0.0
        for ph in phases:
            if ph.type == TYPE_OUT:
                c_wait += gate(cycle, "Departure")
            add(cycle, ph.name, CATEGORY[ph.type], ph.hours)
            if ph.type == TYPE_IN:
                c_wait += gate(cycle, "Entry")
        stats.append(dict(cycle=cycle, start=c_start, finish=t,
                          hours=(t - c_start).total_seconds() / 3600, wait_h=c_wait))

    points.append((t, cum))
    return PlanResult(segments=segments, gates=gates, delay_points=points, cycles=cycles, start=start,
                      finish=t, total_wait_h=cum, nominal_cycle_h=float(sum(p.hours for p in phases)),
                      tide=tide, rule=rule, cycle_stats=stats)
