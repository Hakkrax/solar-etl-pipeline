"""Command line: ``solar-etl run | report | snapshot | canary``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from solar_etl import config, db, pipeline, quality, snapshot
from solar_etl.extract import OpenMeteoClient, PvdaqClient, make_session
from solar_etl.logs import setup_logging

log = logging.getLogger("solar_etl.cli")


def _systems(arg: str | None) -> list[config.System]:
    ids = [int(x) for x in arg.split(",")] if arg else list(config.DEFAULT_SYSTEM_IDS)
    unknown = [i for i in ids if i not in config.SYSTEMS]
    if unknown:
        raise SystemExit(f"unknown system id(s): {unknown}; configured: {list(config.SYSTEMS)}")
    return [config.SYSTEMS[i] for i in ids]


def _run(args: argparse.Namespace, engine) -> pipeline.RunResult:
    session = make_session()
    raw = Path(args.raw_dir)
    return pipeline.run_pipeline(
        engine, args.start, args.end, _systems(args.systems),
        fetch_day=PvdaqClient(raw / "pvdaq", session=session, refresh=args.refresh),
        fetch_weather=OpenMeteoClient(raw / "open_meteo", session=session, refresh=args.refresh),
        workers=args.workers,
    )


def cmd_run(args: argparse.Namespace) -> int:
    engine = db.get_engine(args.db)
    result = _run(args, engine)
    report = quality.write_report(engine, Path(args.report_dir), result.run_id)
    print(json.dumps({"run_id": result.run_id, "status": result.status, "rows_upserted": result.rows_upserted,
                      "failed": result.systems_failed, "report": str(report)}, indent=2))
    return 0 if result.status == "success" else 1


def cmd_report(args: argparse.Namespace) -> int:
    report = quality.write_report(db.get_engine(args.db), Path(args.report_dir), args.run_id)
    print(report)
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    out = snapshot.export_sqlite(db.get_engine(args.db), Path(args.out))
    print(f"{out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


def cmd_canary(args: argparse.Namespace) -> int:
    """Run the same window twice against the live sources and check nothing changed."""
    engine = db.get_engine(args.db)
    first = _run(args, engine)
    fp1 = snapshot.fingerprint(engine)
    second = _run(args, engine)
    fp2 = snapshot.fingerprint(engine)
    report = quality.write_report(engine, Path(args.report_dir), second.run_id)
    ok = first.status == second.status == "success" and fp1 == fp2
    summary = {"ok": ok, "runs": [first.run_id, second.run_id], "statuses": [first.status, second.status],
               "fingerprint_first": fp1, "fingerprint_second": fp2, "report": str(report)}
    Path(args.report_dir, "canary_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if ok else 1


def _date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="solar-etl", description=__doc__)
    p.add_argument("--db", default=config.db_url(), help="SQLAlchemy URL (env SOLAR_ETL_DB_URL)")
    p.add_argument("--report-dir", default=str(config.REPORT_DIR))
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--log-file", type=Path, default=None)
    sub = p.add_subparsers(dest="command", required=True)

    for name, func, helptext in (("run", cmd_run, "extract, validate, transform and load a date window"),
                                 ("canary", cmd_canary, "run a window twice and verify idempotency")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--start", type=_date, required=True, help="first local date (YYYY-MM-DD)")
        s.add_argument("--end", type=_date, required=True, help="last local date (YYYY-MM-DD)")
        s.add_argument("--systems", help="comma-separated system ids (default: all configured)")
        s.add_argument("--raw-dir", default=str(config.RAW_DIR))
        s.add_argument("--refresh", action="store_true", help="ignore the raw cache and download again")
        s.add_argument("--workers", type=int, default=8)
        s.set_defaults(func=func)

    s = sub.add_parser("report", help="rebuild the quality report for a run")
    s.add_argument("--run-id", default=None, help="default: latest run")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("snapshot", help="export all tables to a SQLite file")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_snapshot)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_file)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
