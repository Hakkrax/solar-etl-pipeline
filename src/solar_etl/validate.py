"""Named data-quality rules.

Every rule that changes or drops data is counted per system and local day, so
nothing is removed silently. Row-level rules also leave a ``flag__<rule>``
column, which ends up in the hourly ``dq_flags`` field.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import pandas as pd

from solar_etl.config import System

REQUIRED_FIELDS = ("ac_power_w",)
OPTIONAL_FIELDS = ("poa_irradiance_wm2", "ambient_temp_c", "module_temp_c", "wind_speed_ms")
NUMERIC_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

MAX_POWER_RATIO = 1.2  # of rated power
MISMATCH_RATIO, MISMATCH_W = 0.10, 500.0
MIN_PEAK_RATIO, SUNNY_POA_WM2 = 0.02, 300.0
NIGHT_IRRADIANCE_WM2 = 5.0
FROZEN_MINUTES = 120  # identical non-zero readings lasting this long are treated as stuck
IRRADIANCE_RANGE = (-50.0, 1500.0)
AMBIENT_RANGE_C = (-30.0, 60.0)
MODULE_RANGE_C = (-30.0, 95.0)


@dataclass(frozen=True)
class Rule:
    name: str
    severity: str  # info | warning | error
    action: str
    description: str


RULES: tuple[Rule, ...] = (
    Rule("missing_file", "warning", "day skipped", "PVDAQ has no file for this system and day."),
    Rule("schema_missing_required", "error", "day rejected",
         "Timestamp or AC power column (and its per-inverter channels) absent from the file."),
    Rule("schema_missing_optional", "info", "field left empty", "An optional sensor column is absent from the file."),
    Rule("unparseable_timestamp", "warning", "row dropped", "measured_on could not be read as a timestamp."),
    Rule("duplicate_timestamp", "warning", "later copies dropped", "The same local timestamp appears more than once."),
    Rule("dst_ambiguous", "warning", "row dropped",
         "Local time is in the repeated hour when clocks go back, so its UTC time is unknown."),
    Rule("dst_nonexistent", "warning", "row dropped", "Local time is in the hour skipped when clocks go forward."),
    Rule("missing_interval", "info", "hour coverage reduced",
         "Fewer usable 15-minute readings than the local day should have."),
    Rule("unit_converted", "info", "converted to canonical unit",
         "A sensor known to have logged in another unit for this period (e.g. °F) was converted."),
    Rule("non_numeric_value", "info", "value left empty", "A sensor cell held text instead of a number."),
    Rule("power_total_mismatch", "warning", "total replaced by channel sum",
         "System AC power differs from the sum of its inverter channels by more than 10% and 500 W."),
    Rule("power_from_channels", "info", "total rebuilt from channels",
         "System AC power was empty but every inverter channel had a value."),
    Rule("null_power", "info", "reading left empty", "AC power empty in the source."),
    Rule("negative_power", "warning", "set to 0", "AC power below zero (standby draw or sensor offset)."),
    Rule("power_above_capacity", "warning", "value left empty", "AC power above 120% of the system's rated power."),
    Rule("implausible_power_scale", "warning", "day's power left empty",
         "Peak power under 2% of rated power on a day with at least 300 W/m² irradiance (likely wrong unit)."),
    Rule("irradiance_out_of_range", "warning", "value left empty",
         "Plane-of-array irradiance below -50 or above 1,500 W/m²."),
    Rule("negative_irradiance_clipped", "info", "set to 0",
         "Small negative irradiance (-50 to 0 W/m²), a normal night-time sensor offset."),
    Rule("temperature_out_of_range", "warning", "value left empty",
         "Ambient outside -30 to 60 °C, or module outside -30 to 95 °C."),
    Rule("frozen_irradiance", "warning", "value left empty",
         "The same non-zero irradiance for 2 hours or more (stuck sensor)."),
    Rule("night_power", "warning", "set to 0", "AC power above zero while irradiance is below 5 W/m²."),
    Rule("frozen_power", "warning", "value left empty",
         "The same non-zero AC power for 2 hours or more (stuck logger)."),
    Rule("low_coverage_hour", "info", "hourly energy left empty",
         "Valid power readings cover less than 45 minutes of the hour (3 of 4 at 15-minute logging)."),
)
RULES_BY_NAME = {rule.name: rule for rule in RULES}


class QualityLog:
    """Counts of affected rows per (system, local day, rule)."""

    def __init__(self) -> None:
        self._counts: dict[tuple[int, date, str], int] = defaultdict(int)

    def add(self, system_id: int, day: date, rule: str, rows: int = 1) -> None:
        if rule not in RULES_BY_NAME:
            raise KeyError(f"unknown rule {rule!r}")
        if rows:
            self._counts[(system_id, day, rule)] += int(rows)

    def add_mask(self, system_id: int, mask: pd.Series, days: pd.Series, rule: str) -> None:
        if not mask.any():
            return
        for day, rows in mask[mask].groupby(days[mask]).size().items():
            self.add(system_id, day, rule, rows)

    def totals(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for (_, _, rule), rows in self._counts.items():
            out[rule] += rows
        return dict(out)

    def count(self, rule: str, system_id: int | None = None, day: date | None = None) -> int:
        return sum(
            rows for (sid, d, r), rows in self._counts.items()
            if r == rule and (system_id is None or sid == system_id) and (day is None or d == day)
        )

    def records(self, run_id: str) -> list[dict]:
        return [
            {"run_id": run_id, "system_id": sid, "local_date": day, "rule": rule,
             "severity": RULES_BY_NAME[rule].severity, "rows_affected": rows}
            for (sid, day, rule), rows in sorted(self._counts.items())
        ]


def check_schema(columns: list[str], system: System) -> tuple[bool, list[str]]:
    """Return (usable, missing optional fields) for one day file's header."""
    cols = set(columns)
    has_total = system.columns["ac_power_w"] in cols
    has_channels = bool(system.ac_power_channels) and all(c in cols for c in system.ac_power_channels)
    usable = "measured_on" in cols and (has_total or has_channels)
    missing_optional = [f for f in OPTIONAL_FIELDS if f in system.columns and system.columns[f] not in cols]
    return usable, missing_optional


def _flag(df: pd.DataFrame, mask: pd.Series, rule: str, system: System, log: QualityLog) -> None:
    col = f"flag__{rule}"
    df[col] = df.get(col, False) | mask
    log.add_mask(system.system_id, mask, df["source_date"], rule)


def _frozen(series: pd.Series, interval_min: pd.Series | int = 15) -> pd.Series:
    """True for readings in runs of identical non-zero values lasting FROZEN_MINUTES or more."""
    minutes = pd.Series(interval_min, index=series.index) if not isinstance(interval_min, pd.Series) else interval_min
    run_id = (series != series.shift()).cumsum()
    run_minutes = minutes.groupby(run_id).transform("sum")
    return series.notna() & (series > 0) & (run_minutes >= FROZEN_MINUTES)


def apply_rules(df: pd.DataFrame, system: System, log: QualityLog) -> pd.DataFrame:
    """Apply row-level rules to one system's readings, sorted by time. Order matters."""
    df = df.sort_values("ts_utc", kind="stable").reset_index(drop=True)
    power = "ac_power_w"
    channels = [f"channel_{i}" for i in range(len(system.ac_power_channels))]

    if channels:
        channel_sum = df[channels].sum(axis=1, min_count=len(channels))
        gap = (df[power] - channel_sum).abs()
        mismatch = channel_sum.notna() & df[power].notna() & (gap > MISMATCH_RATIO * channel_sum) & (gap > MISMATCH_W)
        df.loc[mismatch, power] = channel_sum[mismatch]
        _flag(df, mismatch, "power_total_mismatch", system, log)

        rebuild = df[power].isna() & df[channels].notna().all(axis=1)
        df.loc[rebuild, power] = df.loc[rebuild, channels].sum(axis=1)
        _flag(df, rebuild, "power_from_channels", system, log)

    _flag(df, df[power].isna(), "null_power", system, log)

    negative = df[power] < 0
    df.loc[negative, power] = 0.0
    _flag(df, negative, "negative_power", system, log)

    too_high = df[power] > system.capacity_kw * 1000 * MAX_POWER_RATIO
    df.loc[too_high, power] = float("nan")
    _flag(df, too_high, "power_above_capacity", system, log)

    poa = "poa_irradiance_wm2"
    by_day = df.groupby("source_date")
    peak_power, peak_poa = by_day[power].transform("max"), by_day[poa].transform("max")
    implausible = (peak_poa >= SUNNY_POA_WM2) & (peak_power < MIN_PEAK_RATIO * system.capacity_kw * 1000)
    implausible &= df[power].notna()
    df.loc[implausible, power] = float("nan")
    _flag(df, implausible, "implausible_power_scale", system, log)

    lo, hi = IRRADIANCE_RANGE
    bad_poa = (df[poa] < lo) | (df[poa] > hi)
    df.loc[bad_poa, poa] = float("nan")
    _flag(df, bad_poa, "irradiance_out_of_range", system, log)
    clipped = df[poa] < 0
    df.loc[clipped, poa] = 0.0
    _flag(df, clipped, "negative_irradiance_clipped", system, log)

    bad_temp = pd.Series(False, index=df.index)
    for col, (tlo, thi) in (("ambient_temp_c", AMBIENT_RANGE_C), ("module_temp_c", MODULE_RANGE_C)):
        bad = (df[col] < tlo) | (df[col] > thi)
        df.loc[bad, col] = float("nan")
        bad_temp |= bad
    _flag(df, bad_temp, "temperature_out_of_range", system, log)

    interval = df["interval_min"] if "interval_min" in df else 15
    frozen_poa = _frozen(df[poa], interval)
    df.loc[frozen_poa, poa] = float("nan")
    _flag(df, frozen_poa, "frozen_irradiance", system, log)

    night = df[poa].notna() & (df[poa] < NIGHT_IRRADIANCE_WM2) & (df[power] > 0)
    df.loc[night, power] = 0.0
    _flag(df, night, "night_power", system, log)

    frozen_power = _frozen(df[power], interval)
    df.loc[frozen_power, power] = float("nan")
    _flag(df, frozen_power, "frozen_power", system, log)

    return df


def flag_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c.startswith("flag__")]
