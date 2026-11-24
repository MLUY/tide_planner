import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import viktor as vkt

from .planning import (DEFAULT_CYCLE, PHASE_TYPES, GateRule, Phase, PlanningError, load_tide,
                       plan_installation)

COLORS = {"Port": "#8c564b", "Sailing": "#1f77b4", "Installation": "#2ca02c", "Tidal wait": "#d62728"}
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
    start = datetime.combine(params.start_date, datetime.min.time()) + timedelta(hours=float(params.start_hour or 0))
    try:
        data = params.tide_file.file.getvalue_binary() if params.tide_file else DEFAULT_TIDE.read_bytes()
        tide = load_tide(data)
        return plan_installation(start=start, phases=phases, tide=tide, rule=rule,
                                 n_cycles=int(params.n_cycles or 0))
    except PlanningError as exc:
        raise vkt.UserError(str(exc))


class Parametrization(vkt.Parametrization):
    title = vkt.Text("## Monopile installation - tidal planning (Bokalift 2)")

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
    draft = vkt.NumberField("Vessel draft", suffix="m", default=11.0)
    keel_clearance = vkt.NumberField("Keel clearance", suffix="m", default=0.0, min=0)

    lb3 = vkt.LineBreak()
    channel_text = vkt.Text("### Tidal Entrance channel (berth 64 upto buoy 5 IS APPROX 2km)")
    channel_depth = vkt.NumberField("Dredged depth CD", suffix="m", default=7.4)
    transit_h = vkt.NumberField("Transit time ", suffix="hr", default=0, min=0)

    lb4 = vkt.LineBreak()
    tide_text = vkt.Text("### Tide")
    margin_rising = vkt.NumberField("Extra margin - rising tide", suffix="cm", default=0, min=0)
    margin_falling = vkt.NumberField("Extra margin - falling tide", suffix="cm", default=40, min=0)
    required = vkt.OutputField("Required tide", value=_tide_summary, flex=100)
    tide_file = vkt.FileField("Tidal data (optional, default Gunsan 2026)", file_types=[".csv"])


def build_gantt(res) -> go.Figure:
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
        title=(f"{res.cycles} cycles | nominal cycle {res.nominal_cycle_h:.0f} h | "
               f"finish {res.finish:%Y-%m-%d %H:%M} | total tidal delay {res.total_wait_h:.1f} h"),
        height=480, margin=dict(l=220, t=60), legend_title_text="")
    return fig


def build_delay_curve(res) -> go.Figure:
    fig = go.Figure(go.Scatter(x=[p[0] for p in res.delay_points], y=[p[1] for p in res.delay_points],
                               mode="lines", line=dict(color=COLORS["Tidal wait"], width=2),
                               name="Cumulative tidal delay"))
    fig.update_layout(title=f"Cumulative tidal delay: {res.total_wait_h:.1f} h ({res.total_wait_h / 24:.1f} days)",
                      xaxis_title="Date", yaxis_title="Total tidal delay (hr)", height=450)
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
                      opacity=0.12 if s["category"] != "Tidal wait" else 0.3,
                      line_width=0, layer="below", row=1, col=1)

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
                  f"Tide {x['level_cm']:.0f} cm ({x['tide']}) Waited {x['wait_h']:.1f} h" for x in g],
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
    fig.update_layout(title=f"Tide and planning - {res.cycles} cycles | total tidal delay {res.total_wait_h:.1f} h",
                      barmode="overlay", height=650, hovermode="closest", legend_title_text="")
    return fig


def build_cycle_table(res) -> go.Figure:
    dep = {g["cycle"]: g for g in res.gates if g["direction"] == "Departure"}
    ent = {g["cycle"]: g for g in res.gates if g["direction"] == "Entry"}
    cs = res.cycle_stats
    fig = go.Figure(go.Table(
        header=dict(values=["Cycle", "Start", "Departure", "Wait dep. (h)", "Entry", "Wait entry (h)",
                            "Cycle (h)", "Tidal delay (h)"], align="left"),
        cells=dict(values=[
            [c["cycle"] for c in cs],
            [f"{c['start']:%Y-%m-%d %H:%M}" for c in cs],
            [f"{dep[c['cycle']]['time']:%Y-%m-%d %H:%M}" for c in cs],
            [f"{dep[c['cycle']]['wait_h']:.1f}" for c in cs],
            [f"{ent[c['cycle']]['time']:%Y-%m-%d %H:%M}" for c in cs],
            [f"{ent[c['cycle']]['wait_h']:.1f}" for c in cs],
            [f"{c['hours']:.1f}" for c in cs],
            [f"{c['wait_h']:.1f}" for c in cs]], align="left")))
    avg = sum(c["hours"] for c in cs) / len(cs)
    fig.update_layout(title=f"Average cycle {avg:.1f} h vs nominal {res.nominal_cycle_h:.0f} h", height=600)
    return fig


class Scenario(vkt.Controller):
    label = "Scenario"
    parametrization = Parametrization

    @vkt.PlotlyView("Planning (Gantt)", duration_guess=2)
    def gantt_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_gantt(_run_plan(params)).to_json()))

    @vkt.PlotlyView("Tide and cycles", duration_guess=3)
    def tide_cycles_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_tide_cycles(_run_plan(params)).to_json()))

    @vkt.PlotlyView("Cycle table", duration_guess=2)
    def cycle_table_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_cycle_table(_run_plan(params)).to_json()))

    @vkt.PlotlyView("Tidal delay", duration_guess=2)
    def delay_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_delay_curve(_run_plan(params)).to_json()))