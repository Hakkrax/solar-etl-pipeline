"""Smoke test against the real services. Skipped by default; run with ``pytest -m live``."""

from datetime import date

import pytest

from solar_etl import pipeline
from solar_etl.config import SYSTEMS
from solar_etl.extract import OpenMeteoClient, PvdaqClient, make_session

pytestmark = pytest.mark.live


def test_one_real_day_loads(engine, tmp_path):
    session = make_session()
    result = pipeline.run_pipeline(
        engine, date(2018, 7, 15), date(2018, 7, 15), [SYSTEMS[1419]],
        PvdaqClient(tmp_path / "pvdaq", session=session), OpenMeteoClient(tmp_path / "om", session=session),
    )
    assert result.status == "success"
    assert result.rows_upserted == 48
