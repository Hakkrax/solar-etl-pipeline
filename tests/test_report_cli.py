import json
import logging

from sqlalchemy import func, select

from conftest import MESSY_DAY, MISSING_DAY, SECOND_SYSTEM, TEST_SYSTEM, FakePvdaq, fake_weather
from solar_etl import cli, db, pipeline, quality, snapshot
from solar_etl.logs import JsonFormatter
from solar_etl.validate import RULES


def _loaded(engine):
    return pipeline.run_pipeline(engine, MESSY_DAY, MISSING_DAY, [TEST_SYSTEM, SECOND_SYSTEM], FakePvdaq(),
                                 fake_weather)


def test_report_lists_every_rule_and_system(engine, tmp_path):
    result = _loaded(engine)
    path = quality.write_report(engine, tmp_path / "reports", result.run_id)
    md = path.read_text(encoding="utf-8")
    for rule in RULES:
        assert f"`{rule.name}`" in md
    assert "9001 Test rooftop" in md and "9002 Test carport" in md
    assert "not a live feed" in md
    assert (tmp_path / "reports" / "figures" / "daily_energy.png").stat().st_size > 5000
    assert (tmp_path / "reports" / "figures" / "rule_counts.png").exists()
    summary = json.loads((tmp_path / "reports" / "quality_summary.json").read_text(encoding="utf-8"))
    assert summary["rules"]["frozen_power"] == 16
    assert summary["run"]["status"] == "success"
    first = summary["systems"][0]
    assert first["days"] == 4 and first["days_with_energy"] == 2  # missing day + rejected day have none


def test_snapshot_copies_tables_and_views(engine, tmp_path):
    _loaded(engine)
    out = snapshot.export_sqlite(engine, tmp_path / "snap.db")
    target = db.get_engine(f"sqlite:///{out.as_posix()}")
    with target.connect() as conn:
        assert conn.execute(select(func.count()).select_from(db.fact_pv_hourly)).scalar() == 192
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM v_daily_system").scalar() == 8
    assert snapshot.fingerprint(target) == snapshot.fingerprint(engine)
    target.dispose()


def test_cli_report_and_snapshot_commands(engine, tmp_path, capsys):
    _loaded(engine)
    url = str(engine.url.render_as_string(hide_password=False))
    assert cli.main(["--db", url, "--report-dir", str(tmp_path / "r"), "report"]) == 0
    assert (tmp_path / "r" / "quality_report.md").exists()
    assert cli.main(["--db", url, "snapshot", "--out", str(tmp_path / "s.db")]) == 0
    assert "s.db" in capsys.readouterr().out


def test_cli_rejects_unknown_system(tmp_path):
    import pytest

    with pytest.raises(SystemExit, match="unknown system"):
        cli._systems("1419,99999")


def test_json_log_lines_carry_context():
    record = logging.makeLogRecord({"name": "solar_etl.test", "levelname": "ERROR", "msg": "system failed",
                                    "run_id": "abc", "system_id": 1419})
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "system failed" and line["run_id"] == "abc" and line["system_id"] == 1419
    assert line["level"] == "ERROR" and line["ts"].endswith("+00:00")


def test_failures_are_logged_with_context(engine, caplog):
    def broken(lat, lon, start, end):
        raise TimeoutError("timed out")

    with caplog.at_level(logging.ERROR, logger="solar_etl"):
        pipeline.run_pipeline(engine, MESSY_DAY, MESSY_DAY, [TEST_SYSTEM], FakePvdaq(), broken)
    failed = [r for r in caplog.records if r.getMessage().startswith("system failed")]
    assert failed and failed[0].system_id == 9001 and failed[0].run_id
    assert failed[0].exc_info[0] is TimeoutError
