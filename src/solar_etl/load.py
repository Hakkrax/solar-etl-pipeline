"""Idempotent loading: insert new rows, update existing ones on the primary key."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import Connection, Table

CHUNK_ROWS = 2000


def _insert_for(dialect: str):
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover - only the two tested dialects are supported
        raise NotImplementedError(f"upsert not implemented for {dialect}")
    return insert


def _clean(value):
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def to_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list of dicts with plain Python values and None for gaps."""
    columns = {col: [_clean(v) for v in df[col].tolist()] for col in df.columns}
    return [dict(zip(columns, row, strict=True)) for row in zip(*columns.values(), strict=True)]


def upsert(conn: Connection, table: Table, rows: list[dict]) -> int:
    """Insert or update ``rows`` in ``table`` keyed on its primary key."""
    if not rows:
        return 0
    keys = [c.name for c in table.primary_key.columns]
    insert = _insert_for(conn.dialect.name)
    for start in range(0, len(rows), CHUNK_ROWS):
        stmt = insert(table)
        updates = {c.name: stmt.excluded[c.name] for c in table.columns if c.name not in keys}
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=updates)
        conn.execute(stmt, rows[start:start + CHUNK_ROWS])
    return len(rows)


def utcnow() -> datetime:
    """Naive UTC timestamp, matching how ts_utc columns are stored."""
    return datetime.now(UTC).replace(tzinfo=None)
