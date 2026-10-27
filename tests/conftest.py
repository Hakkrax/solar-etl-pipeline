"""Synthetic PVDAQ-style fixtures with known, deliberately injected defects.

The messy day (2018-07-15) breaks every row-level rule a known number of
times; EXPECTED_MESSY lists those counts. Column names copy PVDAQ's
misleading style (``kwac`` holding watts) on purpose.
"""

from __future__ import annotations

import csv
import io
import math
import os
from dataclasses import replace
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from solar_etl import db
from solar_etl.config import System

CH1, CH2, TOTAL = "ac_power_1_kwac__1", "ac_power_2_kwac__2", "ac_power__3"
POA, AMB, MOD, WIND = ("poa_irradiance_sun__4", "ambient_temp_f_tempf__5",
                       "module_temp_f_temp1__6", "wind_speed_mph_windspeed__7")
HEADER = ["measured_on", CH1, CH2, TOTAL, POA, AMB, MOD, WIND, "system_id"]

TEST_SYSTEM = System(
    9001, "Test rooftop", 10.0, 36.0, -115.0, "America/Los_Angeles", 10.0, 180.0,
    columns={"ac_power_w": TOTAL, "poa_irradiance_wm2": POA, "ambient_temp_c": AMB,
             "module_temp_c": MOD, "wind_speed_ms": WIND},
    ac_power_channels=(CH1, CH2),
    dc_capacity_kw=10.0,
)
SECOND_SYSTEM = replace(TEST_SYSTEM, system_id=9002, name="Test carport")

MESSY_DAY = date(2018, 7, 15)
NO_OPTIONAL_DAY = date(2018, 7, 16)
NO_POWER_DAY = date(2018, 7, 17)
MISSING_DAY = date(2018, 7, 18)
SPRING_FORWARD = date(2018, 3, 11)
FALL_BACK = date(2018, 11, 4)

EXPECTED_MESSY = {
    "unparseable_timestamp": 1,
    "duplicate_timestamp": 1,
    "missing_interval": 2,
    "non_numeric_value": 1,
    "power_total_mismatch": 1,
    "power_from_channels": 1,
    "null_power": 1,
    "negative_power": 1,
    "power_above_capacity": 1,
    "irradiance_out_of_range": 1,
    "negative_irradiance_clipped": 1,
    "temperature_out_of_range": 1,
    "frozen_irradiance": 8,
    "night_power": 2,
    "frozen_power": 8,
    "low_coverage_hour": 3,  # local hours 03 (2 readings removed), 13 and 14 (frozen power)
}


def clean_rows(day: date, system_id: int = 9001) -> list[dict]:
    """96 plausible readings: a sine-shaped sunny day, 8 kW peak at 1,000 W/m²."""
    rows = []
    for i in range(96):
        t = datetime.combine(day, datetime.min.time()) + timedelta(minutes=15 * i)
        h = t.hour + t.minute / 60
        poa = round(1000 * math.sin(math.pi * (h - 6) / 14), 3) if 6 < h < 20 else 0.0
        power = round(poa * 8.0, 3)
        ambient = round(25 + 10 * math.sin(math.pi * (h - 8) / 12), 3)
        rows.append({
            "measured_on": t.strftime("%Y-%m-%d %H:%M:%S"),
            CH1: power / 2, CH2: power / 2, TOTAL: power, POA: poa, AMB: ambient,
            MOD: round(ambient + poa * 0.03, 3), WIND: round(2 + h / 10, 3), "system_id": system_id,
        })
    return rows


def to_csv(rows: list[dict], header: list[str] = HEADER) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue()


def messy_rows(day: date = MESSY_DAY) -> list[dict]:
    rows = {r["measured_on"][11:16]: r for r in clean_rows(day)}

    def set_(hhmm: str, **values) -> None:
        for key, value in values.items():
            rows[hhmm][{"total": TOTAL, "ch1": CH1, "ch2": CH2, "poa": POA, "amb": AMB, "wind": WIND}[key]] = value

    del rows["03:00"], rows["03:15"]                     # missing_interval x2
    set_("01:00", total=-15.0)                           # negative_power
    set_("22:00", total=100.0)                           # night_power
    set_("23:00", total=100.0)                           # night_power
    set_("08:00", total=None)                            # power_from_channels
    set_("08:15", total=None, ch1=None)                  # null_power
    set_("09:00", amb=85.0)                              # temperature_out_of_range
    set_("09:30", wind="ERR")                            # non_numeric_value
    set_("11:00", poa=2000.0)                            # irradiance_out_of_range
    set_("12:00", total=15000.0, ch1=7500.0, ch2=7500.0)  # power_above_capacity (> 12 kW)
    set_("10:30", total=100.0)                           # power_total_mismatch (channels sum ~7.7 kW)
    for i in range(8):                                   # frozen_power 13:00-14:45
        set_(f"{13 + i // 4:02d}:{15 * (i % 4):02d}", total=5000.0, ch1=2500.0, ch2=2500.0)
    for i in range(8):                                   # frozen_irradiance 16:00-17:45
        set_(f"{16 + i // 4:02d}:{15 * (i % 4):02d}", poa=400.0)
    set_("00:30", poa=-3.0)                              # negative_irradiance_clipped
    out = list(rows.values())
    out.append(dict(rows["10:00"]))                      # duplicate_timestamp
    out.append({**rows["10:15"], "measured_on": "not-a-time"})  # unparseable_timestamp
    return out


def fixture_days() -> dict[date, str | None]:
    return {
        MESSY_DAY: to_csv(messy_rows()),
        NO_OPTIONAL_DAY: to_csv(clean_rows(NO_OPTIONAL_DAY), [h for h in HEADER if h not in (MOD, WIND)]),
        NO_POWER_DAY: to_csv(clean_rows(NO_POWER_DAY), [h for h in HEADER if h not in (CH1, CH2, TOTAL)]),
        MISSING_DAY: None,
        SPRING_FORWARD: to_csv(clean_rows(SPRING_FORWARD)),
        FALL_BACK: to_csv(clean_rows(FALL_BACK)),
    }


class FakePvdaq:
    def __init__(self, days: dict[date, str | None] | None = None) -> None:
        self.days = fixture_days() if days is None else days
        self.calls: list[tuple[int, date]] = []

    def __call__(self, system_id: int, day: date) -> str | None:
        self.calls.append((system_id, day))
        return self.days.get(day)


def fake_weather(lat: float, lon: float, start: date, end: date) -> pd.DataFrame:
    """Hourly UTC weather; radiation peaks around 20:00 UTC (13:00 PDT)."""
    ts = pd.date_range(pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(hours=23), freq="h")
    local_h = (ts.hour - 7) % 24
    rad = [max(0.0, round(900 * math.sin(math.pi * (h - 6) / 14), 1)) if 6 < h < 20 else 0.0 for h in local_h]
    return pd.DataFrame({
        "ts_utc": ts, "temperature_2m": 30 + ts.hour / 10, "shortwave_radiation": rad,
        "cloud_cover": 10.0, "wind_speed_10m": 3.0,
    })


@pytest.fixture
def engine(tmp_path):
    url = os.environ.get("SOLAR_ETL_TEST_DB_URL") or f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    eng = db.get_engine(url)
    db.drop_all(eng)
    yield eng
    db.drop_all(eng)
    eng.dispose()
