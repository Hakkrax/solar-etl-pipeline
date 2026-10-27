"""Database schema (SQLAlchemy Core). Works on SQLite and PostgreSQL."""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)

metadata = MetaData()

dim_system = Table(
    "dim_system", metadata,
    Column("system_id", Integer, primary_key=True, autoincrement=False),
    Column("name", String(120), nullable=False),
    Column("capacity_kw", Float, nullable=False),
    Column("dc_capacity_kw", Float, nullable=False),
    Column("latitude", Float, nullable=False),
    Column("longitude", Float, nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("tilt_deg", Float),
    Column("azimuth_deg", Float),
    Column("source", String(200), nullable=False),
)

fact_pv_hourly = Table(
    "fact_pv_hourly", metadata,
    Column("system_id", Integer, ForeignKey("dim_system.system_id"), primary_key=True, autoincrement=False),
    Column("ts_utc", DateTime, primary_key=True),  # hour start, UTC
    Column("local_date", Date, nullable=False, index=True),
    Column("ac_energy_kwh", Float),
    Column("ac_power_kw_mean", Float),
    Column("poa_irradiance_wm2", Float),
    Column("ambient_temp_c", Float),
    Column("module_temp_c", Float),
    Column("wind_speed_ms", Float),
    Column("samples_received", Integer, nullable=False),
    Column("samples_valid", Integer, nullable=False),
    Column("dq_flags", String(400)),
    Column("loaded_run_id", String(32), nullable=False),
    Column("loaded_at", DateTime, nullable=False),
)

weather_hourly = Table(
    "weather_hourly", metadata,
    Column("system_id", Integer, ForeignKey("dim_system.system_id"), primary_key=True, autoincrement=False),
    Column("ts_utc", DateTime, primary_key=True),  # hour start, UTC
    Column("ghi_wm2", Float),
    Column("air_temp_c", Float),
    Column("cloud_cover_pct", Float),
    Column("wind_speed_ms", Float),
    Column("loaded_run_id", String(32), nullable=False),
    Column("loaded_at", DateTime, nullable=False),
)

pipeline_runs = Table(
    "pipeline_runs", metadata,
    Column("run_id", String(32), primary_key=True),
    Column("started_at", DateTime, nullable=False),
    Column("finished_at", DateTime),
    Column("status", String(16), nullable=False),  # running | success | partial | failed
    Column("window_start", Date, nullable=False),
    Column("window_end", Date, nullable=False),
    Column("systems", String(200), nullable=False),
    Column("rows_upserted", Integer),
    Column("error_message", Text),
    Column("code_version", String(64)),
)

dq_results = Table(
    "dq_results", metadata,
    Column("run_id", String(32), ForeignKey("pipeline_runs.run_id"), primary_key=True),
    Column("system_id", Integer, primary_key=True, autoincrement=False),
    Column("local_date", Date, primary_key=True),
    Column("rule", String(64), primary_key=True),
    Column("severity", String(16), nullable=False),
    Column("rows_affected", Integer, nullable=False),
)

# Both pieces of energy and irradiance must be present for an hour to count
# towards the performance ratio, so missing sensor data does not bias it.
_PAIRED = "f.ac_energy_kwh IS NOT NULL AND f.poa_irradiance_wm2 IS NOT NULL"

VIEWS = {
    "v_pv_weather_hourly": """
        SELECT f.system_id, f.ts_utc, f.local_date, f.ac_energy_kwh, f.ac_power_kw_mean,
               f.poa_irradiance_wm2, f.ambient_temp_c, f.module_temp_c,
               f.samples_valid, f.dq_flags,
               w.ghi_wm2, w.air_temp_c, w.cloud_cover_pct, w.wind_speed_ms AS wind_speed_10m_ms
        FROM fact_pv_hourly f
        LEFT JOIN weather_hourly w ON w.system_id = f.system_id AND w.ts_utc = f.ts_utc
    """,
    "v_daily_system": f"""
        SELECT f.system_id,
               f.local_date,
               SUM(f.ac_energy_kwh) AS energy_kwh,
               SUM(f.ac_energy_kwh) / MAX(s.dc_capacity_kw) AS specific_yield_kwh_per_kwp,
               SUM(f.poa_irradiance_wm2) / 1000.0 AS poa_insolation_kwh_m2,
               SUM(w.ghi_wm2) / 1000.0 AS ghi_insolation_kwh_m2,
               SUM(CASE WHEN w.ghi_wm2 >= 20 THEN 1 ELSE 0 END) AS daylight_hours,
               SUM(CASE WHEN w.ghi_wm2 >= 20 AND f.ac_energy_kwh IS NOT NULL THEN 1 ELSE 0 END)
                   AS daylight_hours_with_energy,
               SUM(CASE WHEN {_PAIRED} THEN f.ac_energy_kwh END)
                   / NULLIF(MAX(s.dc_capacity_kw) * SUM(CASE WHEN {_PAIRED} THEN f.poa_irradiance_wm2 END) / 1000.0, 0)
                   AS performance_ratio,
               MAX(w.air_temp_c) AS max_air_temp_c,
               AVG(w.cloud_cover_pct) AS mean_cloud_cover_pct
        FROM fact_pv_hourly f
        JOIN dim_system s ON s.system_id = f.system_id
        LEFT JOIN weather_hourly w ON w.system_id = f.system_id AND w.ts_utc = f.ts_utc
        GROUP BY f.system_id, f.local_date
    """,
}


def get_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        from pathlib import Path

        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, future=True)


def init_db(engine: Engine) -> None:
    """Create tables if needed and (re)create the views."""
    metadata.create_all(engine)
    with engine.begin() as conn:
        for name, sql in VIEWS.items():
            conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
            conn.execute(text(f"CREATE VIEW {name} AS {sql}"))


def drop_all(engine: Engine) -> None:
    with engine.begin() as conn:
        for name in VIEWS:
            conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
    metadata.drop_all(engine)
