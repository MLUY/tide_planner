"""
Planning engine for a tide-restricted monopile installation campaign.
Pure Python (no VIKTOR imports) so it can be tested on its own.

* The cycle repeats from the start date/time until the tidal data runs out.
  Only complete cycles are kept.
* Cycle row types:
    "In port"      : loading etc. (no tidal restriction)
    "Sail to site" : starts with leaving the port  -> departure gate
    "At site"      : installation etc.
    "Sail to port" : ends with entering the port   -> entry gate
* A gate is open when the tide is >= rising_cm on a rising tide,
  or >= falling_cm on a falling tide (optionally for a transit time).
* Departure: the vessel waits alongside until the gate opens.
  Entry: the vessel waits outside the port until the gate opens.
* Tide is linearly interpolated between data points; rising/falling is the sign of the slope.
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

TIME_STEP_H = 1 / 12     # 5 min search step
MAX_WAIT_H = 24 * 14     # give up after two weeks


class PlanningError(Exception):
    """Input problem the user can fix."""


class _DataEnd(Exception):
    """Internal: schedule reached the end of the tidal data."""


@dataclass
class Phase:
    name: str
    type: str
    hours: float


@dataclass
class GateRule:
    rising_cm: float
    falling_cm: float
    transit_h: float = 0.0   # condition must hold from the gate moment until gate + transit_h


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
        """True where the tide is rising (slope of the data segment at that time)."""
        idx = np.clip(np.searchsorted(self.hours, hours, side="right") - 1, 0, len(self._slope) - 1)
        return self._slope[idx] > 0

    def gate_open(self, hours, rule: GateRule):
        hours = np.asarray(hours, float)
        need = np.where(self.rising(hours), rule.rising_cm, rule.falling_cm)
        return self.level_cm(hours) >= need


def load_tide(data: bytes) -> TideCurve:
    """csv: first column date-time, second column tide level in cm."""
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
    if rule.transit_h > 0:
        offs = np.linspace(0.0, rule.transit_h, max(int(math.ceil(rule.transit_h / TIME_STEP_H)) + 1, 2))
    else:
        offs = np.array([0.0])
    w = 0.0
    while w <= MAX_WAIT_H:
        if h0 + w + offs[-1] > tide.hours[-1]:
            raise _DataEnd()
        if tide.gate_open(h0 + w + offs, rule).all():
            return w
        w += TIME_STEP_H
    raise PlanningError("No tidal window found within 14 days. Check the tide thresholds and the tidal data.")


def validate_phases(phases: list[Phase]) -> None:
    if not phases:
        raise PlanningError("The cycle needs at least one phase.")
    for p in phases:
        if p.type not in TYPE_ORDER:
            raise PlanningError(f"Phase '{p.name}' has no valid type.")
        if p.hours < 0:
            raise PlanningError(f"Phase '{p.name}' has a negative duration.")
    types = [p.type for p in phases]
    if types.count(TYPE_OUT) != 1 or types.count(TYPE_IN) != 1:
        raise PlanningError("The cycle needs exactly one 'Sail to site' and one 'Sail to port' phase.")
    order = [TYPE_ORDER[t] for t in types]
    if order != sorted(order):
        raise PlanningError("Keep the order: 'In port' rows, 'Sail to site', 'At site' rows, 'Sail to port'.")
    if sum(p.hours for p in phases) <= 0:
        raise PlanningError("The total cycle duration must be more than 0 hours.")


def _gate(tide, rule, cycle, direction, t, cum, segs, gates, points):
    wait = _wait_for_gate(tide, t, rule)
    if wait > 1e-9:
        where = "alongside in port" if direction == "Departure" else "outside the port"
        fin = t + timedelta(hours=wait)
        points.append((t, cum))
        segs.append(dict(cycle=cycle, activity=f"Tidal wait - {direction.lower()}", category=CAT_WAIT,
                         detail=f"Waiting {where} for the tidal window", start=t, finish=fin, hours=wait))
        t, cum = fin, cum + wait
        points.append((t, cum))
    h = tide.to_hours(t)
    gates.append(dict(cycle=cycle, direction=direction, time=t, wait_h=wait,
                      level_cm=float(tide.level_cm(h)),
                      tide="rising" if bool(tide.rising(np.array([h]))[0]) else "falling"))
    return t, cum


def plan_installation(*, start: datetime, phases: list[Phase], tide: TideCurve, rule: GateRule) -> PlanResult:
    """Repeat the cycle from start until the tidal data ends; keep only complete cycles."""
    validate_phases(phases)
    if start < tide.t0:
        raise PlanningError(f"Tidal data starts {tide.t0:%Y-%m-%d %H:%M}, after the start "
                            f"{start:%Y-%m-%d %H:%M}. Use a later start or a tidal file that covers it.")

    t, cum = start, 0.0
    segments, gates, points, stats = [], [], [(start, 0.0)], []

    cycle = 0
    while True:
        cycle += 1
        c_start, c_cum = t, cum
        c_segs, c_gates, c_points = [], [], []
        try:
            for ph in phases:
                if ph.type == TYPE_OUT:
                    t, cum = _gate(tide, rule, cycle, "Departure", t, cum, c_segs, c_gates, c_points)
                fin = t + timedelta(hours=ph.hours)
                if tide.to_hours(fin) > tide.hours[-1]:
                    raise _DataEnd()
                c_segs.append(dict(cycle=cycle, activity=ph.name, category=CATEGORY[ph.type],
                                   detail="", start=t, finish=fin, hours=ph.hours))
                t = fin
                if ph.type == TYPE_IN:
                    t, cum = _gate(tide, rule, cycle, "Entry", t, cum, c_segs, c_gates, c_points)
        except _DataEnd:
            t, cum = c_start, c_cum      # drop the unfinished cycle
            break
        segments += c_segs
        gates += c_gates
        points += c_points
        stats.append(dict(cycle=cycle, start=c_start, finish=t,
                          hours=(t - c_start).total_seconds() / 3600, wait_h=cum - c_cum))

    if not stats:
        raise PlanningError(f"Not even one full cycle fits between the start and the end of the tidal data "
                            f"({tide.end:%Y-%m-%d %H:%M}). Use an earlier start or a longer tidal file.")

    points.append((t, cum))
    return PlanResult(segments=segments, gates=gates, delay_points=points, cycles=len(stats),
                      start=start, finish=t, total_wait_h=cum,
                      nominal_cycle_h=float(sum(p.hours for p in phases)),
                      tide=tide, rule=rule, cycle_stats=stats)
