"""
Planning engine for a tide-restricted monopile installation campaign.
Pure Python (no VIKTOR imports) so it can be tested on its own.

* The cycle is repeated n_cycles times from the start date/time.
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
* Tidal data of one year is repeated for every other year (same calendar date and time), so the
  planning never runs out of data. Needs at least one full year of data; a shorter file is used as is.
* Time restrictions (optional): daily periods (e.g. 22:00-06:00) in which the vessel may not
  sail in or out of the port. On top of the tidal window, the vessel waits (alongside for a
  departure, outside the port for an entry) until the restriction has ended.
  Waiting caused by the tide and by a time restriction is reported separately.
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
CAT_RESTRICT = "Time restriction wait"

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
class TimeRestriction:
    """Daily period [from_h, to_h) (hours of the day, 0-24) in which the vessel may not
    sail in or out of the port.
    from_h > to_h means the period runs past midnight (e.g. 22 -> 6)."""
    description: str
    from_h: float
    to_h: float

    @property
    def label(self) -> str:
        def hm(h):
            m = int(round(h * 60))
            return f"{m // 60:02d}:{m % 60:02d}"
        return f"{self.description or 'Time restriction'} ({hm(self.from_h)}-{hm(self.to_h)})"

    def blocked(self, hour_of_day):
        h = np.asarray(hour_of_day, float)
        if self.from_h < self.to_h:
            return (h >= self.from_h) & (h < self.to_h)
        return (h >= self.from_h) | (h < self.to_h)


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
    restrictions: list = field(default_factory=list)
    total_tidal_wait_h: float = 0.0
    total_restriction_wait_h: float = 0.0


class TideCurve:
    def __init__(self, df: pd.DataFrame, data_start: datetime | None = None, data_end: datetime | None = None):
        self.df = df
        # period covered by the original data file (differs from t0/end once the data is repeated)
        self.data_start = data_start or df["time"].iloc[0].to_pydatetime()
        self.data_end = data_end or df["time"].iloc[-1].to_pydatetime()
        self.t0 = df["time"].iloc[0].to_pydatetime()
        self.hours = ((df["time"] - df["time"].iloc[0]) / pd.Timedelta(hours=1)).to_numpy(float)
        self.cm = df["cm"].to_numpy(float)
        self._slope = np.diff(self.cm) / np.diff(self.hours)

    @property
    def end(self) -> datetime:
        return self.t0 + timedelta(hours=float(self.hours[-1]))

    @property
    def repeated(self) -> bool:
        return self.t0 < self.data_start or self.end > self.data_end

    def is_measured(self, ts: datetime) -> bool:
        return self.data_start <= ts <= self.data_end

    def can_repeat(self) -> bool:
        """At least one full year of data is needed to repeat it for other years."""
        return self.data_end >= self.data_start + pd.DateOffset(years=1) - timedelta(hours=1, minutes=1)

    def repeated_to_cover(self, start: datetime, end: datetime) -> "TideCurve":
        """New curve covering [start, end] by repeating the first year of the data for every year.
        The copies keep the calendar date and time (29 Feb falls back to 28 Feb)."""
        base = self.df[self.df["time"] < pd.Timestamp(self.data_start) + pd.DateOffset(years=1)]
        y0 = self.data_start.year
        k_min = min(start.year - y0 - 1, 0)
        k_max = max(end.year - y0 + 1, 0)
        parts = []
        for k in range(k_min, k_max + 1):
            part = base.copy()
            if k:
                part["time"] = part["time"] + pd.DateOffset(years=k)
            parts.append(part)
        df = pd.concat(parts).drop_duplicates("time").sort_values("time").reset_index(drop=True)
        # a non-leap data year has no 29 Feb: fill it in leap years with the tide of 28 Feb
        fill = []
        for year in range(df["time"].iloc[0].year, df["time"].iloc[-1].year + 1):
            if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
                feb29 = pd.Timestamp(year, 2, 29)
                if not ((df["time"] >= feb29) & (df["time"] < feb29 + pd.Timedelta(days=1))).any():
                    day = df[(df["time"] >= feb29 - pd.Timedelta(days=1)) & (df["time"] < feb29)].copy()
                    day["time"] = day["time"] + pd.Timedelta(days=1)
                    fill.append(day)
        if fill:
            df = pd.concat([df, *fill]).drop_duplicates("time").sort_values("time").reset_index(drop=True)
        df = df[(df["time"] >= pd.Timestamp(start) - pd.Timedelta(days=2))
                & (df["time"] <= pd.Timestamp(end) + pd.Timedelta(days=2))].reset_index(drop=True)
        return TideCurve(df, self.data_start, self.data_end)

    def to_hours(self, ts: datetime) -> float:
        return (ts - self.t0) / timedelta(hours=1)

    def hour_of_day(self, hours):
        t0 = self.t0.hour + self.t0.minute / 60 + self.t0.second / 3600
        return (t0 + np.asarray(hours, float)) % 24

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


def validate_restrictions(restrictions: list[TimeRestriction]) -> None:
    for r in restrictions:
        name = r.description or "Time restriction"
        for v in (r.from_h, r.to_h):
            if not 0 <= v <= 24:
                raise PlanningError(f"'{name}': times must be between 0 and 24 h.")
        if r.from_h % 24 == r.to_h % 24:
            raise PlanningError(f"'{name}': 'from' and 'to' are equal; the port would be closed all day "
                                f"or never. Remove the row or change the times.")


def _restricted(tide: TideCurve, hours, restrictions) -> np.ndarray:
    hours = np.asarray(hours, float)
    out = np.zeros(hours.shape, bool)
    if restrictions:
        hod = tide.hour_of_day(hours)
        for r in restrictions:
            out |= r.blocked(hod)
    return out


def _wait_for_gate(tide: TideCurve, t: datetime, rule: GateRule, restrictions=()) -> float:
    """Smallest wait (h) from t until the gate is open (for the whole transit time, if any)
    and, if restrictions are given, no time restriction applies during that period."""
    h0 = tide.to_hours(t)
    if rule.transit_h > 0:
        offs = np.linspace(0.0, rule.transit_h, max(int(math.ceil(rule.transit_h / TIME_STEP_H)) + 1, 2))
    else:
        offs = np.array([0.0])
    w = 0.0
    while w <= MAX_WAIT_H:
        if h0 + w + offs[-1] > tide.hours[-1]:
            raise _DataEnd()
        hrs = h0 + w + offs
        if tide.gate_open(hrs, rule).all() and not _restricted(tide, hrs, restrictions).any():
            return w
        w += TIME_STEP_H
    raise PlanningError("No tidal window found within 14 days. Check the tide thresholds, "
                        "the time restrictions and the tidal data.")


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


def _gate(tide, rule, cycle, direction, t, cum, segs, gates, points, restrictions=()):
    """Wait for the tidal window and for the end of any time restriction.
    The tide-only wait is booked as tidal wait, the extra wait as time restriction wait."""
    where = "alongside in port" if direction == "Departure" else "outside the port"
    tidal = _wait_for_gate(tide, t, rule)
    total = _wait_for_gate(tide, t, rule, restrictions) if restrictions else tidal
    restr = max(total - tidal, 0.0)
    if total > 1e-9:
        points.append((t, cum))
    if tidal > 1e-9:
        fin = t + timedelta(hours=tidal)
        segs.append(dict(cycle=cycle, activity=f"Tidal wait - {direction.lower()}", category=CAT_WAIT,
                         detail=f"Waiting {where} for the tidal window", start=t, finish=fin, hours=tidal))
        t, cum = fin, cum + tidal
        points.append((t, cum))
    if restr > 1e-9:
        fin = t + timedelta(hours=restr)
        active = [r.label for r in restrictions
                  if r.blocked(tide.hour_of_day(tide.to_hours(t)))] or [r.label for r in restrictions]
        segs.append(dict(cycle=cycle, activity=f"Time restriction wait - {direction.lower()}",
                         category=CAT_RESTRICT,
                         detail=f"Waiting {where}: " + "; ".join(active), start=t, finish=fin, hours=restr))
        t, cum = fin, cum + restr
        points.append((t, cum))
    h = tide.to_hours(t)
    gates.append(dict(cycle=cycle, direction=direction, time=t, wait_h=tidal + restr,
                      tidal_wait_h=tidal, restriction_wait_h=restr,
                      level_cm=float(tide.level_cm(h)),
                      tide="rising" if bool(tide.rising(np.array([h]))[0]) else "falling"))
    return t, cum


def plan_installation(*, start: datetime, phases: list[Phase], tide: TideCurve, rule: GateRule,
                      n_cycles: int, restrictions: list[TimeRestriction] | None = None) -> PlanResult:
    """Repeat the cycle n_cycles times from start.
    restrictions: daily periods in which the vessel may not sail in or out (departure and entry).
    With at least one year of tidal data, the data is repeated for other years as far as needed."""
    restrictions = list(restrictions or [])
    validate_phases(phases)
    validate_restrictions(restrictions)
    if n_cycles < 1:
        raise PlanningError("Number of cycles must be at least 1.")

    if not tide.can_repeat():
        if start < tide.t0:
            raise PlanningError(f"Tidal data starts {tide.t0:%Y-%m-%d %H:%M}, after the start "
                                f"{start:%Y-%m-%d %H:%M}. Use a later start or a tidal file that covers it "
                                f"(a file of at least one year is repeated for other years automatically).")
        return _plan(start, phases, tide, rule, n_cycles, restrictions)

    # first guess of the period needed; doubled when the schedule runs past it
    nominal = sum(p.hours for p in phases)
    horizon_h = max(n_cycles * nominal * 2, 24 * 60)
    for _ in range(12):
        curve = tide.repeated_to_cover(start, start + timedelta(hours=horizon_h + MAX_WAIT_H))
        try:
            return _plan(start, phases, curve, rule, n_cycles, restrictions, raise_data_end=True)
        except _DataEnd:
            horizon_h *= 2
    raise PlanningError("The planning is too long (more than 100 years); check the number of cycles.")


def _plan(start, phases, tide, rule, n_cycles, restrictions, raise_data_end=False) -> PlanResult:
    t, cum = start, 0.0
    segments, gates, points, stats = [], [], [(start, 0.0)], []

    cycle = 0
    while cycle < n_cycles:
        cycle += 1
        c_start, c_cum = t, cum
        c_segs, c_gates, c_points = [], [], []
        try:
            for ph in phases:
                if ph.type == TYPE_OUT:
                    t, cum = _gate(tide, rule, cycle, "Departure", t, cum, c_segs, c_gates, c_points,
                                   restrictions)
                fin = t + timedelta(hours=ph.hours)
                if tide.to_hours(fin) > tide.hours[-1]:
                    raise _DataEnd()
                c_segs.append(dict(cycle=cycle, activity=ph.name, category=CATEGORY[ph.type],
                                   detail="", start=t, finish=fin, hours=ph.hours))
                t = fin
                if ph.type == TYPE_IN:
                    t, cum = _gate(tide, rule, cycle, "Entry", t, cum, c_segs, c_gates, c_points,
                                   restrictions)
        except _DataEnd:
            if raise_data_end:
                raise
            raise PlanningError(f"Tidal data ends {tide.end:%Y-%m-%d %H:%M} during cycle {cycle}. "
                                f"Use fewer cycles, an earlier start or a longer tidal file "
                                f"(a file of at least one year is repeated for other years automatically).")
        segments += c_segs
        gates += c_gates
        points += c_points
        stats.append(dict(cycle=cycle, start=c_start, finish=t,
                          hours=(t - c_start).total_seconds() / 3600, wait_h=cum - c_cum,
                          tidal_wait_h=sum(g["tidal_wait_h"] for g in c_gates),
                          restriction_wait_h=sum(g["restriction_wait_h"] for g in c_gates)))

    points.append((t, cum))
    return PlanResult(segments=segments, gates=gates, delay_points=points, cycles=len(stats),
                      start=start, finish=t, total_wait_h=cum,
                      nominal_cycle_h=float(sum(p.hours for p in phases)),
                      tide=tide, rule=rule, cycle_stats=stats, restrictions=restrictions,
                      total_tidal_wait_h=sum(c["tidal_wait_h"] for c in stats),
                      total_restriction_wait_h=sum(c["restriction_wait_h"] for c in stats))
