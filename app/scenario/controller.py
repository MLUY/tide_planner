import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from munch import Munch
from plotly.subplots import make_subplots
import viktor as vkt

from .downtime import (DEFAULT_NORM, MONTHS, DowntimeError, Norm, default_month_rows, project_duration,
                       validate_norm)
from .export import build_excel
from .planning import (DEFAULT_CYCLE, PHASE_TYPES, GateRule, Phase, PlanningError, TimeRestriction,
                       load_tide, plan_installation)

COLORS = {"Port": "#8c564b", "Sailing": "#1f77b4", "Installation": "#2ca02c", "Tidal wait": "#d62728",
          "Time restriction wait": "#e377c2", "Weather downtime": "#17becf"}
WAIT_CATEGORIES = ("Tidal wait", "Time restriction wait", "Weather downtime")
RESTRICT_NO, RESTRICT_YES = "No", "Yes"
# start date sensitivity: what is analysed per start date (label -> function of a planning result, in hours)
SENS_HOURLY, SENS_DAILY = "Every hour (24 per day)", "Once a day (planned start time)"
SENS_STEPS = {SENS_HOURLY: 1, SENS_DAILY: 24}      # hours between start moments
SENS_METRICS = {
    "Tidal delay": lambda r: r.total_tidal_wait_h,
    "Tidal + time restriction delay": lambda r: r.total_tidal_wait_h + r.total_restriction_wait_h,
    "Total delay (incl. weather)": lambda r: r.total_wait_h,
    "Total project duration": lambda r: (r.finish - r.start).total_seconds() / 3600,
}
BASIS_GROSS, BASIS_NETT = "Gross (incl. weather downtime)", "Nett (excl. weather downtime)"
DEFAULT_TIDE = Path(__file__).resolve().parents[2] / "sample_data" / "gunsan_tide_2026_hourly.csv"
TABS = ("input", "added_features")
SECTIONS = ("refresh", "restrictions", "weather", "workability", "duration", "sensitivity")   # collapsible sections in 'Added features'


def _flat(params):
    """The inputs are split over tabs and sections (params.input.x, params.added_features.weather.y);
    the calculations use one flat set of inputs (params.x). Field names are unique over all of them."""
    out = Munch()
    for key, value in (params or {}).items():
        if key in TABS + SECTIONS and isinstance(value, dict):
            out.update(_flat(value))
        else:
            out[key] = value
    return out


def _required_tide(params):
    base = (float(params.draft or 0) + float(params.keel_clearance or 0)
            - float(params.channel_depth or 0)) * 100
    return base + float(params.margin_rising or 0), base + float(params.margin_falling or 0)


def _tide_summary(params, **kwargs) -> str:
    rising, falling = _required_tide(_flat(params))
    return f"Required tide: **{rising:.0f} cm** on a rising tide, **{falling:.0f} cm** on a falling tide."


def _tide_bytes(params) -> bytes:
    return params.tide_file.file.getvalue_binary() if params.tide_file else DEFAULT_TIDE.read_bytes()


def _run_plan(params, weather=None, tide=None, shift_days=0):
    """Planning for flat params; tide: already loaded TideCurve (loaded from the inputs when None);
    shift_days: start date shifted by this number of days (start date sensitivity)."""
    if not params.start_date:
        raise vkt.UserError("Please fill in the start date.")

    phases = [Phase(name=r.phase or f"Phase {i + 1}", type=r.type, hours=float(r.hours or 0))
              for i, r in enumerate(params.cycle) if r.type or r.phase or r.hours]
    rising, falling = _required_tide(params)
    rule = GateRule(rising_cm=rising, falling_cm=falling, transit_h=float(params.transit_h or 0))
    restrictions = [TimeRestriction(description=(r.description or "").strip(),
                                    from_h=float(r.from_h or 0), to_h=float(r.to_h or 0))
                    for r in (params.time_restrictions or [])
                    if r.description or r.from_h or r.to_h] \
        if params.use_time_restrictions == RESTRICT_YES else []
    start = (datetime.combine(params.start_date, datetime.min.time())
             + timedelta(days=shift_days, hours=float(params.start_hour or 0)))
    try:
        tide = tide or load_tide(_tide_bytes(params))
        return plan_installation(start=start, phases=phases, tide=tide, rule=rule,
                                 n_cycles=int(params.n_cycles or 0), restrictions=restrictions, weather=weather)
    except PlanningError as exc:
        raise vkt.UserError(str(exc))


_RESTRICT_ON = vkt.IsEqual(vkt.Lookup("added_features.restrictions.use_time_restrictions"), RESTRICT_YES)
_DOWNTIME_ON = vkt.IsEqual(vkt.Lookup("added_features.weather.use_downtime"), RESTRICT_YES)
_PLAN_CACHE: dict = {}


def _plans(params):
    """(nett planning, gross planning, norm) for flat params; gross = nett without weather downtime.
    Cached, because the output fields and views all need the same plannings."""
    # the uploaded tide file is identified by its content (its object text is not unique)
    data = _tide_bytes(params)
    key = json.dumps({**{k: v for k, v in params.items() if k != "tide_file"},
                      "tide_file": hashlib.sha1(data).hexdigest()}, default=str, sort_keys=True)
    if key not in _PLAN_CACHE:
        if len(_PLAN_CACHE) > 20:
            _PLAN_CACHE.clear()
        # nett or gross follows the weather choice: monthly downtime 'Yes' = gross planning (incl. weather),
        # 'No' = nett planning (weather 0 everywhere, gross = nett)
        norm = _selected_norm(params)
        try:
            tide = load_tide(data)              # read the file once for both plannings
        except PlanningError as exc:
            raise vkt.UserError(str(exc))
        nett = _run_plan(params, tide=tide)
        gross = _run_plan(params, weather=norm, tide=tide) if norm else nett
        _PLAN_CACHE[key] = (nett, gross, norm)
    return _PLAN_CACHE[key]


def _view_plan(params):
    """The planning shown in the views and the Excel export: gross when monthly downtime is applied,
    otherwise nett (then gross is the nett planning)."""
    _, gross, _ = _plans(params)
    return gross


def _selected_norm(params):
    """The workability norm from the two-column month table, or None when weather downtime is off."""
    if params.use_downtime != RESTRICT_YES:
        return None
    rows = list(params.downtime_months or [])
    if len(rows) != 12:
        raise vkt.UserError(f"The monthly workability table needs 12 rows (Jan-Dec); it has {len(rows)}.")
    norm = Norm(name=(params.downtime_name or "").strip() or "Workability norm",
                mws_h=float(params.downtime_mws_h or 0),
                monthly_pct=[None if r.pct is None else float(r.pct) for r in rows],
                year_pct=None if params.downtime_year is None else float(params.downtime_year))
    try:
        validate_norm(norm)
    except DowntimeError as exc:
        raise vkt.UserError(str(exc))
    return norm


def _duration(params):
    nett, gross, norm = _plans(params)
    return project_duration(nett, gross, norm)


def _out(fn):
    """Output field callback that shows '-' instead of an error while inputs are incomplete."""
    def callback(params, **kwargs):
        try:
            return fn(_duration(_flat(params)))
        except Exception:  # noqa: BLE001
            return "-"
    return callback


def _fmt_h(h):
    return f"{h:,.1f} h ({h / 24:,.1f} days)"


class Parametrization(vkt.Parametrization):
    # ---------------- tab 1: planning, cycle, vessel, channel, tide ----------------
    input = vkt.Tab("Input")
    input.title = vkt.Text("##  Tidal planner")
    input.download_excel = vkt.DownloadButton("Download planning (Excel)", method="download_excel",
                                              description="All activities (from - to, type of delay), cycles, "
                                                          "departures/entries and tidal data in one Excel file.")

    input.planning_text = vkt.Text("### Planning")
    input.start_date = vkt.DateField("Start date")
    input.start_hour = vkt.NumberField("Start time (hour of day)", default=0, min=0, max=23.99, suffix="h")
    input.n_cycles = vkt.IntegerField("Number of cycles", default=10, min=1)

    input.lb1 = vkt.LineBreak()
    input.cycle_text = vkt.Text("### (Nett) cycle\nThe port is left at the start of *Sail to site* "
                                "and entered at the end of *Sail to port*.")
    input.cycle = vkt.Table("Cycle phases", default=DEFAULT_CYCLE)
    input.cycle.phase = vkt.TextField("Phase")
    input.cycle.type = vkt.OptionField("Type", options=PHASE_TYPES)
    input.cycle.hours = vkt.NumberField("Duration", suffix="hr")   # no min/max: not supported in a table

    input.lb2 = vkt.LineBreak()
    input.vessel_text = vkt.Text("### Vessel")
    input.vessel_name = vkt.TextField("Vessel Name", default="Bokalift 2", flex=100)
    input.draft = vkt.NumberField("Vessel draft", suffix="m", default=11.0)
    input.keel_clearance = vkt.NumberField("Keel clearance", suffix="m", default=0.0, min=0)

    input.lb3 = vkt.LineBreak()
    input.channel_text = vkt.Text("### Tidal entrance channel")
    input.channel_from_to = vkt.TextField("Tidal entrance channel (from - to)", default="Berth 64 up to buoy 5",
                                          flex=100)
    input.channel_length = vkt.NumberField("Channel length (approx.)", suffix="km", default=2.0, min=0)
    input.channel_depth = vkt.NumberField("Dredged depth CD", suffix="m", default=7.4)
    input.transit_h = vkt.NumberField("Transit time ", suffix="hr", default=0, min=0)

    input.lb4 = vkt.LineBreak()
    input.tide_text = vkt.Text("### Tide")
    input.margin_rising = vkt.NumberField("Extra margin - rising tide", suffix="cm", default=0, min=0)
    input.margin_falling = vkt.NumberField("Extra margin - falling tide", suffix="cm", default=40, min=0)
    input.required = vkt.OutputField("Required tide", value=_tide_summary, flex=100)
    input.tide_file = vkt.FileField("Tidal data (optional, default Gunsan 2026)", file_types=[".csv"])
    input.tide_repeat_note = vkt.Text("*Note: the tidal data of one year is repeated for every other year "
                                      "(same calendar date and time), so the tidal data never runs out. This needs "
                                      "a file of at least one full year; a shorter file is only used for its own "
                                      "period. Repeated years are an approximation of the real tide.*")

    input.lb_refresh = vkt.LineBreak()
    # 'Refresh views' raises this hidden counter: VIKTOR redraws all views when an input changes
    input.refresh_counter = vkt.HiddenField("Refresh counter")
    input.refresh_views = vkt.SetParamsButton("Refresh views", method="refresh_views", longpoll=False,
                                              description="Recalculate the planning and redraw all views.")

    # ---------------- tab 2: time restrictions, weather downtime, project duration ----------------
    added_features = vkt.Tab("Added features")
    added_features.restrictions = vkt.Section("Time restrictions - sailing in/out of port", initially_expanded=True)
    added_features.restrictions.use_time_restrictions = vkt.OptionField(
        "Apply time restrictions?", options=[RESTRICT_NO, RESTRICT_YES], default=RESTRICT_NO)
    added_features.restrictions.restriction_text = vkt.Text(
        "Daily periods in which the vessel may **not sail in or out** of the port "
        "(e.g. no night transits). The vessel waits (alongside for departure, outside "
        "the port for entry) until the period has ended, on top of the tidal window. "
        "A period may run past midnight (e.g. from 22 to 6). "
        "Times in hours of the day, same time zone as the tidal data.\n\n"
        "Add one row per time slot (e.g. night closure, pilot shift change); "
        "any number of slots can be combined and they may overlap.",
        visible=_RESTRICT_ON)
    added_features.restrictions.time_restrictions = vkt.Table("Time restrictions", visible=_RESTRICT_ON)
    added_features.restrictions.time_restrictions.description = vkt.TextField("Description")
    # no min/max on table fields (not supported by VIKTOR); 0-24 h is checked by the planning
    added_features.restrictions.time_restrictions.from_h = vkt.NumberField("Not allowed from", suffix="h")
    added_features.restrictions.time_restrictions.to_h = vkt.NumberField("Not allowed until", suffix="h")

    added_features.weather = vkt.Section("Weather downtime (SME)", initially_expanded=True)
    added_features.weather.use_downtime = vkt.OptionField(
        "Apply monthly downtime percentages?", options=[RESTRICT_NO, RESTRICT_YES], default=RESTRICT_NO,
        description="Yes = gross planning (incl. weather downtime); No = nett planning (no weather downtime).")
    added_features.weather.basis_note = vkt.Text("**No** = nett planning, **Yes** = gross planning "
                                                 "(including weather downtime) in all views and the Excel export.")
    added_features.weather.downtime_text = vkt.Text(
        "Workability per month from the Site Metocean Report (SME): the percentage of time in which the "
        "operation can be performed within the MWS weather window. In the gross planning, the vessel waits "
        "on weather at site in every cycle for (working hours of the cycle) x (100 / workability of that "
        "month - 1). The table is in the section *Monthly workability* below (collapsible).",
        visible=_DOWNTIME_ON)

    # separate section, so the table can be collapsed while the selection above stays visible
    added_features.workability = vkt.Section("Monthly workability", initially_expanded=True, visible=_DOWNTIME_ON)
    added_features.workability.downtime_name = vkt.TextField("Norm", default=DEFAULT_NORM[0], flex=100)
    added_features.workability.downtime_mws_h = vkt.NumberField("MWS window", suffix="hr", default=DEFAULT_NORM[1],
                                                                min=0, flex=50)
    added_features.workability.downtime_year = vkt.NumberField("Year (SME)", suffix="%", default=DEFAULT_NORM[3],
                                                               min=0, max=100, flex=50)
    added_features.workability.downtime_months = vkt.Table("Workability per month",
                                                           default=default_month_rows())
    added_features.workability.downtime_months.month = vkt.TextField("Month")
    # no min/max on table fields (not supported by VIKTOR); 0-100 % is checked when the norm is read
    added_features.workability.downtime_months.pct = vkt.NumberField("Workability", suffix="%")

    added_features.duration = vkt.Section("Total project duration", initially_expanded=True)
    added_features.duration.out_cycles = vkt.OutputField(
        "Number of cycles / nominal cycle (no delays)",
        value=_out(lambda d: f"{d.n_cycles} cycles of {d.nominal_cycle_h:.1f} h"), flex=100)
    added_features.duration.out_delays = vkt.OutputField(
        "Delays gross planning: tidal + time restriction + weather = total",
        value=_out(lambda d: f"{d.gross_plan.total_tidal_wait_h:,.1f} + {d.gross_plan.total_restriction_wait_h:,.1f} + "
                             f"{d.gross_plan.total_weather_h:,.1f} = {d.gross_plan.total_wait_h:,.1f} h "
                             f"({d.gross_plan.total_wait_h / 24:,.1f} days)"), flex=100)
    added_features.duration.out_nett_cycle = vkt.OutputField(
        "Nett cycle (nominal + tidal / time restriction delays)",
        value=_out(lambda d: _fmt_h(d.nett_cycle_h)), flex=50)
    added_features.duration.out_gross_cycle = vkt.OutputField(
        "Gross cycle (nominal + all delays incl. weather)", value=_out(lambda d: _fmt_h(d.gross_cycle_h)), flex=50)
    added_features.duration.out_nett_total = vkt.OutputField(
        "Total nett duration", value=_out(lambda d: f"{d.n_cycles} x {d.nett_cycle_h:.1f} h = "
                                                    f"{_fmt_h(d.nett_total_h)}"), flex=50)
    added_features.duration.out_gross_total = vkt.OutputField(
        "Total gross duration", value=_out(lambda d: f"{d.n_cycles} x {d.gross_cycle_h:.1f} h = "
                                                     f"{_fmt_h(d.gross_total_h)}"), flex=50)
    added_features.duration.out_finish = vkt.OutputField(
        "Finish nett / gross", value=_out(lambda d: f"{d.nett_finish:%Y-%m-%d %H:%M} / {d.gross_finish:%Y-%m-%d %H:%M}"),
        flex=100)

    added_features.sensitivity = vkt.Section("Start date sensitivity", initially_expanded=True)
    added_features.sensitivity.sens_text = vkt.Text(
        "The start moment is never known exactly and the tidal windows make the delay sensitive to it. "
        "The planning is run for every start moment within the variance (every hour of every day, or once a "
        "day at the planned start time), with the same inputs and planning basis. The view *Start date "
        "sensitivity* shows the results as a distribution in buckets with the average, P50 and P90.")
    added_features.sensitivity.sens_days = vkt.NumberField("Start date variance (± days)", variant="slider",
                                                           min=0, max=30, step=1, default=7, num_decimals=0,
                                                           flex=100)
    added_features.sensitivity.sens_step = vkt.OptionField("Start moments", options=list(SENS_STEPS),
                                                           default=SENS_HOURLY, flex=50)
    added_features.sensitivity.sens_metric = vkt.OptionField("Result to analyse", options=list(SENS_METRICS),
                                                             default="Tidal delay", flex=50)
    added_features.sensitivity.sens_bucket = vkt.NumberField("Bucket size (0 = automatic)", default=0, min=0,
                                                             flex=50, description="In hours; in days for the "
                                                                                  "total project duration.")

    # last section of the tab; this tab uses sections, so VIKTOR requires the button to be inside one
    added_features.refresh = vkt.Section("Refresh", initially_expanded=True)
    added_features.refresh.refresh_views_2 = vkt.SetParamsButton(
        "Refresh views", method="refresh_views", longpoll=False,
        description="Recalculate the planning and redraw all views.")

def _basis_text(res) -> str:
    return f"Gross planning ({res.weather.name})" if res.weather else "Nett planning"


def _delay_text(res) -> str:
    txt = f"total delay {res.total_wait_h:.1f} h"
    parts = [f"tidal {res.total_tidal_wait_h:.1f} h"]
    if res.restrictions:
        parts.append(f"time restriction {res.total_restriction_wait_h:.1f} h")
    if res.weather:
        parts.append(f"weather {res.total_weather_h:.1f} h")
    if len(parts) > 1:
        txt += f" ({', '.join(parts)})"
    return txt


def build_gantt(res, vessel: str = "") -> go.Figure:
    order = []
    for s in res.segments:
        if s["activity"] not in order:
            order.append(s["activity"])
    rows = [dict(Activity=s["activity"], Category=s["category"], Start=s["start"], Finish=s["finish"],
                 Cycle=s["cycle"], Hours=round(s["hours"], 2), Detail=s["detail"]) for s in res.segments]
    fig = px.timeline(rows, x_start="Start", x_end="Finish", y="Activity", color="Category",
                      color_discrete_map=COLORS, hover_data=["Cycle", "Hours", "Detail"])
    fig.update_yaxes(categoryorder="array", categoryarray=order[::-1], title_text="")
    fig.update_xaxes(rangeslider_visible=True)
    # title on three rows: planning / start - end / delays (whole hours)
    parts = [f"tidal {res.total_tidal_wait_h:.0f} h"]
    if res.weather:
        parts.append(f"weather {res.total_weather_h:.0f} h")
    if res.restrictions:
        parts.append(f"time restriction {res.total_restriction_wait_h:.0f} h")
    delay = f"Total delay {res.total_wait_h:.0f} h ({', '.join(parts)})"
    fig.update_layout(
        title=(f"{_basis_text(res)} | {vessel + ' | ' if vessel else ''}{res.cycles} cycles | "
               f"nominal cycle {res.nominal_cycle_h:.0f} h<br>"
               f"Start {res.start:%Y-%m-%d %H:%M} | End {res.finish:%Y-%m-%d %H:%M}<br>"
               f"{delay}"),
        height=560, margin=dict(l=220, t=135), legend_title_text="")
    return fig


def build_delay_curve(res) -> go.Figure:
    fig = go.Figure(go.Scatter(x=[p[0] for p in res.delay_points], y=[p[1] for p in res.delay_points],
                               mode="lines", line=dict(color=COLORS["Tidal wait"], width=2),
                               name="Cumulative delay"))
    fig.update_layout(title=f"{_basis_text(res)} - cumulative delay {res.total_wait_h / 24:.1f} days<br>"
                            f"{_delay_text(res).capitalize()}",
                      xaxis_title="Date", yaxis_title="Total delay (hr)", height=450)
    return fig


def _inputs_revision(params) -> str:
    """Short fingerprint of all inputs (uploaded tide file by content)."""
    data = _tide_bytes(params)
    text = json.dumps({**{k: v for k, v in params.items() if k != "tide_file"},
                       "tide_file": hashlib.sha1(data).hexdigest()}, default=str, sort_keys=True)
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _result(fig, params) -> "vkt.PlotlyResult":
    """PlotlyResult that the browser always redraws when an input changed.
    Plotly in the browser only redraws what it detects as changed; layout.datarevision tells it the
    data is new (otherwise e.g. a new start time could stay invisible until a page refresh), and
    uirevision resets zoom/pan to the new planning."""
    fig_dict = fig if isinstance(fig, dict) else json.loads(fig.to_json())
    rev = _inputs_revision(params)
    fig_dict.setdefault("layout", {})["datarevision"] = rev
    fig_dict["layout"]["uirevision"] = rev
    return vkt.PlotlyResult(fig_dict)


def _json_safe(value):
    return value.isoformat() if isinstance(value, datetime) else value


def build_tide_cycles(res) -> dict:
    """Plotly figure as a dict. The many background blocks are added to the dict at the end as plain
    shapes: letting plotly create/validate them one by one takes seconds to minutes for long plannings."""
    tide, rule = res.tide, res.rule
    h0 = max(tide.to_hours(res.start) - 12, 0.0)
    h1 = min(tide.to_hours(res.finish) + 12, float(tide.hours[-1]))
    hrs = np.arange(h0, h1, 0.25)
    times = [tide.t0 + timedelta(hours=float(h)) for h in hrs]
    level = tide.level_cm(hrs)
    is_open = tide.gate_open(hrs, rule)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.03)

    # Background blocks are collected as plain shape dicts and set in one go: adding them one by one
    # with fig.add_vrect gets slower with every shape (minutes for a long planning).
    shapes = [dict(type="rect", xref="x", yref="y domain", x0=s["start"], x1=s["finish"], y0=0, y1=1,
                   fillcolor=COLORS[s["category"]], opacity=0.3 if s["category"] in WAIT_CATEGORIES else 0.12,
                   line=dict(width=0), layer="below")
              for s in res.segments]

    # daily periods in which sailing in/out of the port is not allowed (grey band at the top)
    if res.restrictions:
        day = datetime.combine(times[0].date(), datetime.min.time())
        while day <= times[-1]:
            for r in res.restrictions:
                spans = ([(r.from_h, r.to_h)] if r.from_h < r.to_h else [(0.0, r.to_h), (r.from_h, 24.0)])
                for a, b in spans:
                    x0, x1 = day + timedelta(hours=a), day + timedelta(hours=b)
                    if x1 > times[0] and x0 < times[-1]:
                        shapes.append(dict(type="rect", xref="x", yref="y domain", x0=x0, x1=x1, y0=0.93, y1=1.0,
                                           fillcolor="grey", opacity=0.35, line=dict(width=0), layer="below"))
            day += timedelta(days=1)
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name="No sailing in/out (time restriction)",
                                 marker=dict(symbol="square", size=12, color="grey", opacity=0.5)),
                      row=1, col=1)

    fig.add_trace(go.Scatter(x=times, y=level, mode="lines", name="Tide level",
                             line=dict(color="#1f77b4", width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=times, y=np.where(is_open, level, np.nan), mode="lines",
                             name="Window open", line=dict(color="#2ca02c", width=3)), row=1, col=1)
    # required tide lines (as plain shapes too; add_hline would copy all shapes again)
    for level_cm, dash, text, anchor in ((rule.rising_cm, "dash", f"Rising min. {rule.rising_cm:.0f} cm", "top"),
                                         (rule.falling_cm, "dot", f"Falling min. {rule.falling_cm:.0f} cm", "bottom")):
        shapes.append(dict(type="line", xref="x domain", x0=0, x1=1, yref="y", y0=level_cm, y1=level_cm,
                           line=dict(color="black", dash=dash)))
        fig.add_annotation(xref="x domain", x=0, yref="y", y=level_cm, text=text, showarrow=False,
                           xanchor="left", yanchor=anchor)

    for direction, symbol, color in (("Departure", "triangle-up", "#ff7f0e"), ("Entry", "triangle-down", "#9467bd")):
        g = [x for x in res.gates if x["direction"] == direction]
        fig.add_trace(go.Scatter(
            x=[x["time"] for x in g], y=[x["level_cm"] for x in g], mode="markers", name=direction,
            marker=dict(symbol=symbol, size=10, color=color),
            text=[f"Cycle {x['cycle']} {direction.lower()} {x['time']:%Y-%m-%d %H:%M} "
                  f"Tide {x['level_cm']:.0f} cm ({x['tide']}) Waited {x['wait_h']:.1f} h"
                  + (f" (time restriction {x['restriction_wait_h']:.1f} h)"
                     if x["restriction_wait_h"] > 1e-9 else "") for x in g],
            hoverinfo="text"), row=1, col=1)

    for cat, color in COLORS.items():
        segs = [s for s in res.segments if s["category"] == cat]
        if not segs:
            continue
        fig.add_trace(go.Bar(
            base=[s["start"] for s in segs],
            x=[(s["finish"] - s["start"]).total_seconds() * 1000 for s in segs],
            y=["Activity"] * len(segs), orientation="h", marker_color=color, name=cat,
            text=[f"Cycle {s['cycle']}: {s['activity']} {s['start']:%d-%m %H:%M} - {s['finish']:%d-%m %H:%M}"
                  f" {s['hours']:.1f} h" for s in segs],
            hoverinfo="text", textposition="none"), row=2, col=1)

    fig.update_xaxes(type="date")
    fig.update_yaxes(title_text="Tide level (cm)", row=1, col=1)
    fig.update_yaxes(showticklabels=False, row=2, col=1)
    repeat = (f"<br><sup>Tidal data {tide.data_start:%Y-%m-%d} - {tide.data_end:%Y-%m-%d} repeated for other "
              f"years</sup>" if tide.repeated and not (tide.is_measured(res.start) and tide.is_measured(res.finish))
              else "")
    fig.update_layout(title=f"{_basis_text(res)} - {res.cycles} cycles | {_delay_text(res)}{repeat}",
                      barmode="overlay", height=650, hovermode="closest", legend_title_text="")
    fig_dict = json.loads(fig.to_json())
    fig_dict["layout"]["shapes"] = [{k: _json_safe(v) for k, v in sh.items()} for sh in shapes]
    return fig_dict


def build_cycle_table(res) -> go.Figure:
    dep = {g["cycle"]: g for g in res.gates if g["direction"] == "Departure"}
    ent = {g["cycle"]: g for g in res.gates if g["direction"] == "Entry"}
    cs = res.cycle_stats
    header = ["Cycle", "Start", "Departure", "Wait dep. (h)", "Entry", "Wait entry (h)", "Cycle (h)",
              "Tidal delay (h)"]
    values = [
        [c["cycle"] for c in cs],
        [f"{c['start']:%Y-%m-%d %H:%M}" for c in cs],
        [f"{dep[c['cycle']]['time']:%Y-%m-%d %H:%M}" for c in cs],
        [f"{dep[c['cycle']]['wait_h']:.1f}" for c in cs],
        [f"{ent[c['cycle']]['time']:%Y-%m-%d %H:%M}" for c in cs],
        [f"{ent[c['cycle']]['wait_h']:.1f}" for c in cs],
        [f"{c['hours']:.1f}" for c in cs],
        [f"{c['tidal_wait_h']:.1f}" for c in cs]]
    if res.restrictions:
        header += ["Time restr. delay (h)"]
        values += [[f"{c['restriction_wait_h']:.1f}" for c in cs]]
    if res.weather:
        header += ["Weather downtime (h)"]
        values += [[f"{c['weather_h']:.1f}" for c in cs]]
    if res.restrictions or res.weather:
        header += ["Total delay (h)"]
        values += [[f"{c['wait_h']:.1f}" for c in cs]]
    fig = go.Figure(go.Table(header=dict(values=header, align="left"),
                             cells=dict(values=values, align="left")))
    avg = sum(c["hours"] for c in cs) / len(cs)
    fig.update_layout(title=f"{_basis_text(res)} - average cycle {avg:.1f} h vs nominal "
                            f"{res.nominal_cycle_h:.0f} h", height=600)
    return fig


def _duration_rows(d) -> list[tuple]:
    """(label, nett planning, gross planning) - in the order nominal -> delays -> cycle -> total."""
    n, g = d.nett_plan, d.gross_plan

    def h(x):
        return f"{x:,.1f} h"

    def hd(x):
        return f"{x:,.1f} h ({x / 24:,.1f} d)"
    return [
        ("Number of cycles", f"{d.n_cycles}", f"{d.n_cycles}"),
        ("Nominal cycle (no delays)", h(d.nominal_cycle_h), h(d.nominal_cycle_h)),
        ("Tidal delay (total)", hd(n.total_tidal_wait_h), hd(g.total_tidal_wait_h)),
        ("Time restriction delay (total)", hd(n.total_restriction_wait_h), hd(g.total_restriction_wait_h)),
        ("Weather downtime (total)", hd(n.total_weather_h), hd(g.total_weather_h)),
        ("<b>Total delay</b>", f"<b>{hd(n.total_wait_h)}</b>", f"<b>{hd(g.total_wait_h)}</b>"),
        ("Cycle (nominal + total delay / cycles)", h(d.nett_cycle_h), h(d.gross_cycle_h)),
        ("<b>Total duration</b> (cycles x cycle)",
         f"<b>{d.n_cycles} x {d.nett_cycle_h:.1f} h = {d.nett_total_h / 24:,.1f} d</b>",
         f"<b>{d.n_cycles} x {d.gross_cycle_h:.1f} h = {d.gross_total_h / 24:,.1f} d</b>"),
        ("Start", f"{d.start:%Y-%m-%d %H:%M}", f"{d.start:%Y-%m-%d %H:%M}"),
        ("Finish", f"{d.nett_finish:%Y-%m-%d %H:%M}", f"{d.gross_finish:%Y-%m-%d %H:%M}"),
        ("Weather downtime norm", "-",
         f"{d.norm.name} (MWS {d.norm.mws_h:g} hr), effective workability {d.effective_pct:.1f} %"
         if d.norm else "none - gross = nett"),
    ]


def build_duration(d) -> go.Figure:
    rows = _duration_rows(d)
    fill = ["#EEF3F9" if i % 2 else "white" for i in range(len(rows))]
    table = go.Table(columnwidth=[3, 3, 3],
                     header=dict(values=["<b>Project duration</b>", "<b>Nett planning</b>", "<b>Gross planning</b>"],
                                 align="left", fill_color="#C8D4E3", height=28),
                     cells=dict(values=[[r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]],
                                align="left", height=24, fill_color=[fill, fill, fill]))
    if not d.norm:
        fig = go.Figure(table)
        fig.update_layout(title="Total project duration - no weather downtime (nett planning or weather downtime "
                                "switched off): gross = nett", height=420,
                          margin=dict(t=60, b=20, l=60, r=20))
        return fig
    # table on top (fixed height so all rows are visible), compact bar chart below
    fig = make_subplots(rows=2, cols=1, row_heights=[0.62, 0.38], vertical_spacing=0.04,
                        specs=[[{"type": "table"}], [{"type": "xy"}]])
    fig.add_trace(table, row=1, col=1)
    # month, workability and number of cycles under the bar; the days inside the bar (always horizontal)
    labels = [f"<b>{m['month']:%b %Y}</b><br>{m['pct']:.1f} % | {m['cycles']} cycle{'s' if m['cycles'] != 1 else ''}"
              for m in d.months]
    fig.add_trace(go.Bar(
        x=labels, y=[m["downtime_h"] / 24 for m in d.months], name="Weather downtime",
        marker=dict(color=COLORS["Weather downtime"], line=dict(width=0)), opacity=0.85,
        text=[f"<b>{m['downtime_h'] / 24:.1f} d</b>" for m in d.months],
        textposition="inside", insidetextanchor="middle", textangle=0, constraintext="none",
        textfont=dict(color="white", size=14),
        customdata=[[m["downtime_h"], m["pct"], m["cycles"]] for m in d.months],
        hovertemplate="%{x}: %{y:.1f} days (%{customdata[0]:.1f} h) weather downtime<br>"
                      "workability %{customdata[1]:.1f} %, %{customdata[2]} cycle(s)<extra></extra>"),
        row=2, col=1)
    fig.update_yaxes(title_text="Days", rangemode="tozero", row=2, col=1)
    fig.update_xaxes(tickfont=dict(size=11), row=2, col=1)
    fig.update_layout(height=680, margin=dict(t=75, b=15, l=60, r=20), showlegend=False, bargap=0.2,
                      uniformtext=dict(minsize=11, mode="show"),
                      title=f"Total project duration - nett {d.nett_total_h / 24:.1f} days, gross "
                            f"{d.gross_total_h / 24:.1f} days ({d.norm.name})<br><sup>Weather downtime per month "
                            f"(month of arrival at site) - label: workability of the month and number of cycles</sup>")
    return fig


def _sensitivity(params) -> dict:
    """Run the planning for every start moment within ± N days (hourly or daily; same inputs and basis)."""
    n = int(round(float(params.sens_days or 0)))
    metric = params.sens_metric if params.sens_metric in SENS_METRICS else "Tidal delay"
    hourly = params.sens_step != SENS_DAILY
    nett, gross, norm = _plans(params)                       # also validates the inputs
    weather = norm
    planned = gross if weather else nett
    try:
        tide = load_tide(_tide_bytes(params))
    except PlanningError as exc:
        raise vkt.UserError(str(exc))
    day0 = datetime.combine(params.start_date, datetime.min.time())
    start_hour = float(params.start_hour or 0)
    runs = []
    for d in range(-n, n + 1):
        for h in (range(24) if hourly else [None]):
            if h is None:                                     # once a day, at the planned start time
                res = planned if d == 0 else _run_plan(params, weather=weather, tide=tide, shift_days=d)
            else:                                             # every whole hour of the day
                shift = d + (h - start_hour) / 24
                res = _run_plan(params, weather=weather, tide=tide, shift_days=shift)
            runs.append(dict(start=res.start, value=SENS_METRICS[metric](res), day=d,
                             hour=res.start.hour + res.start.minute / 60))
    values = np.array([r["value"] for r in runs])
    return dict(runs=runs, values=values, metric=metric, hourly=hourly, n=n, day0=day0,
                basis="gross" if weather else "nett", planned=SENS_METRICS[metric](planned),
                planned_start=planned.start, mean=float(values.mean()), p50=float(np.percentile(values, 50)),
                p90=float(np.percentile(values, 90)), min=float(values.min()), max=float(values.max()))


def _nice_bucket(span: float) -> float:
    """Automatic bucket size: about 12 buckets, rounded to 1 / 2 / 5 x 10^k."""
    raw = max(span / 12, 1e-9)
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)


def build_sensitivity(sens, bucket=0.0) -> go.Figure:
    metric = sens["metric"]
    days = metric == "Total project duration"           # project duration in days, delays in hours
    unit = "days" if days else "h"
    conv = (lambda h: h / 24) if days else (lambda h: h)
    vals = conv(sens["values"])
    n_runs = len(vals)
    mean, p50, p90, planned = (conv(sens[k]) for k in ("mean", "p50", "p90", "planned"))

    # buckets
    size = float(bucket) if bucket and bucket > 0 else _nice_bucket(float(vals.max() - vals.min()))
    lo = math.floor(vals.min() / size) * size
    edges = np.arange(lo, vals.max() + size * 1.0001, size)
    if len(edges) < 2:
        edges = np.array([lo, lo + size])
    counts, edges = np.histogram(vals, bins=edges)
    pct = counts / n_runs * 100
    centers = (edges[:-1] + edges[1:]) / 2
    mode = int(np.argmax(counts))
    labels = [f"{a:g}-{b:g}" for a, b in zip(edges[:-1], edges[1:])]
    cum = np.cumsum(counts) / n_runs * 100

    rows = 2 if sens["hourly"] else 1
    fig = make_subplots(rows=rows, cols=1, row_heights=[0.62, 0.38] if rows == 2 else [1.0], vertical_spacing=0.14,
                        specs=[[{"secondary_y": True}]] + ([[{}]] if rows == 2 else []),
                        subplot_titles=("", "Per start date and start hour") if rows == 2 else None)
    fig.add_trace(go.Bar(
        x=centers.tolist(), y=pct.tolist(), width=size * 0.92, name="share of start moments",
        marker_color=["#ff7f0e" if k == mode else "#1f77b4" for k in range(len(counts))],
        text=[f"{c}" if c else "" for c in counts], textposition="outside", textfont=dict(size=10),
        customdata=[[lab, int(c)] for lab, c in zip(labels, counts)],
        hovertemplate="%{customdata[0]} " + unit + "<br>%{customdata[1]} start moments (%{y:.1f} %)<extra></extra>",
        showlegend=False), row=1, col=1, secondary_y=False)
    fig.add_trace(go.Scatter(x=edges[1:].tolist(), y=cum.tolist(), mode="lines+markers", name="cumulative %",
                             line=dict(color="#7f7f7f", width=1.5), marker=dict(size=4),
                             hovertemplate="≤ %{x:g} " + unit + ": %{y:.0f} %<extra></extra>"),
                  row=1, col=1, secondary_y=True)
    for val, label, dash, color in ((mean, "Average", "dot", "#2ca02c"), (p50, "P50", "dash", "#9467bd"),
                                    (p90, "P90", "solid", "#d62728"), (planned, "Planned start", "dashdot", "#000000")):
        fig.add_vline(x=val, line=dict(color=color, dash=dash, width=2), row=1, col=1)
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", name=f"{label} {val:.1f} {unit}",
                                 line=dict(color=color, dash=dash, width=2)), row=1, col=1)
    fig.update_xaxes(title_text=f"{metric} ({unit}) - bucket {size:g} {unit} - {n_runs} start moments",
                     row=1, col=1)
    fig.update_yaxes(title_text="% of start moments", rangemode="tozero", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="cumulative %", range=[0, 105], tickvals=[0, 25, 50, 75, 100], showgrid=False,
                     row=1, col=1, secondary_y=True)

    if rows == 2:          # compact overview: value per start date (rows) and start hour (columns)
        dates = sorted({r["day"] for r in sens["runs"]})
        grid = np.full((len(dates), 24), np.nan)
        for r in sens["runs"]:
            grid[dates.index(r["day"]), int(round(r["hour"])) % 24] = conv(r["value"])
        ylab = [f"{sens['day0'] + timedelta(days=d):%d %b}" for d in dates]
        grid_list = [[None if np.isnan(v) else float(v) for v in row] for row in grid]
        fig.add_trace(go.Heatmap(z=grid_list, x=list(range(24)), y=ylab, colorscale="YlOrRd", showscale=True,
                                 colorbar=dict(title=unit, len=0.36, y=0.17, thickness=12),
                                 hovertemplate="%{y} %{x}:00 -> %{z:.1f} " + unit + "<extra></extra>"),
                      row=2, col=1)
        fig.update_xaxes(title_text="Start hour", dtick=2, row=2, col=1)
        fig.update_yaxes(autorange="reversed", tickfont=dict(size=9), row=2, col=1)

    step = "every hour" if sens["hourly"] else "once a day"
    fig.update_layout(
        height=800 if rows == 2 else 540, bargap=0, margin=dict(t=115, b=40, l=60, r=20),
        legend=dict(orientation="h", x=0.5, xanchor="center", y=1.01, yanchor="bottom", font=dict(size=11)),
        title=(f"Start date sensitivity - {metric.lower()}, {sens['basis']} planning, start ± {sens['n']} days "
               f"({step})<br><sup>most common {labels[mode]} {unit} ({pct[mode]:.0f} %) | average {mean:.1f} | "
               f"P50 {p50:.1f} | P90 {p90:.1f} | min {conv(sens['min']):.1f} | max {conv(sens['max']):.1f} {unit}"
               f"</sup>"))
    return fig


def _export_inputs(params) -> dict:
    return dict(
        general=[("Vessel", (params.vessel_name or "").strip(), ""),
                 ("Start date", datetime.combine(params.start_date, datetime.min.time())
                  + timedelta(hours=float(params.start_hour or 0)), ""),
                 ("Number of cycles", int(params.n_cycles or 0), ""),
                 ("Planning basis", BASIS_GROSS if params.use_downtime == RESTRICT_YES else BASIS_NETT, ""),
                 ("Vessel draft", float(params.draft or 0), "m"),
                 ("Keel clearance", float(params.keel_clearance or 0), "m"),
                 ("Tidal entrance channel (from - to)", params.channel_from_to or "", ""),
                 ("Channel length (approx.)", float(params.channel_length or 0), "km"),
                 ("Dredged channel depth CD", float(params.channel_depth or 0), "m"),
                 ("Extra margin - rising tide", float(params.margin_rising or 0), "cm"),
                 ("Extra margin - falling tide", float(params.margin_falling or 0), "cm")],
        phases=[(r.phase or f"Phase {i + 1}", r.type, float(r.hours or 0))
                for i, r in enumerate(params.cycle) if r.type or r.phase or r.hours],
        tide_source=(getattr(params.tide_file, "filename", None) or "uploaded file") if params.tide_file
        else f"default: {DEFAULT_TIDE.name}")


class Scenario(vkt.Controller):
    label = "Scenario"
    parametrization = Parametrization

    def refresh_views(self, params, **kwargs):
        """Raise the hidden counter: an input change makes VIKTOR recalculate and redraw every view."""
        count = int((params.input or {}).get("refresh_counter") or 0) + 1
        return vkt.SetParamsResult({"input": {"refresh_counter": count}})

    def download_excel(self, params, **kwargs):
        params = _flat(params)
        res = _view_plan(params)
        kind = "gross" if res.weather else "nett"
        name = f"tide_planning_{res.start:%Y%m%d}_{res.cycles}cycles_{kind}.xlsx"
        return vkt.DownloadResult(build_excel(res, _export_inputs(params), _duration(params)), name)

    @vkt.PlotlyView("Planning (Gantt)", duration_guess=2)
    def gantt_view(self, params, **kwargs):
        raw = params
        params = _flat(params)
        res = _view_plan(params)
        # diagnostic line in the viktor-cli terminal: which inputs the view received and what it shows
        print(f"[tide planner] {datetime.now():%H:%M:%S} Gantt view calculated | monthly downtime = "
              f"{params.use_downtime!r} | time restrictions = {params.use_time_restrictions!r} | start = "
              f"{res.start:%Y-%m-%d %H:%M} | title = {_basis_text(res)!r} | weather {res.total_weather_h:.0f} h | "
              f"input keys = {sorted(raw.keys()) if hasattr(raw, 'keys') else type(raw).__name__}", flush=True)
        return _result(build_gantt(res, (params.vessel_name or "").strip()), params)

    @vkt.PlotlyView("Tide and cycles", duration_guess=2)
    def tide_cycles_view(self, params, **kwargs):
        params = _flat(params)
        return _result(build_tide_cycles(_view_plan(params)), params)

    @vkt.PlotlyView("Cycle table", duration_guess=2)
    def cycle_table_view(self, params, **kwargs):
        params = _flat(params)
        return _result(build_cycle_table(_view_plan(params)), params)

    @vkt.PlotlyView("Tidal delay", duration_guess=2)
    def delay_view(self, params, **kwargs):
        params = _flat(params)
        return _result(build_delay_curve(_view_plan(params)), params)

    # a simulation of hundreds of plannings: runs when the button is clicked (duration_guess > 3 s)
    @vkt.PlotlyView("Start date sensitivity", duration_guess=10, update_label="Run simulation")
    def sensitivity_view(self, params, **kwargs):
        params = _flat(params)
        return _result(build_sensitivity(_sensitivity(params), float(params.sens_bucket or 0)), params)

    @vkt.PlotlyView("Project duration", duration_guess=2)
    def duration_view(self, params, **kwargs):
        params = _flat(params)
        return _result(build_duration(_duration(params)), params)