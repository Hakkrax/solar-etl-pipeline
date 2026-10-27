"""Export a portable SQLite snapshot and fingerprint tables for idempotency checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, select

from solar_etl import db, load

# Columns that legitimately change on every run (lineage), excluded from fingerprints.
LINEAGE_COLUMNS = {"loaded_run_id", "loaded_at"}
DATA_TABLES = (db.dim_system, db.fact_pv_hourly, db.weather_hourly)


def fingerprint(engine: Engine) -> dict[str, dict]:
    """Row count and content hash per data table, ignoring lineage columns."""
    out = {}
    with engine.connect() as conn:
        for table in DATA_TABLES:
            cols = [c for c in table.columns if c.name not in LINEAGE_COLUMNS]
            keys = list(table.primary_key.columns)
            df = pd.read_sql(select(*cols).order_by(*keys), conn)
            digest = hashlib.sha256(df.to_csv(index=False, float_format="%.6f").encode()).hexdigest()
            out[table.name] = {"rows": int(len(df)), "sha256": digest}
    return out


def export_sqlite(source: Engine, out_path: Path) -> Path:
    """Copy all tables into a fresh SQLite file (with the same schema and views)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    target = db.get_engine(f"sqlite:///{out_path.as_posix()}")
    db.init_db(target)
    with source.connect() as src, target.begin() as dst:
        for table in db.metadata.sorted_tables:
            rows = [dict(r) for r in src.execute(select(table)).mappings()]
            load.upsert(dst, table, rows)
    target.dispose()
    return out_path
