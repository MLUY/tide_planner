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
- **Input**: planning (incl. *Planning basis*), (nett) cycle, vessel, tidal entrance channel, tide.
- **Added features**: time restrictions, weather downtime, total project duration.

## Weather downtime (monthly workability, SME)
Optional (*Apply monthly downtime percentages?* = Yes). One card per norm (fits the input panel):
name, MWS window (hr) and yearly value on the first line, workability % for Jan-Jun and Jul-Dec below. Pre-filled with
BL2 General (6 hr), BL2 MP Installation Best (10 hr) and BL2 MP Installation Worst (10 hr).
Select the norm that is the basis for the downtime of the operational cycle.

## Nett / gross planning
*Planning basis* (tab Input), default **Gross**:
- **Nett**: cycles with tidal and time restriction waits, no weather downtime.
- **Gross**: in every cycle the vessel waits on weather at site (after *Sail to site*) for
  (working hours of the cycle) x (100 / workability of that month - 1); the tidal windows are then
  planned on this later timeline. Without weather downtime gross = nett.
All views and the Excel export follow the selected basis (Weather downtime is shown in grey).

## Total project duration
Section in tab *Added features*, view **Project duration** and Excel (Summary + sheet *Weather downtime*):
- Nett duration per cycle = average cycle of the nett planning (incl. tidal and time restriction waits).
- Total nett duration = number of cycles x nett duration per cycle.
- Total gross duration = duration of the gross planning.
