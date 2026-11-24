import json
from datetime import datetime, timedelta

import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import viktor as vkt

from .planning import (DEFAULT_CYCLE, PHASE_TYPES, GateRule, Phase, PlanningError, load_tide,
                       plan_installation)

COLORS = {"Port": "#8c564b", "Sailing": "#1f77b4", "Installation": "#2ca02c", "Tidal wait": "#d62728"}


def _run_plan(params):
    if not params.start_date:
        raise vkt.UserError("Please fill in the start date.")
    if not params.tide_file:
        raise vkt.UserError("Please upload the tidal data csv file.")

    phases = [Phase(name=r.phase or f"Phase {i + 1}", type=r.type, hours=float(r.hours or 0))
              for i, r in enumerate(params.cycle)]
    rule = GateRule(rising_cm=float(params.tide_rising), falling_cm=float(params.tide_falling),
                    transit_h=float(params.transit_h or 0))
    start = datetime.combine(params.start_date, datetime.min.time()) + timedelta(hours=float(params.start_hour or 0))
    try:
        tide = load_tide(params.tide_file.file.getvalue_binary())
        return plan_installation(start=start, phases=phases, tide=tide, rule=rule)
    except PlanningError as exc:
        raise vkt.UserError(str(exc))


def _draft_check(params, **kwargs) -> str:
    gap = (float(params.draft or 0) - float(params.channel_depth or 0)) * 100
    return (f"Draft minus channel depth = **{gap:.0f} cm** of tide needed for zero keel clearance. "
            f"Clearance with the thresholds: rising {float(params.tide_rising or 0) - gap:.0f} cm, "
            f"falling {float(params.tide_falling or 0) - gap:.0f} cm.")


class Parametrization(vkt.Parametrization):
    title = vkt.Text("## Monopile installation - tidal planning (Bokalift 2)")
    start_date = vkt.DateField("Start date")
    start_hour = vkt.NumberField("Start time (hour of day)", default=0, min=0, max=23.99, suffix="h")

    lb1 = vkt.LineBreak()
    cycle_text = vkt.Text("**Cycle without tidal restriction** (repeats from the start until the tidal data ends). "
                          "The port is left at the start of *Sail to site* and entered at the end of *Sail to port*.")
    cycle = vkt.DynamicArray("Cycle phases", min=4, default=DEFAULT_CYCLE)
    cycle.phase = vkt.TextField("Phase", flex=45)
    cycle.type = vkt.OptionField("Type", options=PHASE_TYPES, default=PHASE_TYPES[2], flex=30)
    cycle.hours = vkt.NumberField("Duration", suffix="hr", min=0, flex=25)

    lb2 = vkt.LineBreak()
    channel_text = vkt.Text("**Port entrance channel**")
    channel_depth = vkt.NumberField("Depth of entrance channel", suffix="m", default=7.4)
    draft = vkt.NumberField("Required vessel draft", suffix="m", default=11.0)
    tide_rising = vkt.NumberField("Minimum tide to depart/enter - rising tide", suffix="cm", default=360)
    tide_falling = vkt.NumberField("Minimum tide to depart/enter - falling tide", suffix="cm", default=400)
    transit_h = vkt.NumberField("Channel transit time (tide must stay above minimum)", suffix="hr",
                                default=0, min=0)
    check = vkt.OutputField("Keel clearance check", value=_draft_check, flex=100)

    lb3 = vkt.LineBreak()
    tide_file = vkt.FileField("Tidal data (csv: date-time, tide level in cm)", file_types=[".csv"])


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
    """Tide level, the rising/falling thresholds, open windows, cycles and the actual gate moments."""
    tide, rule = res.tide, res.rule
    h0 = max(tide.to_hours(res.start) - 12, 0.0)
    h1 = min(tide.to_hours(res.finish) + 12, float(tide.hours[-1]))
    hrs = np.arange(h0, h1, 0.25)
    times = [tide.t0 + timedelta(hours=float(h)) for h in hrs]
    level = tide.level_cm(hrs)
    is_open = tide.gate_open(hrs, rule)

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=times, y=level, mode="lines", name="Tide level",
                             line=dict(color="#1f77b4", width=1.5)))
    fig.add_trace(go.Scatter(x=times, y=np.where(is_open, level, np.nan), mode="lines",
                             name="Window open", line=dict(color="#2ca02c", width=3)))
    fig.add_hline(y=rule.rising_cm, line=dict(color="black", dash="dash"),
                  annotation_text=f"Rising min. {rule.rising_cm:.0f} cm", annotation_position="bottom left")
    fig.add_hline(y=rule.falling_cm, line=dict(color="black", dash="dot"),
                  annotation_text=f"Falling min. {rule.falling_cm:.0f} cm", annotation_position="top left")

    for c in res.cycle_stats:
        fig.add_vrect(x0=c["start"], x1=c["finish"], fillcolor="#7f7f7f" if c["cycle"] % 2 else "#c7c7c7",
                      opacity=0.08, line_width=0, layer="below")
    for s in res.segments:
        if s["category"] == "Tidal wait":
            fig.add_vrect(x0=s["start"], x1=s["finish"], fillcolor=COLORS["Tidal wait"], opacity=0.3, line_width=0)

    for direction, symbol in (("Departure", "triangle-up"), ("Entry", "triangle-down")):
        g = [x for x in res.gates if x["direction"] == direction]
        fig.add_trace(go.Scatter(
            x=[x["time"] for x in g], y=[x["level_cm"] for x in g], mode="markers", name=direction,
            marker=dict(symbol=symbol, size=10, color="#ff7f0e" if direction == "Departure" else "#9467bd"),
            text=[f"Cycle {x['cycle']} {direction.lower()}<br>{x['time']:%Y-%m-%d %H:%M}<br>"
                  f"Tide {x['level_cm']:.0f} cm ({x['tide']})<br>Waited {x['wait_h']:.1f} h" for x in g],
            hoverinfo="text"))
    fig.update_xaxes(rangeslider_visible=True)
    fig.update_layout(title=f"Tide, open windows (green), tidal waits (red) and port passages - {res.cycles} cycles",
                      yaxis_title="Tide level (cm)", xaxis_title="Date", height=520, hovermode="closest")
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

    summary = vkt.Summary(
        item_start=vkt.SummaryItem("Start date", str, "parametrization", "start_date"),
    )

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
n()))