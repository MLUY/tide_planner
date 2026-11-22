import json
from datetime import datetime

import plotly.express as px
import plotly.graph_objects as go
import viktor as vkt

from .planning import DEFAULT_HOURS, PHASES, PlanningError, load_tide, plan_installation

COLORS = {"Port": "#8c564b", "Channel transit": "#17becf", "Sailing": "#1f77b4",
          "Installation": "#2ca02c", "Tidal wait": "#d62728"}

DEFAULT_CYCLE = [{"phase": ph["name"], "hours": h} for ph, h in zip(PHASES, DEFAULT_HOURS)]


def _required_tide_cm(params) -> float:
    """Total required depth = min. water depth + keel clearance. Tide needed = total - channel level."""
    total_depth = params.min_depth + params.keel_clearance
    return (total_depth - params.channel_level) * 100.0


def _run_plan(params):
    if not params.start_date:
        raise vkt.UserError("Please fill in the start date.")
    if not params.tide_file:
        raise vkt.UserError("Please upload the tidal data csv file.")
    if not params.n_mps or params.n_mps < 1:
        raise vkt.UserError("Number of monopiles must be at least 1.")
    if not params.mps_per_cycle or params.mps_per_cycle < 1:
        raise vkt.UserError("Monopiles per cycle must be at least 1.")
    if len(params.cycle) != len(PHASES):
        raise vkt.UserError(f"The cycle table must have exactly {len(PHASES)} rows.")

    hours = [float(r.hours or 0) for r in params.cycle]
    try:
        tide = load_tide(params.tide_file.file.getvalue_binary())
        return plan_installation(
            n_mps=int(params.n_mps),
            mps_per_cycle=int(params.mps_per_cycle),
            start=datetime.combine(params.start_date, datetime.min.time()),
            hours=hours,
            tide=tide,
            required_tide_cm=_required_tide_cm(params),
        )
    except PlanningError as exc:
        raise vkt.UserError(str(exc))


class Parametrization(vkt.Parametrization):
    title = vkt.Text("## Monopile installation - tidal planning")
    n_mps = vkt.IntegerField("Number of monopiles to install", default=100, min=1)
    mps_per_cycle = vkt.IntegerField("Monopiles per cycle", default=2, min=1)
    start_date = vkt.DateField("Start date")

    lb1 = vkt.LineBreak()
    cycle_text = vkt.Text("**Cycle (repeats until all monopiles are installed).** "
                          "Fill in the duration of each phase. Keep the row order.")
    cycle = vkt.DynamicArray("Cycle phases", min=6, max=6, default=DEFAULT_CYCLE)
    cycle.phase = vkt.TextField("Phase", flex=75)
    cycle.hours = vkt.NumberField("Duration", suffix="hr", min=0, flex=25)

    lb2 = vkt.LineBreak()
    tide_file = vkt.FileField("Tidal data (csv: date-time, tide level in cm)", file_types=[".csv"])
    channel_level = vkt.NumberField("Access channel level", suffix="m", default=8.0)
    min_depth = vkt.NumberField("Minimum required water depth in channel", suffix="m", default=11.0)
    keel_clearance = vkt.NumberField("Required keel clearance in channel", suffix="m", default=1.0, min=0)


def build_gantt(params) -> go.Figure:
    res = _run_plan(params)
    order = ["Tidal wait - outbound"] + [p["name"] for p in PHASES[:2]] + [p["name"] for p in PHASES[2:5]] \
        + ["Tidal wait - inbound", PHASES[5]["name"]]
    order = [o for o in order if any(sg["activity"] == o for sg in res.segments)]

    rows = [dict(Activity=s["activity"], Category=s["category"], Start=s["start"], Finish=s["finish"],
                 Cycle=s["cycle"], Hours=round(s["hours"], 2), Detail=s["detail"]) for s in res.segments]
    fig = px.timeline(rows, x_start="Start", x_end="Finish", y="Activity", color="Category",
                      color_discrete_map=COLORS, hover_data=["Cycle", "Hours", "Detail"])
    fig.update_yaxes(categoryorder="array", categoryarray=order[::-1], title_text="")
    fig.update_xaxes(rangeslider_visible=True)
    fig.update_layout(
        title=(f"{res.cycles} cycles | nominal cycle {res.nominal_cycle_h:.1f} h | "
               f"finish {res.finish:%Y-%m-%d %H:%M} | total tidal delay {res.total_wait_h:.1f} h"),
        height=520, margin=dict(l=300, t=60), legend_title_text="")
    return fig


def build_delay_curve(params) -> go.Figure:
    res = _run_plan(params)
    x = [p[0] for p in res.delay_points]
    y = [p[1] for p in res.delay_points]
    fig = go.Figure(go.Scatter(x=x, y=y, mode="lines", line=dict(color=COLORS["Tidal wait"], width=2),
                               name="Cumulative tidal delay"))
    fig.update_layout(title=f"Cumulative tidal delay: {res.total_wait_h:.1f} h ({res.total_wait_h / 24:.1f} days)",
                      xaxis_title="Date", yaxis_title="Total tidal delay (hr)", height=450)
    return fig


def build_tide_cycles(params) -> go.Figure:
    """Tide level with the required level, every cycle as a band and every tidal wait highlighted."""
    import numpy as np
    from datetime import timedelta
    res = _run_plan(params)
    tide = res.tide
    h0 = max(tide.to_hours(res.start) - 12, 0.0)
    h1 = min(tide.to_hours(res.finish) + 12, float(tide.hours[-1]))
    hrs = np.arange(h0, h1, 0.25)
    times = [tide.t0 + timedelta(hours=float(h)) for h in hrs]

    fig = go.Figure(go.Scatter(x=times, y=tide.level_cm(hrs), mode="lines", name="Tide level (cm)",
                               line=dict(color="#1f77b4", width=1.5)))
    fig.add_hline(y=res.required_cm, line=dict(color="black", dash="dash"),
                  annotation_text=f"Required tide {res.required_cm:.0f} cm", annotation_position="top left")

    cycles = {}
    for sg in res.segments:
        c = cycles.setdefault(sg["cycle"], [sg["start"], sg["finish"]])
        c[1] = sg["finish"]
    for n, (a, b) in cycles.items():
        fig.add_vrect(x0=a, x1=b, fillcolor="#2ca02c" if n % 2 else "#98df8a", opacity=0.10, line_width=0,
                      layer="below")
    for sg in res.segments:
        if sg["category"] == "Tidal wait":
            fig.add_vrect(x0=sg["start"], x1=sg["finish"], fillcolor=COLORS["Tidal wait"], opacity=0.35,
                          line_width=0)

    # one invisible trace per cycle: hover shows cycle number, wait and cycle duration
    mid, txt = [], []
    for n, (a, b) in cycles.items():
        wait = sum(s["hours"] for s in res.segments if s["cycle"] == n and s["category"] == "Tidal wait")
        mid.append(a + (b - a) / 2)
        txt.append(f"Cycle {n}<br>Start {a:%Y-%m-%d %H:%M}<br>Duration {(b - a).total_seconds() / 3600:.1f} h"
                   f"<br>Tidal wait {wait:.1f} h")
    fig.add_trace(go.Scatter(x=mid, y=[res.required_cm] * len(mid), mode="markers", name="Cycle info",
                             marker=dict(size=7, color="#2ca02c"), text=txt, hoverinfo="text"))
    fig.update_xaxes(rangeslider_visible=True)
    fig.update_layout(title=f"Tide level, cycles (green bands) and tidal waits (red) - {res.cycles} cycles",
                      yaxis_title="Tide level (cm)", xaxis_title="Date", height=520, hovermode="closest")
    return fig


class Scenario(vkt.Controller):
    label = "Scenario"
    parametrization = Parametrization

    summary = vkt.Summary(
        item_mps=vkt.SummaryItem("Monopiles", float, "parametrization", "n_mps"),
    )

    @vkt.PlotlyView("Planning (Gantt)", duration_guess=2)
    def gantt_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_gantt(params).to_json()))

    @vkt.PlotlyView("Tidal delay", duration_guess=2)
    def delay_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_delay_curve(params).to_json()))

    @vkt.PlotlyView("Tide and cycles", duration_guess=3)
    def tide_cycles_view(self, params, **kwargs):
        return vkt.PlotlyResult(json.loads(build_tide_cycles(params).to_json()))
