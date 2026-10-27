"""Hourly historical weather from the Open-Meteo archive API (no key needed)."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from solar_etl.config import OPEN_METEO_URL
from solar_etl.extract.http import DEFAULT_TIMEOUT, SourceError, make_session

log = logging.getLogger(__name__)

HOURLY_VARIABLES = {
    # API variable -> expected unit
    "temperature_2m": "°C",
    "shortwave_radiation": "W/m²",
    "cloud_cover": "%",
    "wind_speed_10m": "m/s",
}


class OpenMeteoClient:
    def __init__(
        self,
        cache_dir: Path,
        session: requests.Session | None = None,
        url: str = OPEN_METEO_URL,
        refresh: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.session = session or make_session()
        self.url = url
        self.refresh = refresh
        self.timeout = timeout

    def _cache_path(self, lat: float, lon: float, start: date, end: date) -> Path:
        return self.cache_dir / f"{lat:.4f}_{lon:.4f}_{start}_{end}.json"

    def __call__(self, lat: float, lon: float, start: date, end: date) -> pd.DataFrame:
        path = self._cache_path(lat, lon, start, end)
        if path.exists() and not self.refresh:
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            params = {
                "latitude": lat,
                "longitude": lon,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "hourly": ",".join(HOURLY_VARIABLES),
                "timezone": "GMT",
                "wind_speed_unit": "ms",
            }
            resp = self.session.get(self.url, params=params, timeout=self.timeout)
            if resp.status_code == 400:
                raise SourceError(f"open-meteo rejected request: {resp.text[:200]}")
            resp.raise_for_status()
            payload = resp.json()
            parse_payload(payload)  # validate before caching
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            log.info("open-meteo downloaded", extra={"lat": lat, "lon": lon, "start": str(start), "end": str(end)})
        return parse_payload(payload)


def parse_payload(payload: dict) -> pd.DataFrame:
    """Check the response shape and units, then return one row per UTC hour."""
    if payload.get("error"):
        raise SourceError(f"open-meteo error: {payload.get('reason')}")
    hourly = payload.get("hourly")
    units = payload.get("hourly_units", {})
    if not isinstance(hourly, dict) or "time" not in hourly:
        raise SourceError("open-meteo response has no hourly block")
    for var, unit in HOURLY_VARIABLES.items():
        if var not in hourly:
            raise SourceError(f"open-meteo response is missing {var}")
        if units.get(var) != unit:
            raise SourceError(f"open-meteo unit for {var} is {units.get(var)!r}, expected {unit!r}")
        if len(hourly[var]) != len(hourly["time"]):
            raise SourceError(f"open-meteo {var} length does not match time axis")
    df = pd.DataFrame({var: hourly[var] for var in HOURLY_VARIABLES}, dtype="float64")
    df.insert(0, "ts_utc", pd.to_datetime(hourly["time"], format="%Y-%m-%dT%H:%M"))
    return df
