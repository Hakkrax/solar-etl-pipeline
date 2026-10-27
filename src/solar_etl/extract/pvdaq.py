"""Download daily PVDAQ CSV files from the public OEDI S3 bucket, with a raw cache."""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

import requests

from solar_etl.config import PVDAQ_BASE_URL
from solar_etl.extract.http import DEFAULT_TIMEOUT, make_session

log = logging.getLogger(__name__)


class PvdaqClient:
    """Fetch one system-day at a time. Files never change, so cached copies are reused.

    A 404 means PVDAQ has no file for that day; it is cached as a ``.missing``
    marker and reported as a data gap, not an error.
    """

    def __init__(
        self,
        cache_dir: Path,
        session: requests.Session | None = None,
        base_url: str = PVDAQ_BASE_URL,
        refresh: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.session = session or make_session()
        self.base_url = base_url.rstrip("/")
        self.refresh = refresh
        self.timeout = timeout

    def url(self, system_id: int, day: date) -> str:
        return (
            f"{self.base_url}/system_id={system_id}/year={day.year}/month={day.month}/day={day.day}/"
            f"system_{system_id}__date_{day:%Y_%m_%d}.csv"
        )

    def _cache_path(self, system_id: int, day: date) -> Path:
        return self.cache_dir / f"system_id={system_id}" / f"{day.isoformat()}.csv"

    def __call__(self, system_id: int, day: date) -> str | None:
        path = self._cache_path(system_id, day)
        marker = path.with_suffix(".missing")
        if not self.refresh:
            if path.exists():
                return path.read_text(encoding="utf-8")
            if marker.exists():
                return None

        url = self.url(system_id, day)
        started = time.perf_counter()
        resp = self.session.get(url, timeout=self.timeout)
        ctx = {"system_id": system_id, "day": day.isoformat(), "status": resp.status_code,
               "duration_ms": round((time.perf_counter() - started) * 1000)}
        path.parent.mkdir(parents=True, exist_ok=True)
        if resp.status_code == 404:
            marker.touch()
            log.info("pvdaq day missing", extra=ctx)
            return None
        resp.raise_for_status()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(resp.text, encoding="utf-8")
        tmp.replace(path)
        marker.unlink(missing_ok=True)
        log.debug("pvdaq day downloaded", extra={**ctx, "bytes": len(resp.content)})
        return resp.text
