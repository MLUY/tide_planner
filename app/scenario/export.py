"""
Excel export of a planning result (pure Python + openpyxl, no VIKTOR imports).

Sheets
  Summary           inputs and key results
  Activities        every activity and wait, from - to, with the type of delay
  Cycles            one row per cycle (same as the Cycle table view)
  Departures-Entries every port gate with tide level and the waits split by cause
  Tide data         tidal data over the planning period with window / restriction status
  Weather downtime  (only when monthly workability is applied) nett / gross per calendar month
"""
from __future__ import annotations

import io
from datetime import timedelta

import numpy as np
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .downtime import MONTHS
from .planning import CAT_RESTRICT, CAT_WAIT, CAT_WEATHER, PlanResult

DT = "yyyy-mm-dd hh:mm"
HOURS = "0.00"
HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")
TITLE_FONT = Font(bold=True, size=14)
SECTION_FONT = Font(bold=True, size=11, color="1F4E78")
# light versions of the chart colours
FILLS = {"Port": "E8D9D5", "Sailing": "D2E3F1", "Installation": "D5EDD5",
         CAT_WAIT: "F7D0D0", CAT_RESTRICT: "F9DDEF", CAT_WEATHER: "D1F2F5"}
DELAY_TYPE = {CAT_WAIT: "Tidal", CAT_RESTRICT: "Time restriction", CAT_WEATHER: "Weather"}


def _h(x) -> float:
    """hours, rounded to avoid floating point noise like 3.8333333333"""
    return round(float(x), 2)


def _cm(x) -> float:
    return round(float(x), 0)


def _header(ws, row: int, names: list[str]) -> None:
    for col, name in enumerate(names, 1):
        c = ws.cell(row=row, column=col, value=name)
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[row].height = 45


def _table(ws, names: list[str], rows: list[list], formats: dict[int, str] | None = None,
           widths: list[int] | None = None, fill_col: int | None = None) -> None:
    """Header in row 1, data below, frozen header, autofilter, number formats per column."""
    _header(ws, 1, names)
    formats = formats or {}
    for r, values in enumerate(rows, 2):
        fill = None
        if fill_col is not None and values[fill_col] in FILLS:
            fill = PatternFill("solid", fgColor=FILLS[values[fill_col]])
        for col, v in enumerate(values, 1):
            c = ws.cell(row=r, column=col, value=v)
            if col - 1 in formats:
                c.number_format = formats[col - 1]
            if fill:
                c.fill = fill
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(names))}{max(len(rows) + 1, 2)}"
    for i, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _summary(ws, res: PlanResult, inputs: dict, dur=None) -> None:
    ws["A1"] = "Monopile installation - tidal planning"
    ws["A1"].font = TITLE_FONT
    row = 3

    def section(title):
        nonlocal row
        ws.cell(row=row, column=1, value=title).font = SECTION_FONT
        row += 1

    def line(label, value, fmt=None, unit=""):
        nonlocal row
        ws.cell(row=row, column=1, value=label)
        c = ws.cell(row=row, column=2, value=value)
        if fmt:
            c.number_format = fmt
        if unit:
            ws.cell(row=row, column=3, value=unit)
        row += 1

    section("Results - " + ("gross planning (incl. weather)" if res.weather else "nett planning (excl. weather)"))
    avg = sum(c["hours"] for c in res.cycle_stats) / len(res.cycle_stats)
    line("Start", res.start, DT)
    line("Finish", res.finish, DT)
    line("Duration", round((res.finish - res.start).total_seconds() / 86400, 2), "0.0", "days")
    line("Number of cycles", res.cycles)
    line("Nominal cycle (no delay)", res.nominal_cycle_h, "0.0", "h")
    line("Average cycle", _h(avg), "0.0", "h")
    line("Total delay", _h(res.total_wait_h), "0.0", "h")
    line("  of which tidal", _h(res.total_tidal_wait_h), "0.0", "h")
    line("  of which time restriction", _h(res.total_restriction_wait_h), "0.0", "h")
    line("  of which weather downtime", _h(res.total_weather_h), "0.0", "h")
    row += 1

    if dur is not None:
        section("Total project duration")
        n, g = dur.nett_plan, dur.gross_plan
        for col, title in ((2, "Nett planning"), (3, "Gross planning")):
            c = ws.cell(row=row, column=col, value=title)
            c.font, c.fill = HEADER_FONT, HEADER_FILL
        row += 1

        def line2(label, nett, gross, fmt=None, unit="", bold=False):
            nonlocal row
            ws.cell(row=row, column=1, value=label).font = Font(bold=bold)
            for col, v in ((2, nett), (3, gross)):
                c = ws.cell(row=row, column=col, value=v)
                c.font = Font(bold=bold)
                c.alignment = Alignment(horizontal="left")
                if fmt:
                    c.number_format = fmt
            if unit:
                ws.cell(row=row, column=4, value=unit)
            row += 1

        line2("Number of cycles", dur.n_cycles, dur.n_cycles)
        line2("Nominal cycle (no delays)", _h(dur.nominal_cycle_h), _h(dur.nominal_cycle_h), "0.0", "h")
        line2("Tidal delay (total)", _h(n.total_tidal_wait_h), _h(g.total_tidal_wait_h), "0.0", "h")
        line2("Time restriction delay (total)", _h(n.total_restriction_wait_h), _h(g.total_restriction_wait_h),
              "0.0", "h")
        line2("Weather downtime (total)", _h(n.total_weather_h), _h(g.total_weather_h), "0.0", "h")
        line2("Total delay", _h(n.total_wait_h), _h(g.total_wait_h), "0.0", "h", bold=True)
        line2("Cycle (nominal + total delay / cycles)", _h(dur.nett_cycle_h), _h(dur.gross_cycle_h), "0.0", "h")
        line2("Total duration", _h(dur.nett_total_h), _h(dur.gross_total_h), "0.0", "h", bold=True)
        line2("Total duration", round(dur.nett_total_h / 24, 2), round(dur.gross_total_h / 24, 2), "0.0", "days",
              bold=True)
        line2("Finish", dur.nett_finish, dur.gross_finish, DT)
        if dur.norm:
            ws.cell(row=row, column=1, value="Workability per month (%)")
            row += 1
            for m, pct in zip(MONTHS, dur.norm.monthly_pct):
                line(f"  {m}", pct, "0.0", "%")
            if dur.norm.year_pct is not None:
                line("  Year (SME)", dur.norm.year_pct, "0.0", "%")
        row += 1

    section("Inputs")
    for label, value, unit in inputs.get("general", []):
        line(label, value, DT if hasattr(value, "hour") else None, unit)
    line("Required tide - rising", _cm(res.rule.rising_cm), "0", "cm")
    line("Required tide - falling", _cm(res.rule.falling_cm), "0", "cm")
    line("Channel transit time", res.rule.transit_h, "0.00", "h")
    row += 1

    section("Cycle phases")
    for name, typ, hours in inputs.get("phases", []):
        line(f"{name} ({typ})", hours, "0.0", "h")
    row += 1

    section("Time restrictions (no sailing in/out of port)")
    if res.restrictions:
        for r in res.restrictions:
            line(r.description or "Time restriction", f"{_hm(r.from_h)} - {_hm(r.to_h)}", None, "daily")
    else:
        line("None", "")
    row += 1

    section("Tidal data")
    line("Source", inputs.get("tide_source", ""))
    line("Data from", res.tide.data_start, DT)
    line("Data until", res.tide.data_end, DT)
    line("Repeated for other years", "Yes" if res.tide.can_repeat() else "No (less than one year of data)")
    outside = not (res.tide.is_measured(res.start) and res.tide.is_measured(res.finish))
    line("Planning uses repeated data", "Yes" if (outside and res.tide.can_repeat()) else "No")

    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["D"].width = 8
    for r in ws.iter_rows(min_col=2, max_col=2):
        r[0].alignment = Alignment(horizontal="left")


def _hm(h: float) -> str:
    m = int(round(h * 60))
    return f"{m // 60:02d}:{m % 60:02d}"


def _activities(ws, res: PlanResult) -> None:
    rows = []
    for i, s in enumerate(res.segments, 1):
        h0, h1 = res.tide.to_hours(s["start"]), res.tide.to_hours(s["finish"])
        rows.append([i, s["cycle"], s["activity"], s["category"], DELAY_TYPE.get(s["category"], "No delay"),
                     s["start"], s["finish"], _h(s["hours"]),
                     _cm(res.tide.level_cm(h0)), _cm(res.tide.level_cm(h1)), s["detail"]])
    _table(ws, ["No.", "Cycle", "Activity", "Category", "Delay type", "From", "To", "Duration (h)",
                "Tide at start (cm)", "Tide at end (cm)", "Detail"],
           rows, formats={5: DT, 6: DT, 7: HOURS, 8: "0", 9: "0"},
           widths=[6, 7, 30, 20, 17, 17, 17, 12, 11, 11, 60], fill_col=3)


def _cycles(ws, res: PlanResult) -> None:
    dep = {g["cycle"]: g for g in res.gates if g["direction"] == "Departure"}
    ent = {g["cycle"]: g for g in res.gates if g["direction"] == "Entry"}
    rows = [[c["cycle"], c["start"], dep[c["cycle"]]["time"], ent[c["cycle"]]["time"], c["finish"],
             _h(c["hours"]), _h(c["hours"] - c["wait_h"]), _h(c["tidal_wait_h"]),
             _h(c["restriction_wait_h"]), _h(c["weather_h"]), _h(c["wait_h"])]
            for c in res.cycle_stats]
    _table(ws, ["Cycle", "Start", "Departure", "Entry", "End", "Cycle (h)", "Working (h)",
                "Tidal delay (h)", "Time restriction delay (h)", "Weather downtime (h)", "Total delay (h)"],
           rows, formats={1: DT, 2: DT, 3: DT, 4: DT, 5: HOURS, 6: HOURS, 7: HOURS, 8: HOURS, 9: HOURS, 10: HOURS},
           widths=[7, 17, 17, 17, 17, 10, 11, 11, 14, 12, 11])
    n = len(rows) + 1
    tot = n + 1
    ws.cell(row=tot, column=1, value="Total").font = Font(bold=True)
    for col in range(6, 12):
        L = get_column_letter(col)
        c = ws.cell(row=tot, column=col, value=f"=SUM({L}2:{L}{n})")
        c.number_format, c.font = HOURS, Font(bold=True)
    ws.cell(row=tot + 1, column=1, value="Average").font = Font(bold=True)
    for col in range(6, 12):
        L = get_column_letter(col)
        c = ws.cell(row=tot + 1, column=col, value=f"=AVERAGE({L}2:{L}{n})")
        c.number_format, c.font = HOURS, Font(bold=True)


def _gates(ws, res: PlanResult) -> None:
    rows = []
    for g in res.gates:
        requested = g["time"] - timedelta(hours=g["wait_h"])
        rows.append([g["cycle"], g["direction"], requested, g["time"], _cm(g["level_cm"]), g["tide"].capitalize(),
                     _h(g["tidal_wait_h"]), _h(g["restriction_wait_h"]), _h(g["wait_h"]),
                     "Alongside in port" if g["direction"] == "Departure" else "Outside the port"])
    _table(ws, ["Cycle", "Direction", "Ready at", "Sailed / entered at", "Tide (cm)", "Tide", "Tidal wait (h)",
                "Time restriction wait (h)", "Total wait (h)", "Waiting location"],
           rows, formats={2: DT, 3: DT, 4: "0", 6: HOURS, 7: HOURS, 8: HOURS},
           widths=[7, 11, 17, 17, 9, 9, 10, 14, 10, 18])


def _tide(ws, res: PlanResult) -> None:
    tide, rule = res.tide, res.rule
    h0 = max(tide.to_hours(res.start) - 24, 0.0)
    h1 = min(tide.to_hours(res.finish) + 24, float(tide.hours[-1]))
    hrs = tide.hours[(tide.hours >= h0) & (tide.hours <= h1)]
    rising = tide.rising(hrs)
    is_open = tide.gate_open(hrs, rule)
    hod = tide.hour_of_day(hrs)
    blocked = np.zeros(hrs.shape, bool)
    for r in res.restrictions:
        blocked |= r.blocked(hod)
    # what the vessel is doing at each moment
    seg_starts = np.array([tide.to_hours(s["start"]) for s in res.segments])
    rows = []
    for h, lv, up, op, bl in zip(hrs, tide.level_cm(hrs), rising, is_open, blocked):
        idx = np.searchsorted(seg_starts, h, side="right") - 1
        act = ""
        if 0 <= idx < len(res.segments) and h < tide.to_hours(res.segments[idx]["finish"]):
            act = f"C{res.segments[idx]['cycle']} {res.segments[idx]['activity']}"
        ts = tide.t0 + timedelta(hours=float(h))
        rows.append([ts, _cm(lv), "Rising" if up else "Falling",
                     _cm(rule.rising_cm if up else rule.falling_cm), "Yes" if op else "No",
                     "Yes" if bl else "No", "Yes" if (op and not bl) else "No", act,
                     "Data file" if tide.is_measured(ts) else f"Repeated from {tide.data_start.year}"])
    _table(ws, ["Date-time", "Tide (cm)", "Tide", "Required (cm)", "Tidal window open",
                "Time restriction active", "Sailing allowed", "Vessel activity", "Tidal data"],
           rows, formats={0: DT, 1: "0", 3: "0"}, widths=[17, 9, 9, 10, 10, 12, 10, 34, 18])
    green, red = PatternFill("solid", fgColor="D5EDD5"), PatternFill("solid", fgColor="F7D0D0")
    for r in range(2, len(rows) + 2):
        c = ws.cell(row=r, column=7)
        c.fill = green if c.value == "Yes" else red


def _downtime(ws, dur) -> None:
    """Weather downtime of the gross planning per calendar month (month of arrival at site)."""
    rows = [[m["month"], m["pct"], m["cycles"], _h(m["downtime_h"])] for m in dur.months]
    _table(ws, ["Month", "Workability (%)", "Cycles arriving at site", "Weather downtime (h)"],
           rows, formats={0: "mmm yyyy", 1: "0.0", 3: HOURS}, widths=[11, 13, 14, 14])
    n = len(rows) + 1
    ws.cell(row=n + 1, column=1, value="Total").font = Font(bold=True)
    for col in (3, 4):
        L = get_column_letter(col)
        c = ws.cell(row=n + 1, column=col, value=f"=SUM({L}2:{L}{n})")
        c.number_format, c.font = HOURS, Font(bold=True)


def build_excel(res: PlanResult, inputs: dict | None = None, duration=None) -> bytes:
    """inputs: {'general': [(label, value, unit)], 'phases': [(name, type, hours)], 'tide_source': str}"""
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    _summary(ws, res, inputs or {}, duration)
    _activities(wb.create_sheet("Activities"), res)
    _cycles(wb.create_sheet("Cycles"), res)
    _gates(wb.create_sheet("Departures-Entries"), res)
    _tide(wb.create_sheet("Tide data"), res)
    if duration is not None and duration.norm is not None:
        _downtime(wb.create_sheet("Weather downtime"), duration)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_simulation_excel(sim, inputs: dict | None = None, cycle_rows=None, note: str = "") -> bytes:
    """Simulation workbook: settings and P10/P50/P90 statistics, every run, and the P50 planning."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Simulation summary"
    ws["A1"] = "Duration variance simulation (Monte Carlo)"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = note
    row = 4
    ws.cell(row=row, column=1, value="Phase variance (triangular: nominal - x % ... nominal + y %)").font = SECTION_FONT
    row += 1
    _header(ws, row, ["Phase", "Type", "Nominal (h)", "- %", "+ %", "Minimum (h)", "Maximum (h)"])
    for r in (cycle_rows or []):
        row += 1
        h, m, p = float(r.hours or 0), float(r.var_minus or 0), float(r.var_plus or 0)
        for col, v in enumerate([r.phase, r.type, h, m, p, round(h * (1 - m / 100), 2), round(h * (1 + p / 100), 2)], 1):
            ws.cell(row=row, column=col, value=v)
    row += 2
    names = {"nett": "Nett (excl. weather)", "gross": "Gross (incl. weather)"}
    cols = ["nett", "gross"] if sim.basis == "gross" else ["nett"]
    for c in cols:
        ws.cell(row=row, column=1, value=names[c]).font = SECTION_FONT
        row += 1
        _header(ws, row, ["Result", "P10", "P50", "P90", "Average", "Minimum", "Maximum"])
        for label, m, div in (("Tidal delay (h)", "tidal", 1), ("Time restriction delay (h)", "restriction", 1),
                              ("Weather downtime (h)", "weather", 1), ("Total delay (h)", "total", 1),
                              ("Project duration (days)", "duration", 24)):
            row += 1
            st = sim.stats[c][m]
            ws.cell(row=row, column=1, value=label)
            for col, k in enumerate(("p10", "p50", "p90", "mean", "min", "max"), 2):
                cell = ws.cell(row=row, column=col, value=round(st[k] / div, 2))
                cell.number_format = "0.0"
        row += 1
        ws.cell(row=row, column=1, value="Finish date")
        for col, k in enumerate(("p10", "p50", "p90"), 2):
            cell = ws.cell(row=row, column=col, value=sim.finish[c][k])
            cell.number_format = DT
        row += 2
    ws.cell(row=row, column=1, value=f"P50 planning = run {sim.p50_index + 1} of {sim.n_runs} "
                                     f"(sheets 'P50 activities' and 'P50 cycles')")
    ws.column_dimensions["A"].width = 30
    for L in "BCDEFG":
        ws.column_dimensions[L].width = 17

    # every run
    runs_ws = wb.create_sheet("Runs")
    head = ["Run", "Start"]
    for c in cols:
        head += [f"{names[c]}: finish", "duration (days)", "tidal (h)", "time restriction (h)", "weather (h)",
                 "total delay (h)"]
    head += ["Average cycle work (h)"]
    rows = []
    for i, r in enumerate(sim.runs, 1):
        vals = [i, r["start"]]
        for c in cols:
            x = r[c]
            vals += [x["finish"], round(x["duration"] / 24, 2), _h(x["tidal"]), _h(x["restriction"]),
                     _h(x["weather"]), _h(x["total"])]
        vals.append(_h(sum(sum(h) for h in r["cycle_hours"]) / len(r["cycle_hours"])))
        rows.append(vals)
    fmts = {1: DT}
    for k in range(len(cols)):
        base = 2 + 6 * k
        fmts.update({base: DT, base + 1: "0.00", base + 2: HOURS, base + 3: HOURS, base + 4: HOURS, base + 5: HOURS})
    fmts[len(head) - 1] = HOURS
    _table(runs_ws, head, rows, formats=fmts, widths=[6, 17] + [17, 12, 10, 12, 11, 12] * len(cols) + [14])

    _activities(wb.create_sheet("P50 activities"), sim.p50_plan)
    _cycles(wb.create_sheet("P50 cycles"), sim.p50_plan)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
