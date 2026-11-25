"""
Weather downtime from monthly workability percentages (Site Metocean Report, SME).

Nett duration  = number of cycles x nett duration per cycle (planned cycle incl. tidal and
                 time restriction waits, without weather downtime).
Gross duration = calendar time needed to perform the nett duration when, in each calendar
                 month, only the workability percentage of the time can be worked.
                 Walking month by month from the start: in a month with workability w,
                 one nett hour takes 1 / w gross hours.
Pure Python (no VIKTOR imports) so it can be tested on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_KEYS = [m.lower() for m in MONTHS]

# Norms from the SME: name, MWS window (h), Jan..Dec workability (%), year (%)
DEFAULT_NORMS = [
    ("BL2 General", 6, [77.4, 80.4, 87.9, 91.7, 95.2, 95.8, 91.9, 87.1, 85.0, 81.5, 80.8, 75.8], 85.0),
    ("BL2 MP Installation Best", 10, [61.8, 69.0, 79.3, 84.7, 91.4, 93.1, 87.4, 83.3, 80.6, 73.9, 69.4, 60.5], 77.5),
    ("BL2 MP Installation Worst", 10, [57.8, 65.5, 76.6, 81.9, 88.7, 91.7, 84.7, 80.6, 79.2, 69.9, 66.7, 57.8], 74.3),
]

MAX_MONTHS = 12 * 20  # safety stop


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
    nett_cycle_h: float
    nominal_cycle_h: float
    nett_total_h: float
    gross_total_h: float
    start: datetime
    nett_finish: datetime
    gross_finish: datetime
    norm: Norm | None = None
    months: list = field(default_factory=list)   # per calendar month: start, gross_h, pct, nett_h, downtime_h

    @property
    def downtime_h(self) -> float:
        return self.gross_total_h - self.nett_total_h

    @property
    def effective_pct(self) -> float:
        return 100 * self.nett_total_h / self.gross_total_h if self.gross_total_h else 100.0


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


def _next_month(t: datetime) -> datetime:
    return datetime(t.year + (t.month == 12), t.month % 12 + 1, 1)


def project_duration(*, start: datetime, n_cycles: int, nett_cycle_h: float, nominal_cycle_h: float,
                     norm: Norm | None = None) -> DurationResult:
    """Nett and gross project duration. Without a norm, gross equals nett."""
    nett_total = n_cycles * nett_cycle_h
    nett_finish = start + timedelta(hours=nett_total)
    if norm is None:
        return DurationResult(n_cycles=n_cycles, nett_cycle_h=nett_cycle_h, nominal_cycle_h=nominal_cycle_h,
                              nett_total_h=nett_total, gross_total_h=nett_total, start=start,
                              nett_finish=nett_finish, gross_finish=nett_finish)
    validate_norm(norm)

    months, t, remaining = [], start, nett_total
    for _ in range(MAX_MONTHS):
        if remaining <= 1e-9:
            break
        w = norm.monthly_pct[t.month - 1] / 100
        month_end = _next_month(t)
        available = (month_end - t).total_seconds() / 3600
        if remaining <= available * w:
            gross = remaining / w
            nett = remaining
        else:
            gross = available
            nett = available * w
        months.append(dict(month=datetime(t.year, t.month, 1), start=t, gross_h=gross,
                           pct=norm.monthly_pct[t.month - 1], nett_h=nett, downtime_h=gross - nett))
        remaining -= nett
        t = t + timedelta(hours=gross)
    else:
        raise DowntimeError("Gross duration exceeds 20 years; check the workability percentages.")

    t = datetime.min + timedelta(minutes=round((t - datetime.min).total_seconds() / 60))  # whole minutes
    gross_total = (t - start).total_seconds() / 3600
    return DurationResult(n_cycles=n_cycles, nett_cycle_h=nett_cycle_h, nominal_cycle_h=nominal_cycle_h,
                          nett_total_h=nett_total, gross_total_h=gross_total, start=start,
                          nett_finish=nett_finish, gross_finish=t, norm=norm, months=months)
