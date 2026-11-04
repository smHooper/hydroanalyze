"""
Tests for clients.py. No real network access: requests.get is replaced.
"""

import datetime as dt
from types import SimpleNamespace

import pytest
import requests

import clients
from clients import NOAAClient, USGSClient


class FakeResponse:
    def __init__(self, content=b'', json_data=None, status=200):
        self.content = content
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error', response=self)

    def json(self):
        return self._json


@pytest.fixture
def fake_get(monkeypatch):
    state = SimpleNamespace(calls=[], response=FakeResponse(), error=None)

    def _get(url, params=None, **kwargs):
        state.calls.append({'url': url, 'params': params, 'kwargs': kwargs})
        if state.error is not None:
            raise state.error
        return state.response

    monkeypatch.setattr(clients.requests, 'get', _get)
    return state


class TestUSGSClient:
    def test_returns_raw_bytes(self, fake_get):
        fake_get.response = FakeResponse(content=b'<xml/>')
        assert USGSClient().fetch_data('15356000') == b'<xml/>'

    def test_request_parameters(self, fake_get):
        USGSClient().fetch_data('15356000')

        call = fake_get.calls[0]
        assert call['url'] == USGSClient.BASE_URL
        assert call['params']['format'] == 'waterml,1.1'
        assert call['params']['sites'] == '15356000'
        assert call['params']['startDT'] == '1851-01-01'
        assert call['params']['endDT'] == dt.date.today().isoformat()

    @pytest.mark.parametrize('status', [400, 404, 500])
    def test_http_errors_propagate(self, fake_get, status):
        fake_get.response = FakeResponse(status=status)
        with pytest.raises(requests.HTTPError):
            USGSClient().fetch_data('15356000')

    def test_connection_errors_propagate(self, fake_get):
        fake_get.error = requests.ConnectionError('no network')
        with pytest.raises(requests.ConnectionError):
            USGSClient().fetch_data('15356000')


class TestNOAAClient:
    def test_returns_parsed_json(self, fake_get):
        fake_get.response = FakeResponse(json_data={'name': 'Test River'})
        assert NOAAClient().get_metadata('RLYA2') == {'name': 'Test River'}

    def test_url_contains_gage_id(self, fake_get):
        fake_get.response = FakeResponse(json_data={})
        NOAAClient().get_metadata('RLYA2')
        assert fake_get.calls[0]['url'] == f'{NOAAClient.BASE_URL}/gauges/RLYA2'

    def test_http_error_propagates(self, fake_get):
        fake_get.response = FakeResponse(status=404)
        with pytest.raises(requests.HTTPError):
            NOAAClient().get_metadata('NOPE1')

    def test_timeout_propagates(self, fake_get):
        fake_get.error = requests.Timeout('slow')
        with pytest.raises(requests.Timeout):
            NOAAClient().get_metadata('RLYA2')