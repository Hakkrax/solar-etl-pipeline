"""Turn raw day files into clean UTC readings, hourly facts and aligned weather."""

from __future__ import annotations

import io
from datetime import date, timedelta

import numpy as np
import pandas as pd

from solar_etl.config import INTERVAL_MINUTES, MIN_COVERED_MINUTES, System
from solar_etl.validate import NUMERIC_FIELDS, OPTIONAL_FIELDS, QualityLog, check_schema, flag_columns

HOUR = pd.Timedelta(hours=1)

UNIT_CONVERSIONS = {
    "kW": lambda s: s * 1000.0,  # -> W
    "degF": lambda s: (s - 32.0) * 5.0 / 9.0,  # -> °C
    "mph": lambda s: s * 0.44704,  # -> m/s
}


def utc_hour_grid(tz: str, start: date, end: date) -> pd.DatetimeIndex:
    """Every UTC hour from local midnight on ``start`` up to local midnight after ``end``."""
    first = pd.Timestamp(start).tz_localize(tz).tz_convert("UTC").tz_localize(None)
    last = pd.Timestamp(end + timedelta(days=1)).tz_localize(tz).tz_convert("UTC").tz_localize(None)
    return pd.date_range(first, last, freq="h", inclusive="left", name="ts_utc")


def expected_intervals(tz: str, day: date, minutes: int = INTERVAL_MINUTES) -> int:
    """Logging slots in a local day, e.g. 96 at 15 minutes; 92 or 100 on clock-change days."""
    start = pd.Timestamp(day).tz_localize(tz)
    end = pd.Timestamp(day + timedelta(days=1)).tz_localize(tz)
    return int((end - start) / pd.Timedelta(minutes=minutes))


def standardise_day(text: str, system: System, day: date, log: QualityLog) -> pd.DataFrame | None:
    """Parse one PVDAQ day file into canonical columns, or None if it cannot be used."""
    raw = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, na_values=[""])
    usable, missing_optional = check_schema(list(raw.columns), system)
    if not usable:
        log.add(system.system_id, day, "schema_missing_required")
        return None
    log.add(system.system_id, day, "schema_missing_optional", len(missing_optional))

    out = pd.DataFrame({"source_date": [day] * len(raw), "measured_on": raw["measured_on"]})
    sources = {f: system.columns.get(f) for f in NUMERIC_FIELDS}
    sources.update({f"channel_{i}": c for i, c in enumerate(system.ac_power_channels)})
    non_numeric = 0
    for field, column in sources.items():
        if column and column in raw.columns:
            values = pd.to_numeric(raw[column], errors="coerce")
            non_numeric += int((values.isna() & raw[column].notna()).sum())
            out[field] = values.astype("float64")
        else:
            out[field] = np.nan
    log.add(system.system_id, day, "non_numeric_value", non_numeric)

    for field, unit, fixed_from in system.unit_fixes:
        if day >= fixed_from:
            continue
        convert = UNIT_CONVERSIONS[unit]
        targets = [c for c in out.columns if c.startswith("channel_")] if field == "channels" else [field]
        for col in targets:
            log.add(system.system_id, day, "unit_converted", int(out[col].notna().sum()))
            out[col] = convert(out[col])
    return out


def to_utc(df: pd.DataFrame, system: System, log: QualityLog) -> pd.DataFrame:
    """Parse local timestamps, drop bad/duplicate/DST-ambiguous rows, add ``ts_utc``."""
    sid = system.system_id
    local = pd.to_datetime(df["measured_on"], errors="coerce", format="ISO8601")
    bad = local.isna()
    log.add_mask(sid, bad, df["source_date"], "unparseable_timestamp")
    df, local = df[~bad].copy(), local[~bad]

    dup = local.duplicated(keep="first")
    log.add_mask(sid, dup, df["source_date"], "duplicate_timestamp")
    df, local = df[~dup].copy(), local[~dup]

    ambiguous = local.dt.tz_localize(system.timezone, ambiguous="NaT", nonexistent="shift_forward").isna()
    nonexistent = local.dt.tz_localize(
        system.timezone, ambiguous=np.zeros(len(local), dtype=bool), nonexistent="NaT"
    ).isna()
    log.add_mask(sid, ambiguous, df["source_date"], "dst_ambiguous")
    log.add_mask(sid, nonexistent & ~ambiguous, df["source_date"], "dst_nonexistent")
    keep = ~(ambiguous | nonexistent)
    df, local = df[keep].copy(), local[keep]

    df["ts_utc"] = local.dt.tz_localize(system.timezone).dt.tz_convert("UTC").dt.tz_localize(None)
    df = df.drop(columns=["measured_on"])

    # Logging interval varies (system 1420 used 5 minutes in early 2017), so detect it per day.
    spacing = df.groupby("source_date")["ts_utc"].transform(lambda s: s.sort_values().diff().median())
    minutes = (spacing.dt.total_seconds() / 60).round().fillna(INTERVAL_MINUTES).clip(1, 60)
    df["interval_min"] = minutes.astype(int)

    for day, g in df.groupby("source_date"):
        expected = expected_intervals(system.timezone, day, int(g["interval_min"].iloc[0]))
        log.add(sid, day, "missing_interval", max(expected - len(g), 0))
    return df


def to_hourly(df: pd.DataFrame, system: System, grid: pd.DatetimeIndex, log: QualityLog) -> pd.DataFrame:
    """Aggregate clean 15-minute readings to one row per UTC hour on the full grid."""
    readings = df[(df["ts_utc"] >= grid[0]) & (df["ts_utc"] < grid[-1] + HOUR)].copy()
    readings["hour"] = readings["ts_utc"].dt.floor("h")
    if "interval_min" not in readings:
        readings["interval_min"] = INTERVAL_MINUTES
    readings["covered_min"] = readings["interval_min"].where(readings["ac_power_w"].notna(), 0)
    g = readings.groupby("hour")
    hourly = pd.DataFrame({
        "samples_received": g.size(),
        "samples_valid": g["ac_power_w"].count(),
        "covered_min": g["covered_min"].sum(),
        "ac_power_kw_mean": g["ac_power_w"].mean() / 1000.0,
        **{field: g[field].mean() for field in OPTIONAL_FIELDS},
    }).reindex(grid)
    hourly[["samples_received", "samples_valid"]] = hourly[["samples_received", "samples_valid"]].fillna(0).astype(int)

    enough = hourly["covered_min"].fillna(0) >= MIN_COVERED_MINUTES
    hourly["ac_energy_kwh"] = hourly["ac_power_kw_mean"].where(enough)  # mean kW over one hour = kWh

    local_dates = pd.Series(grid.tz_localize("UTC").tz_convert(system.timezone).date, index=grid)
    hourly["local_date"] = local_dates

    flags = pd.DataFrame(index=grid)
    for col in flag_columns(readings):
        flags[col.removeprefix("flag__")] = g[col].any().reindex(grid, fill_value=False).astype(bool)
    flags["low_coverage_hour"] = ~enough
    log.add_mask(system.system_id, ~enough, local_dates, "low_coverage_hour")
    hourly["dq_flags"] = [
        ",".join(sorted(name for name, hit in row.items() if hit)) or None
        for row in flags.to_dict("records")
    ]

    hourly = hourly.reset_index()
    hourly.insert(0, "system_id", system.system_id)
    return hourly[[
        "system_id", "ts_utc", "local_date", "ac_energy_kwh", "ac_power_kw_mean",
        "poa_irradiance_wm2", "ambient_temp_c", "module_temp_c", "wind_speed_ms",
        "samples_received", "samples_valid", "dq_flags",
    ]]


def align_weather(weather: pd.DataFrame, system: System, grid: pd.DatetimeIndex) -> pd.DataFrame:
    """Put Open-Meteo values on the same hour-start grid as the PV facts.

    Open-Meteo's ``shortwave_radiation`` is the mean of the preceding hour, so
    the value stamped H+1 describes hour H. Temperature, cloud cover and wind
    are instant values, so hour H gets the mean of the readings at H and H+1.
    """
    w = weather.set_index("ts_utc").sort_index()
    w = w[~w.index.duplicated(keep="first")]
    now, nxt = w.reindex(grid), w.reindex(grid + HOUR)

    def instant_mean(col: str) -> np.ndarray:
        return ((now[col].to_numpy() + nxt[col].to_numpy()) / 2.0)

    out = pd.DataFrame({
        "system_id": system.system_id,
        "ts_utc": grid,
        "ghi_wm2": nxt["shortwave_radiation"].to_numpy(),
        "air_temp_c": instant_mean("temperature_2m"),
        "cloud_cover_pct": instant_mean("cloud_cover"),
        "wind_speed_ms": instant_mean("wind_speed_10m"),
    })
    return out
