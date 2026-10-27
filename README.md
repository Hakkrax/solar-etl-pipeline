# Solar PV ETL pipeline

[![tests](https://github.com/Hakkrax/solar-etl-pipeline/actions/workflows/tests.yml/badge.svg)](https://github.com/Hakkrax/solar-etl-pipeline/actions/workflows/tests.yml)
[![scheduled canary run](https://github.com/Hakkrax/solar-etl-pipeline/actions/workflows/canary.yml/badge.svg)](https://github.com/Hakkrax/solar-etl-pipeline/actions/workflows/canary.yml)

An automated Python ETL pipeline that turns messy public solar-panel monitoring
data into a clean, trusted, hourly SQL dataset joined with weather, with a
named data-quality rule for every fix it makes.

- **Sources:** [NREL PVDAQ](https://doi.org/10.25984/1846021) 5- and 15-minute
  monitoring files from a public S3 bucket, and the
  [Open-Meteo](https://open-meteo.com/) historical weather API. Both are free,
  keyless and CC BY 4.0.
- **Sites:** three rooftop systems on Clark County public buildings in Las Vegas,
  Nevada (30-42 kW, hot desert climate), 2017-2019.
- **Output:** SQLite by default, PostgreSQL with one connection URL (tested in CI
  against PostgreSQL 16). `solar-etl snapshot` exports a portable SQLite file
  for downstream apps.

> This is **historical open data**, not a live equipment feed. No employer code
> or data is used.

## Results: 2017-2019 backfill

One run, three systems, 1,095 days each: 78,840 hourly fact rows and 78,840
weather rows, loaded in under 2.5 minutes from a cold cache on a laptop (about
25 seconds when re-run from the cache). Full report:
[reports/quality_report.md](reports/quality_report.md).

| System | DC kWp | Energy 2017-19 | Daylight coverage | Median daily PR |
|---|---:|---:|---:|---:|
| 1418 Desert Breeze | 43.68 | 151.1 MWh | 71.3% | 0.805 |
| 1419 Hollywood Rec Center | 43.68 | 169.1 MWh | 86.8% | 0.778 |
| 1420 Government Center | 35.84 | 177.7 MWh | 99.9% | 0.918 |

Daylight coverage is the share of daylight hours with a trusted energy value.
The gaps are real: 1418 has 229 days with no file, and 1419's files carried only
one of its two inverters from October 2017 to March 2018, so those days are
rejected rather than reported at half output. PR (performance ratio) is
indicative only, because capacity comes from PVDAQ's module list.

![Daily energy per system](reports/figures/daily_energy.png)

Largest rule counts in that run: `unit_converted` 148,270 readings,
`night_power` 62,202, `null_power` 33,241, `low_coverage_hour` 20,371 hours,
`schema_missing_required` 236 days, `missing_file` 229 days.

## What it does

```
PVDAQ S3 (daily CSV per system)  ─┐
                                  ├─ extract ─ validate ─ transform ─ load ─┬─ fact_pv_hourly
Open-Meteo archive API (hourly) ──┘  retries   named      UTC, DST,   upsert ├─ weather_hourly
                                     + cache   rules      hourly,     per    ├─ dq_results
                                                          weather     system ├─ pipeline_runs
                                                          alignment   txn    └─ views: v_daily_system,
                                                                                v_pv_weather_hourly
                                                                     └─ quality report (Markdown, JSON, PNG)
```

| Stage | Module | Highlights |
|---|---|---|
| Extract | `extract/pvdaq.py`, `extract/open_meteo.py` | Shared `requests` session with retry and backoff on 429/5xx, 30 s timeouts, raw-file cache (re-runs make no network calls), 404 recorded as a data gap, API response shape and units checked before caching |
| Validate | `validate.py` | 23 named rules with severity and action; per-file schema check; counts per system, day and rule |
| Transform | `transform.py` | Local time to UTC with explicit daylight-saving handling, 15-minute to hourly, coverage threshold, weather aligned to hour-start |
| Load | `load.py`, `db.py` | SQLAlchemy Core schema, `INSERT ... ON CONFLICT DO UPDATE` on natural keys, one transaction per system |
| Quality | `quality.py` | Per-run Markdown report, JSON summary and charts |
| Orchestration | `pipeline.py`, `cli.py` | Failure isolation per system, run audit table, JSON-lines logs with run and system context |

Design decisions and the evidence for each are in [docs/DESIGN.md](docs/DESIGN.md).

## Data problems it handles

These were found in the real files, not invented for the demo:

| Problem | Example | Rule |
|---|---|---|
| Column names lie about units | `ac_power_1_kwac` holds watts; `ambient_temp_f` holds °C (per NREL's metrics table) | explicit column map in `config.py` |
| Daylight-saving gaps and repeats | 11 Mar 2018 has no 02:00 hour; 4 Nov 2018 holds one copy of a repeated hour | `dst_nonexistent`, `dst_ambiguous` |
| Power at night | system 1420 reports 100 W at midnight with zero irradiance | `night_power` |
| Columns that come and go | system 1418's July 2018 files have no irradiance or temperature; one day is missing an inverter | `schema_missing_optional`, `schema_missing_required` |
| Missing days and readings | some days have no file; 1418 only logs in daylight | `missing_file`, `missing_interval`, `low_coverage_hour` |
| Units change mid-record | until 29 Mar 2017: 1420 power in kW and module temperature in °F; 1418/1419 inverter channels in kW and wind in mph (checked against Open-Meteo wind) | date-bounded `unit_fixes`, `unit_converted`, plus `implausible_power_scale` as a safety net |
| Logging interval changes | 5-minute readings until late March 2017, 15-minute after | interval detected per file; coverage measured in minutes |
| Inconsistent capacity | 1420's listed 30 kW is the inverter rating; its modules total 35.84 kWp | DC capacity used for yield and PR |

Other rules (`duplicate_timestamp`, `negative_power`, `frozen_power`,
`frozen_irradiance`, `power_above_capacity`, out-of-range checks and more) are
exercised by a deliberately messy fixture in the tests.

## Run it

Needs Python 3.11 or 3.12.

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt -e .
```

On Windows use `.venv\Scripts\pip` instead. Then:

```bash
solar-etl run --start 2019-06-01 --end 2019-06-30
```

This downloads about 90 day files and one weather series per system into
`data/raw/`, loads `data/solar.db`, and writes `reports/quality_report.md`. A
second run uses the cache and changes no rows.

Other commands:

| Command | Purpose |
|---|---|
| `solar-etl run --start D --end D [--systems 1419,1420] [--refresh]` | load a window of local dates |
| `solar-etl canary --start D --end D` | run the window twice and fail if any table changed |
| `solar-etl report [--run-id ID]` | rebuild the report for a run |
| `solar-etl snapshot --out solar.db` | export every table and view to a portable SQLite file |

PostgreSQL: `pip install -e ".[postgres]"` and pass
`--db postgresql+psycopg://user:pass@host/dbname` (or set `SOLAR_ETL_DB_URL`).

## Tests

```bash
pytest -q
```

63 tests (plus 1 live test, run with `pytest -m live`). A synthetic "messy
day" injects each row-level defect a known number of times, and the tests check
every rule's exact count, that fixes are applied (not just counted), daylight-
saving handling, 5-minute vs 15-minute logging, weather alignment, idempotent
re-runs and overlapping windows, rollback when a source or the database fails
mid-load, the report, the snapshot export and the CLI.

GitHub Actions runs the suite on Python 3.11 and 3.12 with SQLite, and again
against a PostgreSQL 16 service container. A weekly scheduled job (and a manual
trigger) runs June 2019 twice against the real sources, compares table
fingerprints, and uploads the quality report as a build artifact.

## Schema

| Table / view | Grain | Key |
|---|---|---|
| `dim_system` | one row per PV system | `system_id` |
| `fact_pv_hourly` | system x UTC hour: energy, mean power, irradiance, temperatures, samples, `dq_flags` | `(system_id, ts_utc)` |
| `weather_hourly` | system x UTC hour: GHI, air temperature, cloud cover, wind | `(system_id, ts_utc)` |
| `dq_results` | run x system x local day x rule: rows affected | `(run_id, system_id, local_date, rule)` |
| `pipeline_runs` | one row per run: window, status, rows, error, code version | `run_id` |
| `v_daily_system` | system x local day: energy, specific yield, insolation, daylight coverage, performance ratio | view |
| `v_pv_weather_hourly` | hourly facts joined to weather | view |

## Project layout

```
src/solar_etl/
  config.py        systems, column maps, settings
  extract/         http.py (retries), pvdaq.py, open_meteo.py
  validate.py      rule catalogue, QualityLog, row-level rules
  transform.py     UTC + DST, hourly aggregation, weather alignment
  db.py            tables and views (SQLAlchemy Core)
  load.py          dialect-aware upsert
  pipeline.py      orchestration and failure isolation
  quality.py       report, JSON summary, charts
  snapshot.py      SQLite export, table fingerprints
  cli.py           command line
tests/             messy fixtures, unit and integration tests
reports/           quality report from the 2017-2019 backfill
docs/DESIGN.md     decisions and evidence
```

## Data credits

- NREL, PV Data Acquisition (PVDAQ) public dataset, via the Open Energy Data
  Initiative, CC BY 4.0. https://doi.org/10.25984/1846021
- Open-Meteo historical weather API (ERA5-based reanalysis), CC BY 4.0.

MIT licence for the code. The data is downloaded at run time, is not stored in
this repository and is not covered by the code licence.
