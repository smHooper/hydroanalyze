"""
Shared fixtures for the hydroanalyze test suite.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

# The package modules import each other as top-level modules
# (``from metadata import ...``), so put the package directory on sys.path.
PACKAGE_DIR = Path(__file__).resolve().parents[1] / 'hydroanalyze'
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

from analyze import RiverData  # noqa: E402

WATERML_NS = 'http://www.cuahsi.org/waterML/1.1/'


# ----------------------------------------------------------------------
# WaterML 1.1 builder
# ----------------------------------------------------------------------
def build_waterml(
    series: list[dict],
    site_code: str | None = '15356000',
    site_name: str = 'TEST RIVER NEAR PLACE',
    include_site: bool = True,
) -> bytes:
    """
    Build a minimal WaterML 1.1 document.

    Each item in ``series`` is a dict with keys: code, name, stat, nodata,
    values (list of (iso_datetime, value) pairs). Use ``daily_series``.
    """
    site_code_xml = (
        f'<ns1:siteCode network="NWIS" agencyCode="USGS">{site_code}</ns1:siteCode>'
        if site_code
        else ''
    )
    source_info = (
        '<ns1:sourceInfo>'
        f'<ns1:siteName>{site_name}</ns1:siteName>'
        f'{site_code_xml}'
        '<ns1:timeZoneInfo siteUsesDaylightSavingsTime="true">'
        '<ns1:defaultTimeZone zoneOffset="-09:00" zoneAbbreviation="AKST"/>'
        '<ns1:daylightSavingsTimeZone zoneOffset="-08:00" zoneAbbreviation="AKDT"/>'
        '</ns1:timeZoneInfo>'
        '<ns1:geoLocation>'
        '<ns1:geogLocation srs="EPSG:4326">'
        '<ns1:latitude>64.5</ns1:latitude>'
        '<ns1:longitude>-149.1</ns1:longitude>'
        '</ns1:geogLocation>'
        '</ns1:geoLocation>'
        '<ns1:siteProperty name="siteTypeCd">ST</ns1:siteProperty>'
        '<ns1:siteProperty name="hucCd">19040506</ns1:siteProperty>'
        '<ns1:siteProperty name="stateCd">02</ns1:siteProperty>'
        '<ns1:siteProperty name="countyCd">02090</ns1:siteProperty>'
        '</ns1:sourceInfo>'
        if include_site
        else ''
    )
    query_info = (
        '<ns1:queryInfo>'
        '<ns1:queryURL>https://example.test/nwis/dv</ns1:queryURL>'
        '<ns1:criteria><ns1:timeParam>'
        '<ns1:beginDateTime>2020-10-01T00:00:00.000</ns1:beginDateTime>'
        '<ns1:endDateTime>2020-10-03T00:00:00.000</ns1:endDateTime>'
        '</ns1:timeParam></ns1:criteria>'
        '<ns1:note title="filter:sites">[ALL:15356000]</ns1:note>'
        '</ns1:queryInfo>'
    )

    ts_xml = []
    for s in series:
        values = ''.join(
            f'<ns1:value qualifiers="A" dateTime="{d}">{v}</ns1:value>'
            for d, v in s['values']
        )
        ts_xml.append(
            f'<ns1:timeSeries name="USGS:{site_code}:{s['code']}">'
            f'{source_info}'
            '<ns1:variable>'
            f'<ns1:variableCode vocabulary="NWIS:UnitValues">{s['code']}</ns1:variableCode>'
            f'<ns1:variableName>{s['name']}</ns1:variableName>'
            f'<ns1:variableDescription>{s['name']} description</ns1:variableDescription>'
            '<ns1:unit><ns1:unitCode>ft3/s</ns1:unitCode></ns1:unit>'
            f'<ns1:options><ns1:option name="Statistic" optionCode="00003">{s['stat']}</ns1:option></ns1:options>'
            f'<ns1:noDataValue>{s['nodata']}</ns1:noDataValue>'
            '</ns1:variable>'
            f'<ns1:values>{values}</ns1:values>'
            '</ns1:timeSeries>'
        )

    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<ns1:timeSeriesResponse xmlns:ns1="{WATERML_NS}">'
        f'{query_info}{''.join(ts_xml)}'
        '</ns1:timeSeriesResponse>'
    )
    return xml.encode('utf-8')


def daily_series(
    values,
    code: str = '00060',
    name: str = 'Streamflow',
    stat: str = 'Mean',
    start: str = '2020-10-01',
    nodata: str = '-999999.0',
) -> dict:
    """One daily time series, ready to pass to ``build_waterml``."""
    dates = pd.date_range(start, periods=len(values), freq='D')
    return {
        'code': code,
        'name': name,
        'stat': stat,
        'nodata': nodata,
        'values': [
            (d.strftime('%Y-%m-%dT00:00:00.000'), v) for d, v in zip(dates, values)
        ],
    }


@pytest.fixture
def waterml():
    """``waterml.build([...])`` and ``waterml.series(values, ...)`` helpers."""
    return SimpleNamespace(build=build_waterml, series=daily_series)


# ----------------------------------------------------------------------
# RiverData factory
# ----------------------------------------------------------------------
@pytest.fixture
def river_factory():
    """
    Build a RiverData with a daily ``discharge_cfs`` column.

    Either pass ``values`` (one per day, starting at ``start``) or pass
    ``end`` to get a constant ``value`` for every day from start to end.
    Water-year columns are NOT added, so tests exercise the lazy paths.
    """

    def _make(values=None, start='2020-10-01', end=None, value=100.0, gage_id='TEST'):
        if end is not None:
            idx = pd.date_range(start, end, freq='D', name='date')
        else:
            idx = pd.date_range(start, periods=len(values), freq='D', name='date')
        if values is None:
            values = [value] * len(idx)
        river = RiverData(gage_id)
        river.data = pd.DataFrame({'discharge_cfs': values}, index=idx)
        return river

    return _make


@pytest.fixture
def write_noaa_csv():
    """Write a minimal NOAA CSV (Obstime, PE, Value) holding stage ('HG') data."""

    def _write(directory, gage_id='RLYA2', values=(5.0, 5.5, 6.0), start='2020-10-01'):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        idx = pd.date_range(start, periods=len(values), freq='D')
        df = pd.DataFrame({'Obstime': idx, 'PE': 'HG', 'Value': list(values)})
        path = directory / f'{gage_id}.csv'
        df.to_csv(path, index=False)
        return path

    return _write