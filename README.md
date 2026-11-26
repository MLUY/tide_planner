# Monopile tide planner (VIKTOR tree app)

ProjectsFolder > Project > Scenario. Create a Scenario, fill in the inputs and upload a tidal csv
(date-time, level in cm; see `sample_data/`).

Views: **Planning (Gantt)**, **Tide and cycles**, **Cycle table** and **Tidal delay**.

Button **Download planning (Excel)** exports one workbook with the sheets *Summary* (inputs and results),
*Activities* (every activity and wait, from - to, delay type: no delay / tidal / time restriction),
*Cycles* (incl. totals and averages), *Departures-Entries* (tide level and waits per port passage)
and *Tide data* (tide over the planning period with window, restriction and vessel activity).

## Defaults (Bokalift 2)
| Phase | Type | Hours |
|---|---|---|
| Loading | In port | 12 |
| Sail to site | Sail to site | 13 |
| Installation | At site | 52 |
| Sail to loading port | Sail to port | 12 |

Nominal cycle without tidal restriction: 89 h.

Entrance channel depth 7.4 m, vessel draft 11.0 m (3.6 m tide needed for zero keel clearance).

## Tidal rule
The vessel may only **depart** (start of *Sail to site*) or **enter** (end of *Sail to port*) when
- the tide is **rising** and at least **360 cm**, or
- the tide is **falling** and at least **400 cm**.

Optionally a channel transit time can be set; the rule must then hold for that whole period.

- Departure: vessel waits alongside after loading until the window opens.
- Entry: vessel waits outside the port until the window opens; loading starts after entering.
- Tide is linearly interpolated between data points; rising/falling = sign of the slope. Search step 5 min.
- Tidal data of one year is repeated for every other year (same calendar date and time), so the
  planning never runs out of data (before or after the file). Needs at least one full year of data;
  29 Feb in leap years gets the tide of 28 Feb. Repeated data is marked in the Excel *Tide data* sheet.
- Cycle rows: any number of *In port* rows, one *Sail to site*, any number of *At site* rows, one *Sail to port*.
- Tide datum is assumed to be the same as the channel depth datum (chart datum).

## Time restrictions (sailing in/out of port)
On top of the tidal rule, daily periods can be defined in which the vessel may **not sail in or out** of the port.
Set *Apply time restrictions?* to **Yes** to show the table *Time restrictions* (description, not allowed
from / until, hours of the day). With **No** (default) the table is hidden and ignored; rows entered earlier are kept.
- One row per time slot; any number of slots, they may overlap. Example: `No night transit` 22 until 6
  (periods may run past midnight) plus `Pilot shift change` 12 until 13.5.
- Departure: the vessel waits alongside; entry: the vessel waits outside the port, until the
  restriction has ended **and** the tidal window is open.
  If a transit time is set, no restriction may apply during the transit either.
- The wait is split: the wait the tide alone would cause is *Tidal wait*, the extra wait is
  *Time restriction wait* (pink in the charts; restricted periods shown as grey bands in *Tide and cycles*).
- Times are in the same time zone as the tidal data.

## Input tabs
- **Input**: vessel, planning, (nett) cycle, tidal entrance channel, tide.
- **Weather**: monthly workability (SME) in collapsible sections; *Yes* = gross planning (incl. weather),
  *No* = nett planning. The month table is only shown when weather downtime is switched on.
- **Variance**: start date sensitivity (± days slider, start moments, result, bucket size).
- **Time constraints**: daily periods without sailing in/out of port.
- **Download**: Excel download of the planning.
Every tab has a *Refresh views* button at the bottom. The project duration is in the view *Project duration*.

## Weather downtime (monthly workability, SME)
Optional (*Apply monthly downtime percentages?* = Yes). Section *Monthly workability*: norm name,
MWS window, yearly value and one table with two columns (month | workability %). Default:
BL2 MP Installation Worst (10 hr): 57.8 65.5 76.6 81.9 88.7 91.7 84.7 80.6 79.2 69.9 66.7 57.8 (year 74.3).

## Nett / gross planning
Follows the weather choice *Apply monthly downtime percentages?*:
- **No** = **nett planning**: cycles with tidal and time restriction waits, no weather downtime.
- **Yes** = **gross planning**: in every cycle the vessel waits on weather at site (after *Sail to site*) for
  (working hours of the cycle) x (100 / workability of that month - 1); the tidal windows are then
  planned on this later timeline (weather downtime shown in teal).
All views, the project duration, the Excel export and the start date sensitivity follow this choice.
The Gantt title shows: planning, vessel, cycles, nominal cycle / start and end / total delay split into
tidal, weather and time restriction (whole hours).

## Total project duration
Section in tab *Added features*, view **Project duration** and Excel (Summary + sheet *Weather downtime*),
nett and gross planning side by side: number of cycles, nominal cycle, tidal / time restriction /
weather delays and their total, cycle (nominal + total delay / cycles), total duration (cycles x cycle),
start and finish.

## Start date sensitivity
Section *Start date sensitivity* (tab *Added features*) and view **Start date sensitivity**:
the planning is run for every start date within ± N days (slider 0-30, one run per day, same start time
and planning basis). Result to analyse: tidal delay (default), tidal + time restriction delay, total delay
incl. weather, or total project duration. The view shows the result per start date (planned start in
orange) and a histogram with the average, P50 and P90 (P90 = exceeded by 10 % of the start dates).

Start moments: every hour of every day (default; e.g. ± 7 days = 360 plannings) or once a day at the planned
start time. The view shows the share of start moments per bucket (most common bucket in orange; bucket size
automatic or set), the cumulative %, average, P50, P90 and the planned start, and for the hourly run a map
per start date and start hour. Because it runs hundreds of plannings, the view runs with the button
*Run simulation*.

## Duration variance simulation (Monte Carlo)
Cycle table (tab *Input*): per phase a **- %** and **+ %** variance (defaults: loading, sailing -10/+20 %,
installation -10/+25 %). Tab *Variance*: number of runs (default 300), random seed, and optionally a random
start moment within the ± days.

Every run draws a duration for every phase of every cycle (triangular: nominal - x % ... nominal + y %,
most likely nominal) and plans the nett and - with weather downtime - the gross planning with the same
draws. Views (button *Run simulation*):
- **Simulation - P50 planning**: the run whose project duration is the median, as a Gantt, with the
  P10 / P50 / P90 finish dates.
- **Simulation - results**: P10 / P50 / P90 / average of tidal, time restriction, weather and total delay,
  project duration and finish date for nett and gross, plus the distribution of the project duration.
Tab *Download*: **Download simulation (Excel)** with the settings, statistics, every run and the P50
planning. The normal planning views keep using the nominal durations.
