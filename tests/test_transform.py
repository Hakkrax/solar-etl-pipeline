from datetime import date

import numpy as np
import pandas as pd
import pytest

from conftest import FALL_BACK, SPRING_FORWARD, TEST_SYSTEM, FakePvdaq, fake_weather
from solar_etl import pipeline, transform, validate

TZ = TEST_SYSTEM.timezone


@pytest.mark.parametrize("day,expected", [(date(2018, 7, 15), 96), (SPRING_FORWARD, 92), (FALL_BACK, 100)])
def test_expected_intervals_follow_clock_changes(day, expected):
    assert transform.expected_intervals(TZ, day) == expected


def test_hour_grid_spans_local_days_in_utc():
    grid = transform.utc_hour_grid(TZ, date(2018, 7, 15), date(2018, 7, 16))
    assert len(grid) == 48
    assert grid[0] == pd.Timestamp("2018-07-15 07:00")  # local midnight PDT = 07:00 UTC
    assert len(transform.utc_hour_grid(TZ, SPRING_FORWARD, SPRING_FORWARD)) == 23
    assert len(transform.utc_hour_grid(TZ, FALL_BACK, FALL_BACK)) == 25


def test_spring_forward_rows_in_skipped_hour_are_dropped():
    log = validate.QualityLog()
    hourly, _ = pipeline.build_system(TEST_SYSTEM, SPRING_FORWARD, SPRING_FORWARD, FakePvdaq(), fake_weather, log)
    assert log.count("dst_nonexistent") == 4  # 02:00-02:45 never happened that night
    assert log.count("missing_interval") == 0
    assert log.count("dst_ambiguous") == 0
    assert len(hourly) == 23


def test_fall_back_repeated_hour_is_dropped_not_guessed():
    log = validate.QualityLog()
    hourly, _ = pipeline.build_system(TEST_SYSTEM, FALL_BACK, FALL_BACK, FakePvdaq(), fake_weather, log)
    assert log.count("dst_ambiguous") == 4  # 01:00-01:45 happened twice; the file holds one copy
    assert log.count("missing_interval") == 8  # 100 slots expected, 92 usable
    assert len(hourly) == 25
    # the two UTC hours of the repeated 01:00 local hour have no readings
    assert (hourly["samples_received"] == 0).sum() == 2


def test_utc_conversion_uses_daylight_time_in_summer():
    log = validate.QualityLog()
    text = "measured_on,ac_power__3\n2018-07-15 12:00:00,1000\n2018-01-15 12:00:00,1000\n"
    frame = transform.standardise_day(text, TEST_SYSTEM, date(2018, 7, 15), log)
    out = transform.to_utc(frame, TEST_SYSTEM, log)
    assert list(out["ts_utc"]) == [pd.Timestamp("2018-07-15 19:00"), pd.Timestamp("2018-01-15 20:00")]


def test_hourly_energy_is_mean_power_over_the_hour():
    log = validate.QualityLog()
    readings_w = {0: 4000, 15: 5000, 30: 6000, 45: 7000}
    rows = "\n".join(f"2018-07-15 10:{m:02d}:00,{w}" for m, w in readings_w.items())
    frame = transform.standardise_day("measured_on,ac_power__3\n" + rows + "\n", TEST_SYSTEM, date(2018, 7, 15), log)
    readings = transform.to_utc(frame, TEST_SYSTEM, log)
    grid = pd.DatetimeIndex([pd.Timestamp("2018-07-15 17:00")], name="ts_utc")
    hourly = transform.to_hourly(validate.apply_rules(readings, TEST_SYSTEM, log), TEST_SYSTEM, grid, log)
    assert hourly.loc[0, "ac_energy_kwh"] == pytest.approx(5.5)
    assert hourly.loc[0, "samples_valid"] == 4
    assert hourly.loc[0, "local_date"] == date(2018, 7, 15)


def test_weather_radiation_is_shifted_back_one_hour():
    grid = pd.date_range("2018-07-15 19:00", periods=2, freq="h", name="ts_utc")
    raw = pd.DataFrame({
        "ts_utc": pd.date_range("2018-07-15 19:00", periods=3, freq="h"),
        "temperature_2m": [30.0, 32.0, 34.0],
        "shortwave_radiation": [700.0, 800.0, 900.0],
        "cloud_cover": [0.0, 10.0, 20.0],
        "wind_speed_10m": [1.0, 2.0, 3.0],
    })
    w = transform.align_weather(raw, TEST_SYSTEM, grid)
    # hour 19:00-20:00 takes the value stamped 20:00 ("preceding hour mean")
    assert list(w["ghi_wm2"]) == [800.0, 900.0]
    # instant values: mean of the start and end of the hour
    assert list(w["air_temp_c"]) == [31.0, 33.0]
    assert list(w["cloud_cover_pct"]) == [5.0, 15.0]


def test_weather_gap_stays_empty():
    grid = pd.date_range("2018-07-15 19:00", periods=2, freq="h", name="ts_utc")
    raw = pd.DataFrame({"ts_utc": [pd.Timestamp("2018-07-15 19:00")], "temperature_2m": [30.0],
                        "shortwave_radiation": [700.0], "cloud_cover": [0.0], "wind_speed_10m": [1.0]})
    w = transform.align_weather(raw, TEST_SYSTEM, grid)
    assert np.isnan(w["ghi_wm2"]).all()


def test_five_minute_logging_uses_covered_time_not_reading_count():
    log = validate.QualityLog()
    # 12 five-minute readings in 10:00-10:59; only the first 6 have power (30 minutes covered)
    rows = [f"2018-07-15 10:{m:02d}:00,{1000 if m < 30 else ''}" for m in range(0, 60, 5)]
    rows += [f"2018-07-15 11:{m:02d}:00,{2000 + m}" for m in range(0, 60, 5)]
    text = "measured_on,ac_power__3\n" + "\n".join(rows) + "\n"
    frame = transform.standardise_day(text, TEST_SYSTEM, date(2018, 7, 15), log)
    readings = validate.apply_rules(transform.to_utc(frame, TEST_SYSTEM, log), TEST_SYSTEM, log)
    assert (readings["interval_min"] == 5).all()
    grid = pd.date_range("2018-07-15 17:00", periods=2, freq="h", name="ts_utc")
    hourly = transform.to_hourly(readings, TEST_SYSTEM, grid, log)
    assert hourly.loc[0, "samples_valid"] == 6 and pd.isna(hourly.loc[0, "ac_energy_kwh"])
    assert hourly.loc[1, "ac_energy_kwh"] == pytest.approx(2.0275)
    assert transform.expected_intervals(TZ, date(2018, 7, 15), 5) == 288
