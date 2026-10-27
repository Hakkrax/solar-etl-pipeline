from datetime import date

import pytest
from sqlalchemy import func, select

from conftest import MESSY_DAY, MISSING_DAY, SECOND_SYSTEM, TEST_SYSTEM, FakePvdaq, fake_weather
from solar_etl import db, load, pipeline, snapshot

START, END = MESSY_DAY, MISSING_DAY  # 4 local days, 96 hours per system
SYSTEMS = [TEST_SYSTEM, SECOND_SYSTEM]


def _count(engine, table, **where):
    stmt = select(func.count()).select_from(table)
    for col, value in where.items():
        stmt = stmt.where(table.c[col] == value)
    with engine.connect() as conn:
        return conn.execute(stmt).scalar()


def _run(engine, fetch_weather=fake_weather, fetch_day=None):
    return pipeline.run_pipeline(engine, START, END, SYSTEMS, fetch_day or FakePvdaq(), fetch_weather, workers=4)


def test_full_run_loads_every_hour_and_records_the_run(engine):
    result = _run(engine)
    assert result.status == "success" and result.systems_ok == [9001, 9002]
    assert _count(engine, db.fact_pv_hourly) == 2 * 96
    assert _count(engine, db.weather_hourly) == 2 * 96
    assert result.rows_upserted == 4 * 96
    with engine.connect() as conn:
        run = conn.execute(select(db.pipeline_runs)).mappings().one()
    assert run["status"] == "success" and run["rows_upserted"] == 384 and run["finished_at"] is not None
    assert result.dq_totals["missing_file"] == 2  # one missing day per system
    assert result.dq_totals["frozen_power"] == 16


def test_running_twice_is_idempotent(engine):
    first = _run(engine)
    fp1 = snapshot.fingerprint(engine)
    second = _run(engine)
    fp2 = snapshot.fingerprint(engine)
    assert first.run_id != second.run_id
    assert fp1 == fp2  # same rows, same values
    assert fp1["fact_pv_hourly"]["rows"] == 192
    assert _count(engine, db.pipeline_runs) == 2  # the audit trail does grow
    # every fact row now carries the second run's id
    assert _count(engine, db.fact_pv_hourly, loaded_run_id=second.run_id) == 192


def test_overlapping_windows_do_not_duplicate(engine):
    pipeline.run_pipeline(engine, START, date(2018, 7, 16), SYSTEMS, FakePvdaq(), fake_weather)
    pipeline.run_pipeline(engine, date(2018, 7, 16), END, SYSTEMS, FakePvdaq(), fake_weather)
    assert _count(engine, db.fact_pv_hourly) == 2 * 96


def test_failed_weather_source_isolates_that_system(engine):
    def flaky_weather(lat, lon, start, end):
        if flaky_weather.calls == 1:
            raise ConnectionError("open-meteo unreachable")
        flaky_weather.calls += 1
        return fake_weather(lat, lon, start, end)

    flaky_weather.calls = 0
    result = _run(engine, fetch_weather=flaky_weather)
    assert result.status == "partial"
    assert result.systems_ok == [9001] and 9002 in result.systems_failed
    assert _count(engine, db.fact_pv_hourly, system_id=9001) == 96
    assert _count(engine, db.fact_pv_hourly, system_id=9002) == 0
    with engine.connect() as conn:
        run = conn.execute(select(db.pipeline_runs)).mappings().one()
    assert run["status"] == "partial" and "open-meteo unreachable" in run["error_message"]


def test_failure_during_load_rolls_back_the_whole_system(engine, monkeypatch):
    real_upsert = load.upsert

    def upsert_that_breaks_on_weather(conn, table, rows):
        if table is db.weather_hourly and rows and rows[0]["system_id"] == 9002:
            raise RuntimeError("disk full")
        return real_upsert(conn, table, rows)

    monkeypatch.setattr(load, "upsert", upsert_that_breaks_on_weather)
    result = _run(engine)
    assert result.status == "partial"
    # 9002's fact rows were written in the same transaction, then rolled back
    assert _count(engine, db.fact_pv_hourly, system_id=9002) == 0
    assert _count(engine, db.dq_results, system_id=9002) == 0
    assert _count(engine, db.fact_pv_hourly, system_id=9001) == 96


def test_earlier_good_data_survives_a_later_failed_run(engine):
    _run(engine)
    before = snapshot.fingerprint(engine)

    def broken(lat, lon, start, end):
        raise TimeoutError("timed out")

    result = _run(engine, fetch_weather=broken)
    assert result.status == "failed"
    assert snapshot.fingerprint(engine) == before


def test_quality_results_are_stored_per_day_and_rule(engine):
    result = _run(engine)
    with engine.connect() as conn:
        rows = conn.execute(select(db.dq_results).where(db.dq_results.c.run_id == result.run_id)
                            .where(db.dq_results.c.system_id == 9001)).mappings().all()
    by_key = {(r["local_date"], r["rule"]): r["rows_affected"] for r in rows}
    assert by_key[(MESSY_DAY, "frozen_power")] == 8
    assert by_key[(MISSING_DAY, "missing_file")] == 1
    assert by_key[(date(2018, 7, 17), "schema_missing_required")] == 1


def test_daily_view_matches_hourly_facts(engine):
    _run(engine)
    with engine.connect() as conn:
        daily = conn.execute(select(db.metadata.tables["fact_pv_hourly"].c.local_date,
                                    func.sum(db.fact_pv_hourly.c.ac_energy_kwh))
                             .where(db.fact_pv_hourly.c.system_id == 9001)
                             .group_by(db.fact_pv_hourly.c.local_date)).all()
        view = conn.exec_driver_sql(
            "SELECT local_date, energy_kwh, performance_ratio FROM v_daily_system WHERE system_id = 9001 "
            "ORDER BY local_date").all()
    assert len(view) == 4
    assert [round(v[1] or 0, 6) for v in view] == [round(d[1] or 0, 6) for d in sorted(daily)]
    clean_day = view[1]  # 2018-07-16: no row-level defects
    assert clean_day[2] == pytest.approx(0.8, abs=0.01)  # 8 kW per 1,000 W/m² on a 10 kWp system


def test_reversed_window_is_rejected(engine):
    with pytest.raises(ValueError):
        pipeline.run_pipeline(engine, END, START, SYSTEMS, FakePvdaq(), fake_weather)
