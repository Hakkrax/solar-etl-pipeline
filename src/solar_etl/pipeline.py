"""Run the pipeline: extract -> validate -> transform -> load, one system at a time.

Each system is loaded in its own transaction. If anything fails for a system
(download, parsing, weather, database), that system's changes are rolled back,
the error is logged with context, and the other systems still load. The run
is then marked ``partial``.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import Engine, update

from solar_etl import db, load, transform, validate
from solar_etl.config import System

log = logging.getLogger(__name__)

DayFetcher = Callable[[int, date], "str | None"]
WeatherFetcher = Callable[[float, float, date, date], pd.DataFrame]

SOURCE_LABEL = "NREL PVDAQ (OEDI, CC BY 4.0) + Open-Meteo archive (CC BY 4.0)"


@dataclass
class RunResult:
    run_id: str
    status: str
    rows_upserted: int = 0
    systems_ok: list[int] = field(default_factory=list)
    systems_failed: dict[int, str] = field(default_factory=dict)
    dq_totals: dict[str, int] = field(default_factory=dict)


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def build_system(
    system: System,
    start: date,
    end: date,
    fetch_day: DayFetcher,
    fetch_weather: WeatherFetcher,
    quality: validate.QualityLog,
    workers: int = 8,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (hourly PV facts, aligned weather) for one system and date window."""
    days = days_between(start, end)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        texts = list(pool.map(lambda d: fetch_day(system.system_id, d), days))

    frames = []
    for day, text in zip(days, texts, strict=True):
        if text is None:
            quality.add(system.system_id, day, "missing_file")
            continue
        frame = transform.standardise_day(text, system, day, quality)
        if frame is not None and not frame.empty:
            frames.append(frame)

    grid = transform.utc_hour_grid(system.timezone, start, end)
    if frames:
        readings = transform.to_utc(pd.concat(frames, ignore_index=True), system, quality)
        readings = validate.apply_rules(readings, system, quality)
    else:
        readings = pd.DataFrame(columns=["source_date", "ts_utc", *validate.NUMERIC_FIELDS])
        readings["ts_utc"] = pd.to_datetime(readings["ts_utc"])
    hourly = transform.to_hourly(readings, system, grid, quality)

    # one extra day so the last hour has its "next hour" weather value
    weather_raw = fetch_weather(system.latitude, system.longitude, grid[0].date(), (grid[-1] + transform.HOUR).date())
    weather = transform.align_weather(weather_raw, system, grid)
    return hourly, weather


def _system_row(system: System) -> dict:
    return {
        "system_id": system.system_id, "name": system.name, "capacity_kw": system.capacity_kw,
        "dc_capacity_kw": system.dc_capacity_kw,
        "latitude": system.latitude, "longitude": system.longitude, "timezone": system.timezone,
        "tilt_deg": system.tilt_deg, "azimuth_deg": system.azimuth_deg, "source": SOURCE_LABEL,
    }


def run_pipeline(
    engine: Engine,
    start: date,
    end: date,
    systems: Iterable[System],
    fetch_day: DayFetcher,
    fetch_weather: WeatherFetcher,
    workers: int = 8,
    run_id: str | None = None,
) -> RunResult:
    if end < start:
        raise ValueError("end date is before start date")
    systems = list(systems)
    run_id = run_id or uuid.uuid4().hex[:12]
    result = RunResult(run_id=run_id, status="running")
    ctx = {"run_id": run_id, "window_start": str(start), "window_end": str(end)}

    db.init_db(engine)
    with engine.begin() as conn:
        conn.execute(db.pipeline_runs.insert().values(
            run_id=run_id, started_at=load.utcnow(), status="running", window_start=start, window_end=end,
            systems=",".join(str(s.system_id) for s in systems),
            code_version=os.environ.get("GITHUB_SHA", "local")[:64],
        ))
        load.upsert(conn, db.dim_system, [_system_row(s) for s in systems])
    log.info("run started", extra={**ctx, "systems": [s.system_id for s in systems]})

    totals: dict[str, int] = {}
    for system in systems:
        sctx = {**ctx, "system_id": system.system_id}
        started = time.perf_counter()
        quality = validate.QualityLog()
        try:
            hourly, weather = build_system(system, start, end, fetch_day, fetch_weather, quality, workers)
            loaded_at = load.utcnow()
            hourly = hourly.assign(loaded_run_id=run_id, loaded_at=loaded_at)
            weather = weather.assign(loaded_run_id=run_id, loaded_at=loaded_at)
            with engine.begin() as conn:  # one transaction per system
                rows = load.upsert(conn, db.fact_pv_hourly, load.to_records(hourly))
                rows += load.upsert(conn, db.weather_hourly, load.to_records(weather))
                load.upsert(conn, db.dq_results, quality.records(run_id))
        except Exception as exc:  # noqa: BLE001 - one bad system must not stop the others
            result.systems_failed[system.system_id] = f"{type(exc).__name__}: {exc}"[:500]
            log.exception("system failed; its changes were rolled back", extra=sctx)
            continue
        result.rows_upserted += rows
        result.systems_ok.append(system.system_id)
        for rule, n in quality.totals().items():
            totals[rule] = totals.get(rule, 0) + n
        log.info("system loaded", extra={**sctx, "rows_upserted": rows, "dq": quality.totals(),
                                         "duration_ms": round((time.perf_counter() - started) * 1000)})

    result.dq_totals = totals
    if not result.systems_failed:
        result.status = "success"
    else:
        result.status = "partial" if result.systems_ok else "failed"
    errors = "; ".join(f"{sid}: {msg}" for sid, msg in result.systems_failed.items()) or None
    with engine.begin() as conn:
        conn.execute(update(db.pipeline_runs).where(db.pipeline_runs.c.run_id == run_id).values(
            finished_at=load.utcnow(), status=result.status, rows_upserted=result.rows_upserted,
            error_message=errors,
        ))
    log.info("run finished", extra={**ctx, "status": result.status, "rows_upserted": result.rows_upserted,
                                    "failed": result.systems_failed})
    return result
