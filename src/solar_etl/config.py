"""Settings, source URLs and the PV systems this pipeline loads.

Column names in PVDAQ files are misleading (``ac_power_1_kwac`` holds watts,
``ambient_temp_f`` holds degrees Celsius). The mappings below follow NREL's
own metrics table (``pvdaq/parquet/metrics``), which lists the stored unit of
each column, not the column names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("SOLAR_ETL_DATA_DIR", PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
REPORT_DIR = PROJECT_ROOT / "reports"
DEFAULT_DB_URL = f"sqlite:///{(DATA_DIR / 'solar.db').as_posix()}"

PVDAQ_BASE_URL = "https://oedi-data-lake.s3.amazonaws.com/pvdaq/csv/pvdata"
OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
USER_AGENT = "solar-etl-pipeline/0.1 (+https://github.com/Hakkrax/solar-etl-pipeline)"

INTERVAL_MINUTES = 15  # default logging interval; each day's actual interval is detected
MIN_COVERED_MINUTES = 45  # valid readings must cover this much of an hour, else energy is left empty
DAYLIGHT_GHI_WM2 = 20.0  # hours with modelled GHI at or above this count as daylight


def db_url() -> str:
    return os.environ.get("SOLAR_ETL_DB_URL", DEFAULT_DB_URL)


@dataclass(frozen=True)
class System:
    system_id: int
    name: str
    capacity_kw: float  # rated power from PVDAQ system metadata (AC for 1420)
    latitude: float
    longitude: float
    timezone: str
    tilt_deg: float
    azimuth_deg: float
    # canonical field -> PVDAQ column. ac_power_w is required, the rest optional.
    columns: dict[str, str] = field(default_factory=dict)
    # per-inverter AC power columns, used when the system total is empty
    ac_power_channels: tuple[str, ...] = ()
    # module count x module rating from the PVDAQ module list; used for yield and PR
    dc_capacity_kw: float = 0.0
    # (field, source unit, first local date already in the canonical unit): source
    # values before that date are converted. "channels" means every per-inverter
    # AC power column. Found by profiling the data, see docs/DESIGN.md.
    unit_fixes: tuple[tuple[str, str, date], ...] = ()


# PVDAQ changed logging for these systems on 29 March 2017: 5-minute -> 15-minute
# readings, and several columns moved to the units the metrics table lists.
UNIT_SWITCH = date(2017, 3, 29)


SYSTEMS: dict[int, System] = {
    1418: System(
        1418, "Clark County NV - Desert Breeze", 42.0, 36.1253, -115.2713,
        "America/Los_Angeles", 10.0, 138.0,
        columns={
            "ac_power_w": "ac_power__5048",
            "poa_irradiance_wm2": "poa_irradiance_sun__4785",
            "ambient_temp_c": "ambient_temp_f_tempf__4792",
            "module_temp_c": "module_temp_f_temp2__4793",
            "wind_speed_ms": "wind_speed_mph_windspeed__4794",
        },
        ac_power_channels=("ac_power_1_kwac__4786", "ac_power_2_kwac__4788", "ac_power_3_kwac__4790"),
        dc_capacity_kw=43.68,  # 156 x SolarWorld SW280 mono
        unit_fixes=(("channels", "kW", UNIT_SWITCH), ("wind_speed_ms", "mph", UNIT_SWITCH)),
    ),
    1419: System(
        1419, "Clark County NV - Hollywood Rec Center", 40.0, 36.1534, -115.0256,
        "America/Los_Angeles", 10.0, 210.0,
        columns={
            "ac_power_w": "ac_power__4808",
            "poa_irradiance_wm2": "poa_irradiance_sun__4796",
            "ambient_temp_c": "ambient_temp_f_tempf__4803",
            "wind_speed_ms": "wind_speed_mph_windspeed__4804",
        },
        ac_power_channels=("ac_power_1_kwac__4797", "ac_power_2_kwac__4799"),
        dc_capacity_kw=43.68,  # 156 x SolarWorld SW280 mono
        unit_fixes=(("channels", "kW", UNIT_SWITCH), ("wind_speed_ms", "mph", UNIT_SWITCH)),
    ),
    1420: System(
        1420, "Clark County NV - Government Center", 30.0, 36.1654, -115.1534,
        "America/Los_Angeles", 10.0, 180.0,
        columns={
            "ac_power_w": "ac_power_kwac__4812",
            "poa_irradiance_wm2": "poa_irradiance_sun__4810",
            "module_temp_c": "module_temp_f_temp1__4811",
        },
        dc_capacity_kw=35.84,  # 112 x SunPower SPR-320E
        # AC power in kW and module temperature in °F until 28 Mar 2017
        unit_fixes=(("ac_power_w", "kW", UNIT_SWITCH), ("module_temp_c", "degF", UNIT_SWITCH)),
    ),
}

DEFAULT_SYSTEM_IDS = tuple(SYSTEMS)
