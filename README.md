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
- **Input**: vessel (loaded draft for departure, empty draft for entry), planning, (nett) cycle with
  - % / + % variance per phase, tidal entrance channel, tide, and at the bottom the **simulation**
  (number of runs, default 300, button *Simulate*).
- **Weather**: monthly workability (SME). Weather downtime is always part of the planning.
- **Time constraints**: daily periods without sailing in/out of port.
- **Download**: Excel of the most likely planning, and the simulation workbook.

## Results (all from the simulation)
- **Planning**: Gantt of the most likely planning, titled *Gross planning most likely* with start and finish.
- **Cycle**: tide and cycles of the P50 run, with the required tide for departure (loaded) and entry (empty).
- **Water depth**: the same, as water depth in the channel (dredged depth CD + tide, in m) with the required
  depth (draft + keel clearance + margin) for departure (loaded) and entry (empty), the departures / entries
  and the cycle sequence below.
- **Table**: cycle table of the P50 run.
- **Simulation**: P10 / P50 / P90 / average of tidal, time restriction, weather and total delay, tidal delay
  as % of the duration excl. weather, project duration and finish.
- **Results**: project duration of the most likely run, with the delays per month stacked by cause.

## Loaded / empty draft
Departure uses the loaded draft, entry the empty draft: required tide = draft + keel clearance - channel
depth (+ margins), so entering empty needs less tide.

## Weather downtime (monthly workability, SME)
Optional (*Apply monthly downtime percentages?* = Yes). Section *Monthly workability*: norm name,
MWS window, yearly value and one table with two columns (month | workability %). Default:
BL2 MP Installation Worst (10 hr): 57.8 65.5 76.6 81.9 88.7 91.7 84.7 80.6 79.2 69.9 66.7 57.8 (year 74.3).

## Weather downtime in the planning
Weather downtime is always included: in every cycle the vessel waits on weather at site (after *Sail to site*)
for (working hours of the cycle) x (100 / workability of that month - 1); the tidal windows are then planned on
this later timeline. There is no separate nett planning (excl. weather) in the analysis anymore.

## Simulation (Monte Carlo)
Because the cycles repeat with identical durations, a small shift can make the whole planning keep hitting
or keep missing the tidal windows ('lucky' / 'unlucky'). Therefore every result comes from a simulation:
- Cycle table (tab *Input*): per phase a **- %** and **+ %** variance (defaults: loading and sailing -10/+20 %,
  installation -10/+25 %). Without any variance all runs are identical and a red warning is shown.
- Every run draws a duration for every phase of every cycle (triangular: nominal - x % ... nominal + y %,
  most likely nominal) and plans the installation incl. weather downtime. Number of runs at the bottom of
  the tab *Input* (default 300), button *Simulate*.
- **Campaign variance - % / + % (whole run)** (Simulation section, default -5 / +10 %): one factor per run (triangular, most likely
  1.0) multiplies all phase durations of all cycles of that run - a whole campaign running faster or slower.
  Independent variances per phase average out over many cycles; the campaign variance gives the realistic
  P10 - P90 spread.
- The **most likely planning** is the run closest to the median in both project duration and tidal delay.
- The histogram bucket size is chosen automatically for a solid distribution: no empty buckets and at least
  2 % of the runs in every bucket of the core (P1 - P99).
- Tab *Download*: Excel of the most likely planning, and the simulation workbook (settings, P10/P50/P90
  statistics, every run, the most likely planning).

Test inputs to check the results: `sample_data/input`.
