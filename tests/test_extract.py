from datetime import date

import pytest
import requests
import responses

from solar_etl.extract import OpenMeteoClient, PvdaqClient, SourceError, make_session
from solar_etl.extract.open_meteo import parse_payload

DAY = date(2018, 7, 15)
CSV = "measured_on,ac_power__4808,system_id\n2018-07-15 12:00:00,1000,1419\n"


def _weather_payload(hours: int = 2) -> dict:
    return {
        "hourly_units": {"time": "iso8601", "temperature_2m": "°C", "shortwave_radiation": "W/m²",
                         "cloud_cover": "%", "wind_speed_10m": "m/s"},
        "hourly": {
            "time": [f"2018-07-15T{h:02d}:00" for h in range(hours)],
            "temperature_2m": [30.0] * hours, "shortwave_radiation": [0.0] * hours,
            "cloud_cover": [5] * hours, "wind_speed_10m": [1.5] * hours,
        },
    }


@pytest.fixture
def session():
    return make_session(retries=2, backoff=0)


def test_pvdaq_url_matches_bucket_layout(tmp_path, session):
    client = PvdaqClient(tmp_path, session=session)
    assert client.url(1419, date(2018, 3, 1)).endswith(
        "/pvdata/system_id=1419/year=2018/month=3/day=1/system_1419__date_2018_03_01.csv")


@responses.activate
def test_pvdaq_downloads_once_then_uses_cache(tmp_path, session):
    client = PvdaqClient(tmp_path, session=session)
    responses.get(client.url(1419, DAY), body=CSV)
    assert client(1419, DAY) == CSV
    assert client(1419, DAY) == CSV
    assert len(responses.calls) == 1
    assert (tmp_path / "system_id=1419" / "2018-07-15.csv").exists()


@responses.activate
def test_pvdaq_404_is_a_gap_not_an_error(tmp_path, session):
    client = PvdaqClient(tmp_path, session=session)
    responses.get(client.url(1419, DAY), status=404)
    assert client(1419, DAY) is None
    assert client(1419, DAY) is None  # marker cached
    assert len(responses.calls) == 1


@responses.activate
def test_pvdaq_retries_server_errors(tmp_path, session):
    client = PvdaqClient(tmp_path, session=session)
    url = client.url(1419, DAY)
    responses.get(url, status=503)
    responses.get(url, body=CSV)
    assert client(1419, DAY) == CSV
    assert len(responses.calls) == 2


@responses.activate
def test_pvdaq_gives_up_after_retries(tmp_path, session):
    client = PvdaqClient(tmp_path, session=session)
    responses.get(client.url(1419, DAY), status=503)
    with pytest.raises(requests.HTTPError):
        client(1419, DAY)
    assert not (tmp_path / "system_id=1419" / "2018-07-15.csv").exists()


@responses.activate
def test_open_meteo_parses_and_caches(tmp_path, session):
    client = OpenMeteoClient(tmp_path, session=session)
    responses.get(client.url, json=_weather_payload())
    df = client(36.15, -115.03, DAY, DAY)
    client(36.15, -115.03, DAY, DAY)
    assert list(df.columns) == ["ts_utc", "temperature_2m", "shortwave_radiation", "cloud_cover", "wind_speed_10m"]
    assert len(df) == 2 and len(responses.calls) == 1
    sent = responses.calls[0].request.url
    assert "timezone=GMT" in sent and "wind_speed_unit=ms" in sent


@responses.activate
def test_open_meteo_bad_response_is_not_cached(tmp_path, session):
    client = OpenMeteoClient(tmp_path, session=session)
    bad = _weather_payload()
    del bad["hourly"]["shortwave_radiation"]
    responses.get(client.url, json=bad)
    with pytest.raises(SourceError):
        client(36.15, -115.03, DAY, DAY)
    assert not list(tmp_path.glob("*.json"))


def test_open_meteo_unit_change_is_detected():
    payload = _weather_payload()
    payload["hourly_units"]["temperature_2m"] = "°F"
    with pytest.raises(SourceError, match="unit"):
        parse_payload(payload)


def test_open_meteo_api_error_is_reported():
    with pytest.raises(SourceError, match="out of range"):
        parse_payload({"error": True, "reason": "start_date out of range"})
