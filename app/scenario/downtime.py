"""
Weather downtime from monthly workability percentages (Site Metocean Report, SME)
and the total project duration.

Nett planning  = cycles with tidal and time restriction waits, no weather downtime.
Gross planning = the same, but on arrival at site each cycle waits on weather for
                 (working hours of the cycle) x (100 / workability of that month - 1),
                 after which the tidal windows are planned on the later timeline (planning.py).
Nett duration  = number of cycles x nett duration per cycle (= duration of the nett planning).
Gross duration = duration of the gross planning. Without weather downtime gross = nett.
Pure Python (no VIKTOR imports) so it can be tested on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_KEYS = [m.lower() for m in MONTHS]

# Norms from the SME: name, MWS window (h), Jan..Dec workability (%), year (%)
DEFAULT_NORMS = [
    ("BL2 General", 6, [77.4, 80.4, 87.9, 91.7, 95.2, 95.8, 91.9, 87.1, 85.0, 81.5, 80.8, 75.8], 85.0),
    ("BL2 MP Installation Best", 10, [61.8, 69.0, 79.3, 84.7, 91.4, 93.1, 87.4, 83.3, 80.6, 73.9, 69.4, 60.5], 77.5),
    ("BL2 MP Installation Worst", 10, [57.8, 65.5, 76.6, 81.9, 88.7, 91.7, 84.7, 80.6, 79.2, 69.9, 66.7, 57.8], 74.3),
]

class DowntimeError(Exception):
    """Input problem the user can fix."""


@dataclass
class Norm:
    name: str
    mws_h: float
    monthly_pct: list[float]          # 12 values, Jan..Dec
    year_pct: float | None = None


@dataclass
class DurationResult:
    n_cycles: int
    nominal_cycle_h: float
    nett_cycle_h: float
    nett_total_h: float
    gross_cycle_h: float
    gross_total_h: float
    downtime_h: float                 # weather downtime in the gross planning
    start: datetime
    nett_finish: datetime
    gross_finish: datetime
    norm: Norm | None = None
    months: list = field(default_factory=list)   # per calendar month: month, pct, cycles, downtime_h

    @property
    def effective_pct(self) -> float:
        work = self.n_cycles * self.nominal_cycle_h
        return 100 * work / (work + self.downtime_h) if self.downtime_h else 100.0


def default_table_rows() -> list[dict]:
    rows = []
    for name, mws, monthly, year in DEFAULT_NORMS:
        row = dict(norm=name, mws_h=mws, year=year)
        row.update({k: v for k, v in zip(MONTH_KEYS, monthly)})
        rows.append(row)
    return rows


def validate_norm(norm: Norm) -> None:
    if len(norm.monthly_pct) != 12:
        raise DowntimeError(f"Norm '{norm.name}' needs 12 monthly percentages.")
    for m, p in zip(MONTHS, norm.monthly_pct):
        if p is None or not 0 < p <= 100:
            raise DowntimeError(f"Norm '{norm.name}': workability for {m} must be more than 0 and at most 100 %.")


def project_duration(nett, gross=None, norm: Norm | None = None) -> DurationResult:
    """nett / gross: planning results (planning.PlanResult) without / with weather downtime.
    Without a norm (or gross planning) the gross duration equals the nett duration."""
    def total(res):
        return (res.finish - res.start).total_seconds() / 3600
    if norm is None or gross is None:
        gross = nett
    months = {}
    for s in gross.segments:
        if s["category"] == "Weather downtime":
            key = datetime(s["start"].year, s["start"].month, 1)
            m = months.setdefault(key, dict(month=key, pct=s.get("workability_pct"), cycles=0, downtime_h=0.0))
            m["cycles"] += 1
            m["downtime_h"] += s["hours"]
    return DurationResult(
        n_cycles=nett.cycles, nominal_cycle_h=nett.nominal_cycle_h,
        nett_cycle_h=total(nett) / nett.cycles, nett_total_h=total(nett),
        gross_cycle_h=total(gross) / gross.cycles, gross_total_h=total(gross),
        downtime_h=gross.total_weather_h if gross is not nett else 0.0,
        start=nett.start, nett_finish=nett.finish, gross_finish=gross.finish,
        norm=norm if gross is not nett else None, months=[months[k] for k in sorted(months)])
