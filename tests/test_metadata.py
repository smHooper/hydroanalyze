"""Tests for metadata.py."""
import datetime as dt
import json
import logging
import xml.etree.ElementTree as ET

import pytest
import requests

import metadata as metadata_mod
from metadata import (
    Metadata,
    NOAAMetadata,
    USGSMetadata,
    ensure_noaa_metadata,
    list_saved_metadata,
)

NOAA_RAW = {
    'name': 'Test River',
    'latitude': 61.2,
    'longitude': -149.9,
    'usgsId': '15356000',
    'state': {'abbreviation': 'AK'},
    'timeZone': 'America/Anchorage',
}


class FakeNOAAClient:
    def __init__(self, raw=None, error=None):
        self.raw = raw
        self.error = error
        self.calls = []

    def get_metadata(self, gage_id):
        self.calls.append(gage_id)
        if self.error is not None:
            raise self.error
        return self.raw


# ----------------------------------------------------------------------
# Metadata base class: serialization and file I/O
# ----------------------------------------------------------------------
class TestSerialization:
    def test_to_dict_includes_source(self):
        assert Metadata(gage_id='A').to_dict()['source'] == 'unknown'
        assert USGSMetadata(gage_id='A').to_dict()['source'] == 'USGS'
        assert NOAAMetadata(gage_id='A').to_dict()['source'] == 'NOAA'

    def test_from_dict_dispatches_on_source(self):
        d = USGSMetadata(gage_id='1', gage_name='N', huc='123').to_dict()
        m = Metadata.from_dict(d)
        assert isinstance(m, USGSMetadata)
        assert m.huc == '123'

    def test_from_dict_unknown_source_falls_back_to_base_class(self):
        m = Metadata.from_dict({'source': 'mystery', 'gage_id': 'X'})
        assert type(m) is Metadata

    def test_from_dict_without_source_key(self):
        assert type(Metadata.from_dict({'gage_id': 'X'})) is Metadata

    def test_from_dict_ignores_unknown_keys_and_does_not_mutate_input(self):
        d = {'source': 'NOAA', 'gage_id': 'X', 'bogus': 1}
        m = Metadata.from_dict(d)
        assert m.gage_id == 'X'
        assert not hasattr(m, 'bogus')
        assert d['source'] == 'NOAA'

    def test_get_fetch_time_sets_utc_iso_timestamp(self):
        m = Metadata(gage_id='A')
        m.get_fetch_time()
        parsed = dt.datetime.fromisoformat(m.fetched_at)
        assert parsed.utcoffset() == dt.timedelta(0)


class TestSaveAndLoad:
    def test_path_for(self, tmp_path):
        assert Metadata.path_for(tmp_path, 'RLYA2') == tmp_path / 'RLYA2_meta.json'

    def test_round_trip_noaa(self, tmp_path):
        original = NOAAMetadata(
            gage_id='RLYA2', gage_name='Test River', latitude=61.0,
            usgs_id='1', state='AK', raw={'a': 1},
        )
        path = original.save(tmp_path)
        assert path.name == 'RLYA2_meta.json'

        loaded = Metadata.load(tmp_path, 'RLYA2')
        assert isinstance(loaded, NOAAMetadata)
        assert loaded == original

    def test_round_trip_usgs_keeps_variables(self, tmp_path):
        original = USGSMetadata(gage_id='1', variables={'00060': {'name': 'Q'}})
        original.save(tmp_path)
        loaded = Metadata.load(tmp_path, '1')
        assert isinstance(loaded, USGSMetadata)
        assert loaded.variables == {'00060': {'name': 'Q'}}

    def test_save_creates_missing_directories(self, tmp_path):
        path = Metadata(gage_id='A').save(tmp_path / 'a' / 'b')
        assert path.exists()

    def test_load_missing_file_returns_none(self, tmp_path):
        assert Metadata.load(tmp_path, 'NOPE') is None

    def test_load_corrupt_json_returns_none(self, tmp_path):
        Metadata.path_for(tmp_path, 'A').write_text('{not json', encoding='utf-8')
        assert Metadata.load(tmp_path, 'A') is None

    def test_load_json_missing_required_field_returns_none(self, tmp_path):
        Metadata.path_for(tmp_path, 'A').write_text(
            json.dumps({'source': 'USGS'}), encoding='utf-8'
        )
        assert Metadata.load(tmp_path, 'A') is None


class TestPrettyPrint:
    def test_summary_contains_key_fields(self):
        m = NOAAMetadata(
            gage_id='RLYA2', gage_name='Test River', latitude=61.0,
            longitude=-149.0, timezone='AKST',
            fetched_at='2026-01-05T15:30:00+00:00',
        )
        text = m.pretty_print()
        assert 'Test River' in text
        assert 'RLYA2' in text
        assert 'NOAA' in text
        assert '61.0, -149.0' in text
        assert 'AKST' in text
        assert 'January 05, 2026 03:30 PM' in text

    def test_all_true_dumps_json(self):
        m = Metadata(gage_id='A', fetched_at='2026-01-05T15:30:00+00:00')
        assert json.loads(m.pretty_print(all=True))['gage_id'] == 'A'

    def test_missing_fetched_at_does_not_crash(self):
        Metadata(gage_id='A').pretty_print()


# ----------------------------------------------------------------------
# USGSMetadata.from_xml
# ----------------------------------------------------------------------
class TestUSGSFromXml:
    def parse(self, waterml, **kwargs):
        xml = waterml.build([waterml.series([1, 2, 3])], **kwargs)
        return ET.fromstring(xml)

    def test_site_fields(self, waterml):
        meta = USGSMetadata.from_xml(self.parse(waterml))
        assert meta.gage_id == '15356000'
        assert meta.gage_name == 'TEST RIVER NEAR PLACE'
        assert meta.latitude == pytest.approx(64.5)
        assert meta.longitude == pytest.approx(-149.1)
        assert meta.timezone == 'AKST'
        assert meta.state_code == '02'
        assert meta.county_code == '02090'
        assert meta.site_type == 'ST'
        assert meta.huc == '19040506'

    def test_fetch_time_is_set(self, waterml):
        meta = USGSMetadata.from_xml(self.parse(waterml))
        assert meta.fetched_at is not None

    def test_query_info_in_raw(self, waterml):
        raw = USGSMetadata.from_xml(self.parse(waterml)).raw
        assert raw['query']['url'] == 'https://example.test/nwis/dv'
        assert raw['query']['begin'].startswith('2020-10-01')
        assert raw['query']['notes'] == {'filter:sites': '[ALL:15356000]'}
        assert raw['site']['uses_dst'] is True

    def test_variable_info(self, waterml):
        meta = USGSMetadata.from_xml(self.parse(waterml))
        var = meta.variables['00060']
        assert var['unit'] == 'ft3/s'
        assert var['no_data_value'] == '-999999.0'
        assert var['statistics'] == ['Mean']

    def test_multiple_statistics_for_one_variable_are_merged(self, waterml):
        xml = waterml.build([
            waterml.series([1, 2], code='00010', stat='Maximum'),
            waterml.series([0, 1], code='00010', stat='Minimum'),
        ])
        meta = USGSMetadata.from_xml(ET.fromstring(xml))
        assert meta.variables['00010']['statistics'] == ['Maximum', 'Minimum']

    def test_gage_id_fallback_when_site_code_missing(self, waterml):
        root = self.parse(waterml, site_code=None)
        meta = USGSMetadata.from_xml(root, gage_id='FALLBACK')
        assert meta.gage_id == 'FALLBACK'

    def test_response_without_site_info(self, waterml):
        root = ET.fromstring(waterml.build([], include_site=False))
        meta = USGSMetadata.from_xml(root, gage_id='X')
        assert meta.gage_id == 'X'


# ----------------------------------------------------------------------
# NOAAMetadata.from_api
# ----------------------------------------------------------------------
class TestNOAAFromApi:
    def test_maps_fields(self):
        meta = NOAAMetadata.from_api(NOAA_RAW, 'RLYA2')
        assert meta.gage_id == 'RLYA2'
        assert meta.gage_name == 'Test River'
        assert meta.latitude == 61.2
        assert meta.longitude == -149.9
        assert meta.usgs_id == '15356000'
        assert meta.state == 'AK'
        assert meta.timezone == 'America/Anchorage'
        assert meta.raw == NOAA_RAW

    def test_tolerates_missing_keys(self):
        meta = NOAAMetadata.from_api({}, 'RLYA2')
        assert meta.gage_name is None
        assert meta.state is None

    def test_state_none(self):
        assert NOAAMetadata.from_api({'state': None}, 'A').state is None


# ----------------------------------------------------------------------
# ensure_noaa_metadata
# ----------------------------------------------------------------------
class TestEnsureNoaaMetadata:
    def test_fetches_and_saves_when_nothing_cached(self, tmp_path):
        client = FakeNOAAClient(raw=NOAA_RAW)
        meta = ensure_noaa_metadata('RLYA2', tmp_path, client=client)

        assert isinstance(meta, NOAAMetadata)
        assert meta.gage_name == 'Test River'
        assert meta.fetched_at is not None
        assert client.calls == ['RLYA2']
        assert Metadata.path_for(tmp_path, 'RLYA2').exists()

    def test_uses_cached_file_without_calling_client(self, tmp_path):
        NOAAMetadata(gage_id='RLYA2', gage_name='Cached').save(tmp_path)
        client = FakeNOAAClient(error=AssertionError('client should not be called'))

        meta = ensure_noaa_metadata('RLYA2', tmp_path, client=client)

        assert meta.gage_name == 'Cached'
        assert client.calls == []

    def test_refresh_bypasses_cache_and_rewrites_file(self, tmp_path):
        NOAAMetadata(gage_id='RLYA2', gage_name='Old').save(tmp_path)
        client = FakeNOAAClient(raw=NOAA_RAW)

        meta = ensure_noaa_metadata('RLYA2', tmp_path, client=client, refresh=True)

        assert meta.gage_name == 'Test River'
        assert Metadata.load(tmp_path, 'RLYA2').gage_name == 'Test River'

    def test_write_file_false_does_not_write(self, tmp_path):
        client = FakeNOAAClient(raw=NOAA_RAW)
        meta = ensure_noaa_metadata('RLYA2', tmp_path, client=client, write_file=False)
        assert meta.gage_name == 'Test River'
        assert not Metadata.path_for(tmp_path, 'RLYA2').exists()

    def test_network_failure_returns_bare_metadata_and_logs(self, tmp_path, caplog):
        client = FakeNOAAClient(error=requests.ConnectionError('boom'))
        log = logging.getLogger('test_metadata')

        with caplog.at_level(logging.WARNING, logger='test_metadata'):
            meta = ensure_noaa_metadata('RLYA2', tmp_path, client=client, log=log)

        assert type(meta) is Metadata
        assert meta.gage_id == 'RLYA2'
        assert meta.gage_name is None
        assert 'metadata request failed' in caplog.text
        assert not Metadata.path_for(tmp_path, 'RLYA2').exists()

    def test_network_failure_without_logger(self, tmp_path):
        client = FakeNOAAClient(error=requests.Timeout('slow'))
        assert type(ensure_noaa_metadata('A', tmp_path, client=client)) is Metadata

    def test_default_client_is_noaa_client(self, tmp_path, monkeypatch):
        fake = FakeNOAAClient(raw=NOAA_RAW)
        monkeypatch.setattr(metadata_mod, 'NOAAClient', lambda: fake)
        ensure_noaa_metadata('RLYA2', tmp_path)
        assert fake.calls == ['RLYA2']


# ----------------------------------------------------------------------
# list_saved_metadata
# ----------------------------------------------------------------------
class TestListSavedMetadata:
    def test_missing_directory_raises(self, tmp_path):
        with pytest.raises(ValueError, match='does not exist'):
            list_saved_metadata(tmp_path / 'nope')

    def test_empty_directory(self, tmp_path):
        assert list_saved_metadata(tmp_path) == []

    def test_sorted_with_fallback_and_filtering(self, tmp_path):
        (tmp_path / 'B.csv').write_text('')
        (tmp_path / 'A.csv').write_text('')
        (tmp_path / 'A_overwrites.csv').write_text('')   # skipped
        (tmp_path / 'notes.txt').write_text('')          # not a csv
        NOAAMetadata(gage_id='A', gage_name='Gage A').save(tmp_path)

        found = list_saved_metadata(tmp_path)

        assert [m.gage_id for m in found] == ['A', 'B']
        assert found[0].gage_name == 'Gage A'
        assert found[1].gage_name is None          # bare Metadata fallback