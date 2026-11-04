"""
Tests for cli.py.

The data layer is stubbed where possible (cli._load, cli.USGSClient,
cli.get_noaa_data_path, cli.ingest), so these tests check the CLI's own
behavior: argument handling, flag validation, exit codes and output files.
"""
from types import SimpleNamespace

import pandas as pd
import pytest
import requests
import typer
from typer.testing import CliRunner

import cli
from cli import METRIC_FLAGS, Metric, _load, _parse_month_day, app
from metadata import Metadata, NOAAMetadata

runner = CliRunner()


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def full_river(river_factory):
    """Two complete water years of constant flow, with metadata attached."""
    river = river_factory(start='2020-10-01', end='2022-09-30', value=100.0)
    river._metadata = Metadata(
        gage_id='TEST',
        gage_name='Test River',
        fetched_at='2026-01-05T15:30:00+00:00',
    )
    return river

@pytest.fixture
def empty_river(river_factory):
    """River with nothing but null values"""
    river = river_factory(start='2020-10-01', end='2022-09-30', value=pd.NA)
    river._metadata = Metadata(
        gage_id='TEST',
        gage_name='Test River',
        fetched_at='2026-01-05T15:30:00+00:00',
    )
    return river

@pytest.fixture
def stub_load(monkeypatch):
    """Replace cli._load so commands run against an in-memory river."""

    def _stub(river, xml=None):
        def fake(gage_id, data_source=None):
            river.get_water_year()
            return river, xml

        monkeypatch.setattr(cli, '_load', fake)
        return river

    return _stub


@pytest.fixture
def fake_usgs(monkeypatch):
    state = SimpleNamespace(xml=b'', error=None, calls=[])

    class _Client:
        def fetch_data(self, gage_id):
            state.calls.append(gage_id)
            if state.error is not None:
                raise state.error
            return state.xml

    monkeypatch.setattr(cli, 'USGSClient', _Client)
    return state


@pytest.fixture
def noaa_dir(tmp_path, monkeypatch):
    directory = tmp_path / 'noaa_store'
    directory.mkdir()
    monkeypatch.setattr(cli, 'get_noaa_data_path', lambda: directory)
    return directory


@pytest.fixture
def fake_ingest(monkeypatch):
    state = SimpleNamespace(calls=[], code=0, error=None)

    def _ingest(search_dir, **kwargs):
        state.calls.append((search_dir, kwargs))
        if state.error is not None:
            raise state.error
        return state.code

    monkeypatch.setattr(cli, 'ingest', _ingest)
    return state


# ----------------------------------------------------------------------
# Static definitions
# ----------------------------------------------------------------------
class TestMetricDefinitions:
    def test_values_are_unique_kebab_case(self):
        values = [m.value for m in Metric]
        assert len(values) == len(set(values))
        assert all('_' not in v for v in values)

    def test_every_metric_has_description(self):
        assert all(m.description for m in Metric)

    def test_every_metric_flag_is_a_real_analyze_option(self):
        analyze_cmd = typer.main.get_command(app).commands['analyze']
        real_options = {opt for p in analyze_cmd.params for opt in p.opts}
        assert METRIC_FLAGS <= real_options


class TestParseMonthDay:
    def test_none_passes_through(self):
        assert _parse_month_day(None) is None

    @pytest.mark.parametrize(
        'text, expected',
        [('03-01', (3, 1)), ('12-31', (12, 31)), ('02-29', (2, 29))],
    )
    def test_valid(self, text, expected):
        assert _parse_month_day(text) == expected

    @pytest.mark.parametrize('text', ['13-01', '02-30', '3/1', 'March 1', ''])
    def test_invalid(self, text):
        with pytest.raises(typer.BadParameter):
            _parse_month_day(text)


# ----------------------------------------------------------------------
# metrics / top-level
# ----------------------------------------------------------------------
class TestMetricsCommand:
    def test_lists_every_metric_and_its_options(self):
        result = runner.invoke(app, ['metrics'])
        assert result.exit_code == 0
        for m in Metric:
            assert m.value in result.output
            for option in m.options:
                assert option in result.output

    def test_no_args_shows_usage(self):
        result = runner.invoke(app, [])
        assert 'Usage' in result.output


# ----------------------------------------------------------------------
# _load
# ----------------------------------------------------------------------
class TestLoad:
    def test_numeric_id_uses_usgs(self, fake_usgs, waterml):
        fake_usgs.xml = waterml.build([waterml.series([1.0, 2.0, 3.0])])

        river, xml = _load('15356000')

        assert fake_usgs.calls == ['15356000']
        assert xml == fake_usgs.xml
        assert 'water_year' in river.data.columns

    def test_alphanumeric_id_uses_noaa_store(
        self, fake_usgs, noaa_dir, write_noaa_csv
    ):
        write_noaa_csv(noaa_dir, 'RLYA2')
        NOAAMetadata(gage_id='RLYA2', gage_name='Test River').save(noaa_dir)

        river, xml = _load('RLYA2')

        assert xml is None
        assert fake_usgs.calls == []
        assert 'water_year' in river.data.columns
        assert river.gage_name == 'Test River'

    def test_data_source_override_forces_usgs(self, fake_usgs, waterml):
        fake_usgs.xml = waterml.build([waterml.series([1.0, 2.0])])
        _load('RLYA2', data_source=cli.DataSource.usgs)
        assert fake_usgs.calls == ['RLYA2']

    def test_data_source_override_forces_noaa(self, fake_usgs, noaa_dir, capsys):
        with pytest.raises(typer.Exit) as exc:
            _load('15356000', data_source=cli.DataSource.noaa)

        assert exc.value.exit_code == 1
        assert fake_usgs.calls == []
        assert 'Could not find NOAA data' in capsys.readouterr().err

    def test_missing_noaa_data_exits(self, noaa_dir, capsys):
        with pytest.raises(typer.Exit) as exc:
            _load('RLYA2')

        assert exc.value.exit_code == 1
        assert 'Could not find NOAA data for gage RLYA2' in capsys.readouterr().err

    def test_usgs_http_error_exits(self, fake_usgs, capsys):
        fake_usgs.error = requests.HTTPError('400 Bad Request')
        with pytest.raises(typer.Exit) as exc:
            _load('15356000')

        assert exc.value.exit_code == 1
        assert 'USGS rejected the request' in capsys.readouterr().err

    def test_usgs_connection_error_exits(self, fake_usgs, capsys):
        fake_usgs.error = requests.ConnectionError('offline')
        with pytest.raises(typer.Exit) as exc:
            _load('15356000')

        assert exc.value.exit_code == 1
        assert 'Could not reach USGS' in capsys.readouterr().err

    def test_malformed_xml_exits(self, fake_usgs):
        fake_usgs.xml = b'not xml'
        with pytest.raises(typer.Exit) as exc:
            _load('15356000')
        assert exc.value.exit_code == 1

    def test_unknown_gage_gives_friendly_error(self, fake_usgs, waterml, capsys):
        fake_usgs.xml = waterml.build([], include_site=False)
        with pytest.raises(typer.Exit) as exc:
            _load('00000000')

        assert exc.value.exit_code == 1
        assert 'No timeseries data found' in capsys.readouterr().err


# ----------------------------------------------------------------------
# analyze
# ----------------------------------------------------------------------
class TestAnalyze:
    def invoke(self, tmp_path, *args):
        return runner.invoke(app, ['analyze', 'TEST', *args, '--out', str(tmp_path)])

    @pytest.mark.parametrize('metric', [m.value for m in Metric])
    def test_every_metric_runs_with_defaults(self, metric, stub_load, full_river, tmp_path):
        stub_load(full_river)
        result = self.invoke(tmp_path, metric)

        assert result.exit_code == 0, result.output
        assert (tmp_path / f'TEST_{metric}.csv').exists()
        assert 'Saved' in result.output

    def test_output_contents(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        self.invoke(tmp_path, 'mean-discharge')

        out = pd.read_csv(tmp_path / 'TEST_mean-discharge.csv', index_col=0)
        assert out.iloc[:, 0].tolist() == pytest.approx([100.0, 100.0])

    def test_monthly_long_format(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        result = self.invoke(tmp_path, 'monthly', '--long')

        assert result.exit_code == 0, result.output
        out = pd.read_csv(tmp_path / 'TEST_monthly.csv')
        assert {'water_year', 'month', 'discharge_cfs_mean'} <= set(out.columns)

    def test_max_with_from_date(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        result = self.invoke(tmp_path, 'max', '--from-date', '03-01')
        assert result.exit_code == 0, result.output

    def test_creates_missing_output_directory(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        out = tmp_path / 'a' / 'b'
        result = runner.invoke(
            app, ['analyze', 'TEST', 'total-discharge', '--out', str(out)]
        )
        assert result.exit_code == 0, result.output
        assert (out / 'TEST_total-discharge.csv').exists()

    def test_overwrite_reuses_filename(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        for _ in range(2):
            self.invoke(tmp_path, 'total-discharge', '--overwrite')
        assert len(list(tmp_path.glob('*.csv'))) == 1

    def test_without_overwrite_assigns_unique_name(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        for _ in range(2):
            self.invoke(tmp_path, 'total-discharge')
        assert len(list(tmp_path.glob('*.csv'))) == 2

    # --- flag validation ---
    def test_flag_not_used_by_metric_is_rejected(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        result = self.invoke(tmp_path, 'total-discharge', '--stat', 'max')

        assert result.exit_code == 1
        assert "--stat not used by 'total-discharge'" in result.output
        assert not list(tmp_path.glob('*.csv'))

    def test_from_date_and_after_water_doy_conflict(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        result = self.invoke(
            tmp_path, 'max', '--from-date', '03-01', '--after-water-doy', '100'
        )

        assert result.exit_code == 1
        assert 'not both' in result.output

    def test_bad_from_date_is_a_usage_error(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        result = self.invoke(tmp_path, 'max', '--from-date', '13-45')
        assert result.exit_code == 2

    def test_after_water_doy_below_minimum_is_rejected(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        result = self.invoke(tmp_path, 'max', '--after-water-doy', '0')
        assert result.exit_code == 2

    def test_unknown_metric_is_a_usage_error(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        result = self.invoke(tmp_path, 'not-a-metric')
        assert result.exit_code == 2

    def test_value_error_from_analysis_becomes_exit_1(self, stub_load, full_river, tmp_path):
        stub_load(full_river)
        result = self.invoke(tmp_path, 'max', '--variable', 'nope')

        assert result.exit_code == 1
        assert 'Error:' in result.output
        assert not list(tmp_path.glob('*.csv'))


# ----------------------------------------------------------------------
# info
# ----------------------------------------------------------------------
class TestInfo:
    def test_shows_metadata_ranges_and_missing_days(self, stub_load, full_river):
        stub_load(full_river)
        result = runner.invoke(app, ['info', 'TEST'])

        assert result.exit_code == 0, result.output
        assert 'Test River' in result.output
        assert 'Available Data Variables: discharge_cfs' in result.output
        assert 'Date Ranges per Data Variable' in result.output
        assert 'Missing Days per Gage Variable' in result.output

    def test_missing_days_option_complete_record(self, stub_load, full_river):
        stub_load(full_river)
        result = runner.invoke(app, ['info', 'TEST', '--missing-days', 'discharge_cfs'])

        assert result.exit_code == 0, result.output
        assert 'Every water year in the record is complete' in result.output

    def test_missing_days_option_reports_gaps(self, stub_load, full_river):
        full_river.data = full_river.data.drop(pd.date_range('2021-03-10', '2021-03-12'))
        stub_load(full_river)

        result = runner.invoke(app, ['info', 'TEST', '--missing-days', 'discharge_cfs'])

        assert result.exit_code == 0, result.output
        assert '1 of 2 water years have gaps (3 missing days total)' in result.output

    def test_missing_days_unknown_variable_exits(self, stub_load, full_river):
        stub_load(full_river)
        result = runner.invoke(app, ['info', 'TEST', '--missing-days', 'nope'])

        assert result.exit_code == 1
        assert 'Error:' in result.output

    def test_no_metadata_message(self, stub_load, full_river):
        full_river._metadata = None
        stub_load(full_river)
        result = runner.invoke(app, ['info', 'TEST'])

        assert result.exit_code == 0, result.output
        assert 'No metadata found for this gage' in result.output

    def test_empty_missing_days_result(self, stub_load, empty_river):
        stub_load(empty_river)
        result = runner.invoke(app, ['info', 'TEST'])

        assert result.exit_code == 0, result.output
        assert 'WARNING: No valid data found for any variable' in result.output


# ----------------------------------------------------------------------
# fetch
# ----------------------------------------------------------------------
class TestFetch:
    def test_usgs_saves_xml_and_csv(self, stub_load, full_river, tmp_path):
        stub_load(full_river, xml=b'<raw/>')
        result = runner.invoke(app, ['fetch', 'TEST', '--out', str(tmp_path)])

        assert result.exit_code == 0, result.output
        folder = tmp_path / 'TEST'
        assert len(list(folder.glob('*.xml'))) == 1
        assert len(list(folder.glob('*.csv'))) == 1
        assert result.output.count('Saved') == 2

    def test_noaa_saves_only_csv(self, stub_load, full_river, tmp_path):
        stub_load(full_river, xml=None)
        result = runner.invoke(app, ['fetch', 'TEST', '--out', str(tmp_path)])

        assert result.exit_code == 0, result.output
        folder = tmp_path / 'TEST'
        assert not list(folder.glob('*.xml'))
        assert len(list(folder.glob('*.csv'))) == 1

    def test_show_metadata_writes_nothing(self, stub_load, full_river, tmp_path):
        stub_load(full_river, xml=b'<raw/>')
        out = tmp_path / 'out'
        result = runner.invoke(
            app, ['fetch', 'TEST', '--show-metadata', '--out', str(out)]
        )

        assert result.exit_code == 0, result.output
        assert 'Test River' in result.output
        assert not out.exists()


# ----------------------------------------------------------------------
# gages
# ----------------------------------------------------------------------
class TestGages:
    @pytest.fixture
    def populated(self, noaa_dir):
        (noaa_dir / 'RLYA2.csv').write_text('')
        (noaa_dir / 'SRDA2.csv').write_text('')
        NOAAMetadata(gage_id='RLYA2', gage_name='Test River').save(noaa_dir)
        return noaa_dir

    def test_lists_all_gages(self, populated):
        result = runner.invoke(app, ['gages'])

        assert result.exit_code == 0, result.output
        assert 'RLYA2' in result.output
        assert 'Test River' in result.output
        assert 'SRDA2' in result.output
        assert '(no metadata)' in result.output
        assert '2 gage(s)' in result.output

    def test_search_matches_name_case_insensitively(self, populated):
        result = runner.invoke(app, ['gages', '--search', 'test riv'])

        assert result.exit_code == 0, result.output
        assert 'RLYA2' in result.output
        assert 'SRDA2' not in result.output
        assert '1 gage(s)' in result.output

    def test_search_matches_id(self, populated):
        result = runner.invoke(app, ['gages', '--search', 'srda'])
        assert 'SRDA2' in result.output
        assert 'RLYA2' not in result.output

    def test_no_matches(self, populated):
        result = runner.invoke(app, ['gages', '--search', 'zzz'])

        assert result.exit_code == 0
        assert 'No gages found' in result.output

    def test_missing_store_directory_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, 'get_noaa_data_path', lambda: tmp_path / 'missing')
        result = runner.invoke(app, ['gages'])

        assert result.exit_code == 1
        assert 'Error:' in result.output


# ----------------------------------------------------------------------
# add-noaa
# ----------------------------------------------------------------------
class TestAddNoaa:
    def test_defaults_are_passed_to_ingest(self, fake_ingest, tmp_path):
        result = runner.invoke(app, ['add-noaa', str(tmp_path)])

        assert result.exit_code == 0, result.output
        search_dir, kwargs = fake_ingest.calls[0]
        assert search_dir == tmp_path
        assert kwargs['dry_run'] is False
        assert kwargs['skip_bad_files'] is False
        assert kwargs['overwrite_gages'] == []
        assert kwargs['config_path'] is None
        assert kwargs['search_pattern'] == cli.DEFAULT_SEARCH_PATTERN

    def test_flags_are_passed_to_ingest(self, fake_ingest, tmp_path):
        result = runner.invoke(app, [
            'add-noaa', str(tmp_path),
            '--dry-run', '--skip-bad-files',
            '--overwrite', 'RLYA2', '--overwrite', 'SRDA2',
            '--search-pattern', r'.*\.txt',
        ])

        assert result.exit_code == 0, result.output
        _, kwargs = fake_ingest.calls[0]
        assert kwargs['dry_run'] is True
        assert kwargs['skip_bad_files'] is True
        assert kwargs['overwrite_gages'] == ['RLYA2', 'SRDA2']
        assert kwargs['search_pattern'] == r'.*\.txt'

    @pytest.mark.parametrize('code', [0, 1, 2])
    def test_ingest_return_code_becomes_exit_code(self, fake_ingest, tmp_path, code):
        fake_ingest.code = code
        result = runner.invoke(app, ['add-noaa', str(tmp_path)])
        assert result.exit_code == code

    @pytest.mark.parametrize(
        'error', [RuntimeError('no NOAA_DATA_PATH'), OSError('locked'), ValueError('bad json')]
    )
    def test_ingest_errors_become_exit_1(self, fake_ingest, tmp_path, error):
        fake_ingest.error = error
        result = runner.invoke(app, ['add-noaa', str(tmp_path)])

        assert result.exit_code == 1
        assert f'Error: {error}' in result.output

    def test_nonexistent_search_dir_is_a_usage_error(self, fake_ingest, tmp_path):
        result = runner.invoke(app, ['add-noaa', str(tmp_path / 'missing')])

        assert result.exit_code == 2
        assert fake_ingest.calls == []