import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import viktor as vkt

from .downtime import (MONTH_KEYS, MONTHS, DowntimeError, Norm, default_table_rows,
                       project_duration)
from .export import build_excel
from .planning import (DEFAULT_CYCLE, PHASE_TYPES, GateRule, Phase, PlanningError, TimeRestriction,
                       load_tide, plan_installation)

COLORS = {"Port": "#8c564b", "Sailing": "#1f77b4", "Installation": "#2ca02c", "Tidal wait": "#d62728",
          "Time restriction wait": "#e377c2"}
WAIT_CATEGORIES = ("Tidal wait", "Time restriction wait")
RESTRICT_NO, RESTRICT_YES = "No", "Yes"
DEFAULT_DOWNTIME_NORM = "BL2 MP Installation Best"
DEFAULT_TIDE = Path(__file__).resolve().parents[2] / "sample_data" / "gunsan_tide_2026_hourly.csv"


def _required_tide(params):
    base = (float(params.draft or 0) + float(params.keel_clearance or 0)
            - float(params.channel_depth or 0)) * 100
    return base + float(params.margin_rising or 0), base + float(params.margin_falling or 0)


def _tide_summary(params, **kwargs) -> str:
    rising, falling = _required_tide(params)
    return f"Required tide: **{rising:.0f} cm** on a rising tide, **{falling:.0f} cm** on a falling tide."


def _run_plan(params):
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
    start = datetime.combine(params.start_date, datetime.min.time()) + timedelta(hours=float(params.start_hour or 0))
    try:
        data = params.tide_file.file.getvalue_binary() if params.tide_file else DEFAULT_TIDE.read_bytes()
        tide = load_tide(data)
        return plan_installation(start=start, phases=phases, tide=tide, rule=rule,
                                 n_cycles=int(params.n_cycles or 0), restrictions=restrictions)
    except PlanningError as exc:
        raise vkt.UserError(str(exc))


_RESTRICT_ON = vkt.IsEqual(vkt.Lookup("use_time_restrictions"), RESTRICT_YES)
_DOWNTIME_ON = vkt.IsEqual(vkt.Lookup("use_downtime"), RESTRICT_YES)
_PLAN_CACHE: dict = {}


def _plan_cached(params):
    """The output fields all need the planning; compute it once per set of inputs."""
    key = json.dumps(params, default=str, sort_keys=True)
    if key not in _PLAN_CACHE:
        if len(_PLAN_CACHE) > 20:
            _PLAN_CACHE.clear()
        _PLAN_CACHE[key] = _run_plan(params)
    return _PLAN_CACHE[key]


def _norm_rows(params):
    return [r for r in (params.downtime_norms or []) if (r.norm or "").strip()]


def _norm_options(params, **kwargs):
    return [r.norm.strip() for r in _norm_rows(params)] or [default_table_rows()[0]["norm"]]


def _selected_norm(params):
    if params.use_downtime != RESTRICT_YES:
        return None
    rows = _norm_rows(params)
    if not rows:
        raise vkt.UserError("Monthly downtime is switched on, but the norm table is empty.")
    chosen = (params.downtime_norm or "").strip()
    row = next((r for r in rows if r.norm.strip() == chosen), None)
    if row is None:
        raise vkt.UserError("Select the norm that is the basis for the weather downtime.")
    return Norm(name=row.norm.strip(), mws_h=float(row.mws_h or 0),
                monthly_pct=[None if row[k] is None else float(row[k]) for k in MONTH_KEYS],
                year_pct=None if row.year is None else float(row.year))


def _duration(params, res=None):
    res = res or _plan_cached(params)
    nett_cycle = sum(c["hours"] for c in res.cycle_stats) / len(res.cycle_stats)
    try:
        return project_duration(start=res.start, n_cycles=res.cycles, nett_cycle_h=nett_cycle,
                                nominal_cycle_h=res.nominal_cycle_h, norm=_selected_norm(params))
    except DowntimeError as exc:
        raise vkt.UserError(str(exc))


def _out(fn):
    """Output field callback that shows '-' instead of an error while inputs are incomplete."""
    def callback(params, **kwargs):
        try:
            return fn(_duration(params))
        except Exception:  # noqa: BLE001
            return "-"
    return callback


def _fmt_h(h):
    return f"{h:,.1f} h ({h / 24:,.1f} days)"


class Parametrization(vkt.Parametrization):
    title = vkt.Text("##  Tidal planner")
    download_excel = vkt.DownloadButton("Download planning (Excel)", method="download_excel",
                                        description="All activities (from - to, type of delay), cycles, "
                                                    "departures/entries and tidal data in one Excel file.")

    planning_text = vkt.Text("### Planning")
    start_date = vkt.DateField("Start date")
    start_hour = vkt.NumberField("Start time (hour of day)", default=0, min=0, max=23.99, suffix="h")
    n_cycles = vkt.IntegerField("Number of cycles", default=10, min=1)

    lb1 = vkt.LineBreak()
    cycle_text = vkt.Text("### Cycle\nThe port is left at the start of *Sail to site* "
                          "and entered at the end of *Sail to port*.")
    cycle = vkt.Table("Cycle phases", default=DEFAULT_CYCLE)
    cycle.phase = vkt.TextField("Phase")
    cycle.type = vkt.OptionField("Type", options=PHASE_TYPES)
    cycle.hours = vkt.NumberField("Duration", suffix="hr", min=0)

    lb2 = vkt.LineBreak()
    vessel_text = vkt.Text("### Vessel")
    vessel_name = vkt.TextField("Vessel Name", default="Bokalift 2",
                                    flex=100)
    draft = vkt.NumberField("Vessel draft", suffix="m", default=11.0)
    keel_clearance = vkt.NumberField("Keel clearance", suffix="m", default=0.0, min=0)

    lb3 = vkt.LineBreak()
    channel_text = vkt.Text("### Tidal entrance channel")
    channel_from_to = vkt.TextField("Tidal entrance channel (from - to)", default="Berth 64 up to buoy 5",
                                    flex=100)
    channel_length = vkt.NumberField("Channel length (approx.)", suffix="km", default=2.0, min=0)
    channel_depth = vkt.NumberField("Dredged depth CD", suffix="m", default=7.4)
    transit_h = vkt.NumberField("Transit time ", suffix="hr", default=0, min=0)

    lb4 = vkt.LineBreak()
    tide_text = vkt.Text("### Tide")
    margin_rising = vkt.NumberField("Extra margin - rising tide", suffix="cm", default=0, min=0)
    margin_falling = vkt.NumberField("Extra margin - falling tide", suffix="cm", default=40, min=0)
    required = vkt.OutputField("Required tide", value=_tide_summary, flex=100)
    tide_file = vkt.FileField("Tidal data (optional, default Gunsan 2026)", file_types=[".csv"])
    tide_repeat_note = vkt.Text("*Note: the tidal data of one year is repeated for every other year "
                                "(same calendar date and time), so the tidal data never runs out. This needs a "
                                "file of at least one full year; a shorter file is only used for its own period. "
                                "Repeated years are an approximation of the real tide.*")

    lb5 = vkt.LineBreak()
    restriction_title = vkt.Text("### Time restrictions - sailing in/out of port")
    use_time_restrictions = vkt.OptionField("Apply time restrictions?", options=[RESTRICT_NO, RESTRICT_YES],
                                            default=RESTRICT_NO)
    restriction_text = vkt.Text("Daily periods in which the vessel may **not sail in or out** of the port "
                                "(e.g. no night transits). The vessel waits (alongside for departure, outside "
                                "the port for entry) until the period has ended, on top of the tidal window. "
                                "A period may run past midnight (e.g. from 22 to 6). "
                                "Times in hours of the day, same time zone as the tidal data.\n\n"
                                "Add one row per time slot (e.g. night closure, pilot shift change); "
                                "any number of slots can be combined and they may overlap.",
                                visible=_RESTRICT_ON)
    time_restrictions = vkt.Table("Time restrictions", visible=_RESTRICT_ON)
    time_restrictions.description = vkt.TextField("Description")
    time_restrictions.from_h = vkt.NumberField("Not allowed from", suffix="h", min=0, max=24)
    time_restrictions.to_h = vkt.NumberField("Not allowed until", suffix="h", min=0, max=24)

    lb6 = vkt.LineBreak()
    downtime_title = vkt.Text("### Weather downtime - monthly workability (SME)")
    use_downtime = vkt.OptionField("Apply monthly downtime percentages?", options=[RESTRICT_NO, RESTRICT_YES],
                                   default=RESTRICT_NO)
    downtime_text = vkt.Text("Workability per month from the Site Metocean Report (SME): the percentage of time "
                             "in which the operation can be performed within the MWS weather window. "
                             "One row per norm; add or edit rows as needed. "
                             "The selected norm is applied to the whole operational cycle.",
                             visible=_DOWNTIME_ON)
    downtime_norms = vkt.Table("Workability norms (%)", default=default_table_rows(), visible=_DOWNTIME_ON)
    downtime_norms.norm = vkt.TextField("Norm")
    downtime_norms.mws_h = vkt.NumberField("MWS window", suffix="hr", min=0)
    for _key, _label in zip(MONTH_KEYS, MONTHS):
        setattr(downtime_norms, _key, vkt.NumberField(_label, suffix="%", min=0, max=100))
    del _key, _label
    downtime_norms.year = vkt.NumberField("Year", suffix="%", min=0, max=100)
    downtime_norm = vkt.OptionField("Norm used as basis for the downtime", options=_norm_options,
                                    default=DEFAULT_DOWNTIME_NORM, visible=_DOWNTIME_ON, flex=60)

    lb7 = vkt.LineBreak()
    duration_title = vkt.Text("### Total project duration")
    out_nominal = vkt.OutputField("Nominal cycle (no delays)", value=_out(lambda d: _fmt_h(d.nominal_cycle_h)),
                                  flex=50)
    out_nett_cycle = vkt.OutputField("Nett duration per cycle (incl. tidal / time restriction waits)",
                                     value=_out(lambda d: _fmt_h(d.nett_cycle_h)), flex=50)
    out_nett_total = vkt.OutputField("Total nett duration (cycles x nett per cycle)",
                                     value=_out(lambda d: f"{d.n_cycles} x {d.nett_cycle_h:.1f} h = "
                                                          f"{_fmt_h(d.nett_total_h)}"), flex=100)
    out_gross_total = vkt.OutputField("Total gross duration (incl. weather downtime)",
                                      value=_out(lambda d: _fmt_h(d.gross_total_h)
                                                 + (f" - norm {d.norm.name}, downtime {d.downtime_h:,.1f} h"
                                                    if d.norm else " - no downtime applied")), flex=100)
    out_finish = vkt.OutputField("Finish (gross)", value=_out(lambda d: f"{d.gross_finish:%Y-%m-%d %H:%M}"),
                                 flex=50)


def _delay_text(res) -> str:
    txt = f"total delay {res.total_wait_h:.1f} h"
    if res.restrictions:
        txt += (f" (tidal {res.total_tidal_wait_h:.1f} h, "
                f"time restriction {res.total_restriction_wait_h:.1f} h)")
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
    fig.update_layout(
        title=(f"{vessel + ' | ' if vessel else ''}{res.cycles} cycles | nominal cycle {res.nominal_cycle_h:.0f} h | "
               f"finish {res.finish:%Y-%m-%d %H:%M}<br>{_delay_text(res).capitalize()}"),
        height=520, margin=dict(l=220, t=110), legend_title_text="")
    return fig


def build_delay_curve(res) -> go.Figure:
    fig = go.Figure(go.Scatter(x=[p[0] for p in res.delay_points], y=[p[1] for p in res.delay_points],
                               mode="lines", line=dict(color=COLORS["Tidal wait"], width=2),
                               name="Cumulative delay"))
    fig.update_layout(title=f"Cumulative delay: {res.total_wait_h:.1f} h ({res.total_wait_h / 24:.1f} days)"
                            + (f" - tidal {res.total_tidal_wait_h:.1f} h, time restriction "
                               f"{res.total_restriction_wait_h:.1f} h" if res.restrictions else ""),
                      xaxis_title="Date", yaxis_title="Total delay (hr)", height=450)
    return fig


def build_tide_cycles(res) -> go.Figure:
    tide, rule = res.tide, res.rule
    h0 = max(tide.to_hours(res.start) - 12, 0.0)
    h1 = min(tide.to_hours(res.finish) + 12, float(tide.hours[-1]))
    hrs = np.arange(h0, h1, 0.25)
    times = [tide.t0 + timedelta(hours=float(h)) for h in hrs]
    level = tide.level_cm(hrs)
    is_open = tide.gate_open(hrs, rule)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.03)

    for s in res.segments:
        fig.add_vrect(x0=s["start"], x1=s["finish"], fillcolor=COLORS[s["category"]],
                      opacity=0.3 if s["category"] in WAIT_CATEGORIES else 0.12,
                      line_width=0, layer="below", row=1, col=1, exclude_empty_subplots=False)

    # daily periods in which sailing in/out of the port is not allowed (grey band at the top)
    if res.restrictions:
        day = datetime.combine(times[0].date(), datetime.min.time())
        while day <= times[-1]:
            for r in res.restrictions:
                spans = ([(r.from_h, r.to_h)] if r.from_h < r.to_h else [(0.0, r.to_h), (r.from_h, 24.0)])
                for a, b in spans:
                    x0, x1 = day + timedelta(hours=a), day + timedelta(hours=b)
                    if x1 > times[0] and x0 < times[-1]:
                        fig.add_vrect(x0=x0, x1=x1, y0=0.93, y1=1.0, fillcolor="grey", opacity=0.35,
                                      line_width=0, layer="below", row=1, col=1,
                                      exclude_empty_subplots=False)
            day += timedelta(days=1)
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name="No sailing in/out (time restriction)",
                                 marker=dict(symbol="square", size=12, color="grey", opacity=0.5)),
                      row=1, col=1)

    fig.add_trace(go.Scatter(x=times, y=level, mode="lines", name="Tide level",
                             line=dict(color="#1f77b4", width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=times, y=np.where(is_open, level, np.nan), mode="lines",
                             name="Window open", line=dict(color="#2ca02c", width=3)), row=1, col=1)
    fig.add_hline(y=rule.rising_cm, line=dict(color="black", dash="dash"), row=1, col=1,
                  annotation_text=f"Rising min. {rule.rising_cm:.0f} cm", annotation_position="bottom left")
    fig.add_hline(y=rule.falling_cm, line=dict(color="black", dash="dot"), row=1, col=1,
                  annotation_text=f"Falling min. {rule.falling_cm:.0f} cm", annotation_position="top left")

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
    fig.update_layout(title=f"Tide and planning - {res.cycles} cycles | {_delay_text(res)}{repeat}",
                      barmode="overlay", height=650, hovermode="closest", legend_title_text="")
    return fig


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
        header += ["Time restr. delay (h)", "Total delay (h)"]
        values += [[f"{c['restriction_wait_h']:.1f}" for c in cs], [f"{c['wait_h']:.1f}" for c in cs]]
    fig = go.Figure(go.Table(header=dict(values=header, align="left"),
                             cells=dict(values=values, align="left")))
    avg = sum(c["hours"] for c in cs) / len(cs)
    fig.update_layout(title=f"Average cycle {avg:.1f} h vs nominal {res.nominal_cycle_h:.0f} h", height=600)
    return fig


def build_duration(d) -> go.Figure:
    rows = [("Nominal cycle (no delays)", _fmt_h(d.nominal_cycle_h)),
            ("Nett duration per cycle (incl. tidal / time restriction waits)", _fmt_h(d.nett_cycle_h)),
            ("Number of cycles", f"{d.n_cycles}"),
            ("Total nett duration", f"{d.n_cycles} x {d.nett_cycle_h:.1f} h = {_fmt_h(d.nett_total_h)}"),
            ("Weather downtime norm", f"{d.norm.name} (MWS {d.norm.mws_h:g} hr)" if d.norm else "None (not applied)"),
            ("Weather downtime", _fmt_h(d.downtime_h)),
            ("Total gross duration", _fmt_h(d.gross_total_h)),
            ("Effective workability", f"{d.effective_pct:.1f} %"),
            ("Start", f"{d.start:%Y-%m-%d %H:%M}"),
            ("Finish nett / gross", f"{d.nett_finish:%Y-%m-%d %H:%M}  /  {d.gross_finish:%Y-%m-%d %H:%M}")]
    if not d.norm:
        fig = go.Figure(go.Table(columnwidth=[3, 4], header=dict(values=["Project duration", ""], align="left"),
                                 cells=dict(values=[[r[0] for r in rows], [r[1] for r in rows]], align="left")))
        fig.update_layout(title="Total project duration - no weather downtime applied (gross = nett)", height=450)
        return fig
    fig = make_subplots(rows=2, cols=1, row_heights=[0.5, 0.5], vertical_spacing=0.08,
                        specs=[[{"type": "table"}], [{"type": "xy"}]])
    fig.add_trace(go.Table(columnwidth=[3, 4], header=dict(values=["Project duration", ""], align="left"),
                           cells=dict(values=[[r[0] for r in rows], [r[1] for r in rows]], align="left")),
                  row=1, col=1)
    labels = [f"{m['month']:%b %Y}" for m in d.months]
    fig.add_trace(go.Bar(x=labels, y=[m["nett_h"] for m in d.months], name="Working (nett)",
                         marker_color=COLORS["Installation"],
                         text=[f"{m['pct']:.1f} %" for m in d.months], textposition="inside",
                         hovertemplate="%{x}: %{y:.1f} h nett<extra></extra>"), row=2, col=1)
    fig.add_trace(go.Bar(x=labels, y=[m["downtime_h"] for m in d.months], name="Weather downtime",
                         marker_color="#7f7f7f", hovertemplate="%{x}: %{y:.1f} h downtime<extra></extra>"),
                  row=2, col=1)
    fig.update_yaxes(title_text="Hours", row=2, col=1)
    fig.update_layout(barmode="stack", height=750, legend_title_text="",
                      title=f"Total project duration - nett {d.nett_total_h / 24:.1f} days, gross "
                            f"{d.gross_total_h / 24:.1f} days ({d.norm.name})")
    return fig


def _export_inputs(params) -> dict:
    return dict(
        general=[("Vessel", (params.vessel_name or "").strip(), ""),
                 ("Start date", datetime.combine(params.start_date, datetime.min.time())
                  + timedelta(hours=float(params.start_hour or 0)), ""),
                 ("Number of cycles", int(params.n_cycles or 0), ""),
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

    def download_excel(self, params, **kwargs):
        res = _run_plan(params)
        name = f"tide_planning_{res.start:%Y%m%d}_{res.cycles}cycles.xlsx"
        return vkt.DownloadResult(build_excel(res, _export_inputs(params), _duration(params, res)), name)

    @vkt.PlotlyView("Project duration", duration_guess=2)
    def duration_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_duration(_duration(params, _run_plan(params))).to_json()))

    @vkt.PlotlyView("Planning (Gantt)", duration_guess=2)
    def gantt_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_gantt(_run_plan(params), (params.vessel_name or "").strip()).to_json()))

    @vkt.PlotlyView("Tide and cycles", duration_guess=3)
    def tide_cycles_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_tide_cycles(_run_plan(params)).to_json()))

    @vkt.PlotlyView("Cycle table", duration_guess=2)
    def cycle_table_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_cycle_table(_run_plan(params)).to_json()))

    @vkt.PlotlyView("Tidal delay", duration_guess=2)
    def delay_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_delay_curve(_run_plan(params)).to_json()))