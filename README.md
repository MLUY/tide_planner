# Monopile tide planner (VIKTOR tree app)

Same framework as the planning-tool app: ProjectsFolder > Project > Scenario.
Create a Scenario, fill in the inputs, upload a tidal csv (date-time, level in cm; see `sample_data/`).

Views: **Planning (Gantt)** and **Tidal delay** (cumulative tidal delay over time).

## Rules used
- Required tide (cm) = (min. water depth + keel clearance - channel level) x 100
  -> (11.0 + 1.0 - 8.0) x 100 = 400 cm with the defaults.
- Tide is linearly interpolated between the hourly points; waiting is searched in 5-minute steps.
- Outbound: channel is crossed in the first (sailing time - hours site-to-channel) hours of "Sail to site".
  The vessel waits alongside in port, before the exit activities.
- Inbound: vessel sails site -> channel entrance (hours site-to-channel), waits there if needed, then transits.
- The vessel can berth in port at any tide.
- Each activity needs a Type: Operation, Exit port, Sail to site (exactly one) or Sail to port (exactly one).
- Tide datum is assumed to be the same datum as the channel level (chart datum).
- Start date is taken as 00:00; the tide file times (KST) are used as-is.
