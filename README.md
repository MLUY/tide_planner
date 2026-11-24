# Monopile tide planner (VIKTOR tree app)

ProjectsFolder > Project > Scenario. Create a Scenario, fill in the inputs and upload a tidal csv
(date-time, level in cm; see `sample_data/`).

Views: **Planning (Gantt)**, **Tide and cycles**, **Cycle table** and **Tidal delay**.

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
- Cycle rows: any number of *In port* rows, one *Sail to site*, any number of *At site* rows, one *Sail to port*.
- Tide datum is assumed to be the same as the channel depth datum (chart datum).
