from datetime import date

import pandas as pd
import pytest

from conftest import (
    CH1,
    CH2,
    EXPECTED_MESSY,
    MESSY_DAY,
    NO_OPTIONAL_DAY,
    NO_POWER_DAY,
    TEST_SYSTEM,
    TOTAL,
    FakePvdaq,
    clean_rows,
    fake_weather,
    to_csv,
)
from solar_etl import pipeline, transform, validate


@pytest.fixture(scope="module")
def messy():
    log = validate.QualityLog()
    hourly, weather = pipeline.build_system(TEST_SYSTEM, MESSY_DAY, MESSY_DAY, FakePvdaq(), fake_weather, log)
    return log, hourly, weather


@pytest.mark.parametrize("rule,expected", sorted(EXPECTED_MESSY.items()))
def test_each_rule_fires_exactly_as_injected(messy, rule, expected):
    log, _, _ = messy
    assert log.count(rule, TEST_SYSTEM.system_id, MESSY_DAY) == expected


def test_messy_fixture_exercises_every_row_and_hour_rule(messy):
    """Guard against adding a rule without a fixture that triggers it."""
    file_level = {"missing_file", "schema_missing_required", "schema_missing_optional",
                  "dst_ambiguous", "dst_nonexistent", "unit_converted", "implausible_power_scale"}
    assert set(EXPECTED_MESSY) == {r.name for r in validate.RULES} - file_level
    log, _, _ = messy
    assert {k: v for k, v in log.totals().items()} == EXPECTED_MESSY


def test_rules_are_unique_and_documented():
    names = [r.name for r in validate.RULES]
    assert len(names) == len(set(names))
    assert all(r.severity in {"info", "warning", "error"} and r.description and r.action for r in validate.RULES)


def test_unknown_rule_name_is_rejected():
    with pytest.raises(KeyError):
        validate.QualityLog().add(1, date(2020, 1, 1), "not_a_rule")


def test_fixes_are_applied_not_just_counted(messy):
    _, hourly, _ = messy
    by_local = hourly.assign(local_hour=(hourly["ts_utc"] - pd.Timedelta(hours=7)).dt.hour).set_index("local_hour")
    # night_power at 22:00 and 23:00 is set to zero, so night energy is exactly 0
    assert by_local.loc[22, "ac_energy_kwh"] == 0.0
    assert by_local.loc[23, "ac_energy_kwh"] == 0.0
    # frozen power at 13:00-14:45 is removed, so those hours have no trusted energy
    assert by_local.loc[[13, 14], "ac_energy_kwh"].isna().all()
    assert by_local.loc[13, "samples_valid"] == 0
    # frozen irradiance 16:00-17:45 removed
    assert by_local.loc[[16, 17], "poa_irradiance_wm2"].isna().all()
    # out-of-range ambient at 09:00 dropped, the other three readings remain
    assert 20 < by_local.loc[9, "ambient_temp_c"] < 40
    # 12:00 above capacity removed; the hour still has 3 valid readings
    assert by_local.loc[12, "samples_valid"] == 3
    assert by_local.loc[12, "ac_power_kw_mean"] < 10


def test_flags_are_attached_to_the_affected_hours(messy):
    _, hourly, _ = messy
    flags = hourly.set_index(hourly["ts_utc"] - pd.Timedelta(hours=7))["dq_flags"]
    assert "night_power" in flags[pd.Timestamp("2018-07-15 22:00")]
    assert "frozen_power" in flags[pd.Timestamp("2018-07-15 13:00")]
    assert "low_coverage_hour" in flags[pd.Timestamp("2018-07-15 13:00")]
    assert "power_from_channels" in flags[pd.Timestamp("2018-07-15 08:00")]
    assert flags[pd.Timestamp("2018-07-15 05:00")] is None  # untouched night hour


def test_missing_optional_columns_are_reported_not_fatal():
    log = validate.QualityLog()
    hourly, _ = pipeline.build_system(TEST_SYSTEM, NO_OPTIONAL_DAY, NO_OPTIONAL_DAY, FakePvdaq(), fake_weather, log)
    assert log.count("schema_missing_optional") == 2
    assert hourly["module_temp_c"].isna().all() and hourly["wind_speed_ms"].isna().all()
    assert hourly["ac_energy_kwh"].notna().all()


def test_missing_power_column_rejects_the_day():
    log = validate.QualityLog()
    hourly, _ = pipeline.build_system(TEST_SYSTEM, NO_POWER_DAY, NO_POWER_DAY, FakePvdaq(), fake_weather, log)
    assert log.count("schema_missing_required") == 1
    assert hourly["ac_energy_kwh"].isna().all()
    assert log.count("low_coverage_hour") == 24


def test_check_schema_accepts_channels_without_total():
    cols = ["measured_on", *TEST_SYSTEM.ac_power_channels]
    usable, missing = validate.check_schema(cols, TEST_SYSTEM)
    assert usable and len(missing) == 4


def test_frozen_ignores_zero_runs_and_short_runs():
    zeros = pd.Series([0.0] * 20)
    short = pd.Series([5.0] * 7 + [6.0])
    assert not validate._frozen(zeros).any()
    assert not validate._frozen(short).any()
    assert validate._frozen(pd.Series([1.0, *[5.0] * 8, 2.0])).sum() == 8
    # at 5-minute logging, 8 identical readings are only 40 minutes: not frozen; 24 are 2 hours
    assert not validate._frozen(pd.Series([5.0] * 8), 5).any()
    assert validate._frozen(pd.Series([5.0] * 24), 5).all()


def test_unit_fix_converts_only_before_the_switch_date():
    from dataclasses import replace

    system = replace(TEST_SYSTEM, unit_fixes=(("module_temp_c", "degF", date(2018, 7, 16)),))
    text = "measured_on,ac_power__3,module_temp_f_temp1__6\n2018-07-15 12:00:00,1000,212\n"
    log = validate.QualityLog()
    before = transform.standardise_day(text, system, date(2018, 7, 15), log)
    after = transform.standardise_day(text.replace("07-15", "07-16"), system, date(2018, 7, 16), log)
    assert before["module_temp_c"].iloc[0] == pytest.approx(100.0)
    assert after["module_temp_c"].iloc[0] == 212.0  # on/after the switch date: left alone (then range-checked)
    assert log.count("unit_converted") == 1


def test_unit_fixes_convert_kw_channels_and_mph():
    from dataclasses import replace

    switch = date(2018, 7, 16)
    system = replace(TEST_SYSTEM, unit_fixes=(("channels", "kW", switch), ("ac_power_w", "kW", switch),
                                              ("wind_speed_ms", "mph", switch)))
    header = "measured_on,ac_power_1_kwac__1,ac_power_2_kwac__2,ac_power__3,wind_speed_mph_windspeed__7"
    text = header + chr(10) + "2018-07-15 12:00:00,2.5,2.5,5,10" + chr(10)
    log = validate.QualityLog()
    out = transform.standardise_day(text, system, date(2018, 7, 15), log)
    assert list(out[["channel_0", "channel_1", "ac_power_w"]].iloc[0]) == [2500.0, 2500.0, 5000.0]
    assert out["wind_speed_ms"].iloc[0] == pytest.approx(4.4704)
    assert log.count("unit_converted") == 4


def test_power_in_wrong_unit_is_caught_even_without_a_known_fix():
    """A sunny day whose power peaks at 8 'W' on a 10 kW system: blank the day, don't report ~0 kWh."""
    rows = clean_rows(date(2018, 7, 16))
    for r in rows:
        r[CH1] = r[CH2] = None
        r[TOTAL] = round(r[TOTAL] / 1000, 3)  # kW stored where W is expected
    log = validate.QualityLog()
    fake = FakePvdaq({date(2018, 7, 16): to_csv(rows)})
    hourly, _ = pipeline.build_system(TEST_SYSTEM, date(2018, 7, 16), date(2018, 7, 16), fake, fake_weather, log)
    assert log.count("implausible_power_scale") == 96
    assert hourly["ac_energy_kwh"].isna().all()
