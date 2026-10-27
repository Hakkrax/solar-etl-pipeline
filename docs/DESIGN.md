# Design notes

Decisions worth explaining in a code review or interview, with the evidence behind each.

## 1. Column names in the source are not trusted

PVDAQ column names describe the logger's original units, not what is stored.
`ac_power_1_kwac` holds **watts** and `ambient_temp_f` holds **degrees Celsius**.
Evidence: NREL's own metrics table (`pvdaq/parquet/metrics/metrics__system_<id>__part000.parquet`)
lists `units = W` and `units = C` for those columns, and the values agree
(about 30,000 on a 40 kW system at noon; 40 on a July afternoon in Las Vegas).
The pipeline maps each source column to a canonical field explicitly in
`config.py`, using that table, rather than guessing from the name.

### 1a. Even the metrics table can be wrong: the 29 March 2017 switch

Profiling the 2017 backfill flagged 1,199 module temperatures above 95 °C on
system 1420, all in February-March 2017. Monthly medians showed why: January
2017 median 45.5 vs 8.2 in January 2018, July 2016 median 95.6. The sensor
logged **°F from the start of the record until 28 March 2017** and °C from
29 March 2017, although the metrics table says °C throughout. A range check
alone only catches the hottest readings; the rest would have been stored as
plausible-looking but wrong °C values.

Looking for the same date elsewhere found more. Before 29 March 2017:

| System | Column | Logged as | Evidence |
|---|---|---|---|
| 1420 | AC power | kW | noon 27.7 on 28 Mar, 17,358 on 29 Mar; the daily-energy chart showed ~0 kWh for Q1 2017 |
| 1420 | module temperature | °F | above |
| 1418, 1419 | per-inverter AC power | kW | channels peak ~10-14 while the system total reads ~30,000-40,000 W |
| 1418, 1419 | wind speed | mph | site / Open-Meteo wind ratio 1.08-1.30 before, 0.51-0.53 after (x2.2, mph to m/s is x2.237) |

`config.py` carries a date-bounded `unit_fixes` entry for each, and every
conversion is counted under `unit_converted`. Because a hard-coded list only
covers problems already found, two general checks back it up:

- `implausible_power_scale` blanks a day's power readings when the peak is under
  2% of rated power while irradiance reaches 300 W/m² (the kW-for-W
  signature). Re-running January 2017 for 1420 without its kW fix, the guard
  blanks 29 of 31 days; the 2 it misses are overcast days that never reach
  300 W/m², which is why the explicit fix is still needed. In
  the corrected 2017-2019 run it blanks 28 readings, all on sparse days that
  had only 1-9 power readings near dawn.
- `power_total_mismatch` replaces a system total that disagrees with the sum of
  its inverter channels by more than 10% and 500 W. It did not fire on the
  2017-2019 data; it is a guard, exercised by the test fixture.

## 2. Timestamps are local wall-clock time with daylight saving

On 11 March 2018 the 02:00-02:45 readings are missing (clocks went forward).
On 4 November 2018 the file has one copy of 01:00-01:45 although that hour
happened twice. The pipeline localises with `America/Los_Angeles` and:

- drops rows in the skipped hour (`dst_nonexistent`), which should not exist;
- drops rows in the repeated hour (`dst_ambiguous`), because there is no way to
  know which of the two real hours they belong to. Guessing would silently
  put energy in the wrong UTC hour.

Everything is stored in UTC (`ts_utc`, hour start) plus a `local_date` for daily
reporting.

## 3. Weather radiation is shifted one hour

Open-Meteo's `shortwave_radiation` is the mean of the **preceding** hour, so the
value stamped 14:00 describes 13:00-14:00. The PV facts use hour-start labels,
so hour H takes the radiation stamped H+1. Instant variables (temperature,
cloud cover, wind) use the mean of the values at H and H+1. Without this, every
irradiance comparison would be off by an hour.

## 4. Flag, don't silently drop

Each rule in `validate.RULES` has a name, severity and action. Every rule that
changes or removes data adds to a per-system, per-day count (`dq_results`) and
to the affected hour's `dq_flags`. Night-time power and negative power become 0
(physically there is no output); stuck or impossible values become empty, so
hourly energy is left empty rather than invented.

An hour gets an energy value only if its valid readings cover at least 45 minutes
(3 of 4 at 15-minute logging, 9 of 12 at 5-minute logging). The logging interval
is detected per file from its own timestamps: all three systems logged every
5 minutes until late March 2017 (1419 and 1420 switched on 29 March, 1418 by
3 April) and every 15 minutes afterwards. A fixed "3 readings per hour" rule
would have accepted hours with only 15 minutes of data in that period. The
stuck-sensor rules are time-based for the same reason: identical readings must
last 2 hours, not a fixed number of rows.

## 5. Per-file schema checks

The set of columns changes from day to day. In July 2018, system 1418's files
carry no irradiance or temperature columns, and on 12 July 2018 one of its
three inverters is absent. Schema is therefore checked per file:

- no timestamp, or no AC power total and not every inverter channel present ->
  the day is rejected (`schema_missing_required`);
- an optional sensor missing -> that field is empty for the day
  (`schema_missing_optional`).

## 6. Idempotent, isolated loads

- Every table has a natural primary key; loads are `INSERT ... ON CONFLICT DO UPDATE`
  (SQLite and PostgreSQL dialects), so re-running a window, or overlapping
  windows, never duplicates rows.
- Each system's facts, weather and quality results are written in **one
  transaction**. Any failure for that system (download, parsing, weather, the
  database itself) rolls all three back; other systems still load and the run
  is marked `partial` with the error recorded in `pipeline_runs`.
- `loaded_run_id` and `loaded_at` give row-level lineage. They are excluded from
  the idempotency fingerprint because they are meant to change.

## 7. Capacity: AC rating vs DC nameplate

PVDAQ's `power` field is 30 kW for system 1420, which is its inverter (AC)
rating. Its 112 x 320 W modules give 35.84 kWp DC. Using the AC figure made the
performance ratio exceed 1.0, which is impossible. Specific yield and
performance ratio therefore use DC kWp from the module list; the
`power_above_capacity` check uses the rated figure.

## 8. Known limits

- Whether a 15-minute timestamp marks the start or the end of its interval is
  not documented by PVDAQ; the pipeline treats it as the start.
- System 1418 logs only from about 05:15 to 20:30 local time, so its night
  hours have no readings. Coverage is therefore reported for daylight hours
  only (modelled GHI of at least 20 W/m²).
- Inverter clipping on very sunny days could, in principle, produce identical
  readings that the `frozen_power` rule would remove. Exact float equality for
  2 hours is required, which real clipped output rarely meets;
  check the `frozen_power` count in the report before relying on clipped hours.
