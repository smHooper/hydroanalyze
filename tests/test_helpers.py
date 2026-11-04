"""Tests for utils/helpers.py."""
import json
from pathlib import Path

import pytest

from utils import helpers
from utils.helpers import (
    find_config,
    get_noaa_data_path,
    unique_filename,
    validate_config,
)

CONFIG_NAME = 'config/hydroanalyze_config.json'


# ----------------------------------------------------------------------
# find_config
# ----------------------------------------------------------------------
class TestFindConfig:
    @pytest.fixture
    def fake_module(self, tmp_path, monkeypatch):
        """Pretend helpers.py lives at <tmp>/pkg/utils/helpers.py."""
        module_file = tmp_path / 'pkg' / 'utils' / 'helpers.py'
        module_file.parent.mkdir(parents=True)
        module_file.write_text('')
        monkeypatch.setattr(helpers, '__file__', str(module_file))
        return module_file

    def test_finds_config_in_an_ancestor_directory(self, tmp_path, fake_module):
        cfg = tmp_path / CONFIG_NAME
        cfg.parent.mkdir()
        cfg.write_text('{}')

        assert find_config() == cfg.resolve()

    def test_nearest_ancestor_wins(self, tmp_path, fake_module):
        far = tmp_path / CONFIG_NAME
        near = tmp_path / 'pkg' / CONFIG_NAME
        for cfg in (far, near):
            cfg.parent.mkdir(parents=True)
            cfg.write_text('{}')

        assert find_config() == near.resolve()

    def test_custom_name(self, tmp_path, fake_module):
        cfg = tmp_path / 'pkg' / 'other.json'
        cfg.write_text('{}')
        assert find_config('other.json') == cfg.resolve()

    def test_not_found_raises(self, fake_module):
        with pytest.raises(FileNotFoundError):
            find_config('definitely_not_a_real_config_name.json')


# ----------------------------------------------------------------------
# validate_config
# ----------------------------------------------------------------------
class TestValidateConfig:
    def test_all_requested_properties_present(self):
        assert validate_config({'A': 1, 'B': 2}, properties=['A', 'B']) is True

    def test_missing_property_raises_and_names_it(self):
        with pytest.raises(ValueError) as exc:
            validate_config({'A': 1}, properties=['A', 'B', 'C'])
        assert 'B' in str(exc.value)
        assert 'C' in str(exc.value)

    def test_raise_error_false_returns_false(self):
        assert validate_config({}, properties=['A'], raise_error=False) is False

    def test_defaults_are_checked_when_no_properties_given(self):
        full = {
            'NOAA_CDO_API_TOKEN': 't',
            'SSL_CERT_PATH': 'p',
            'NOAA_DATA_PATH': 'd',
        }
        assert validate_config(full) is True

        partial = dict(full)
        del partial['SSL_CERT_PATH']
        with pytest.raises(ValueError, match='SSL_CERT_PATH'):
            validate_config(partial)

    def test_empty_list_also_means_defaults(self):
        assert validate_config({}, properties=[], raise_error=False) is False

    def test_extra_properties_are_fine(self):
        assert validate_config({'A': 1, 'EXTRA': 2}, properties=['A']) is True


# ----------------------------------------------------------------------
# get_noaa_data_path
# ----------------------------------------------------------------------
class TestGetNoaaDataPath:
    def test_uses_given_config(self, tmp_path):
        result = get_noaa_data_path({'NOAA_DATA_PATH': str(tmp_path)})
        assert result == tmp_path.resolve()
        assert isinstance(result, Path)

    def test_relative_path_is_resolved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = get_noaa_data_path({'NOAA_DATA_PATH': 'store'})
        assert result == (tmp_path / 'store').resolve()
        assert result.is_absolute()

    def test_missing_property_raises(self):
        with pytest.raises(ValueError, match='NOAA_DATA_PATH'):
            get_noaa_data_path({'SOMETHING_ELSE': 'x'})

    def test_reads_config_file_when_no_config_given(self, tmp_path, monkeypatch):
        cfg = tmp_path / 'cfg.json'
        cfg.write_text(json.dumps({'NOAA_DATA_PATH': str(tmp_path / 'data')}))
        monkeypatch.setattr(helpers, 'find_config', lambda: cfg)

        assert get_noaa_data_path() == (tmp_path / 'data').resolve()

    def test_default_argument_is_not_mutated(self, tmp_path, monkeypatch):
        cfg = tmp_path / 'cfg.json'
        cfg.write_text(json.dumps({'NOAA_DATA_PATH': str(tmp_path)}))
        monkeypatch.setattr(helpers, 'find_config', lambda: cfg)

        get_noaa_data_path()

        assert get_noaa_data_path.__defaults__[0] == {}


# ----------------------------------------------------------------------
# unique_filename
# ----------------------------------------------------------------------
class TestUniqueFilename:
    def test_unused_name_is_returned_unchanged(self, tmp_path):
        path = tmp_path / 'out.csv'
        assert unique_filename(path) == path

    def test_appends_counter_when_file_exists(self, tmp_path):
        path = tmp_path / 'out.csv'
        path.write_text('x')
        assert unique_filename(path) == tmp_path / 'out_1.csv'

    def test_skips_over_existing_counters(self, tmp_path):
        path = tmp_path / 'out.csv'
        for name in ('out.csv', 'out_1.csv', 'out_2.csv'):
            (tmp_path / name).write_text('x')
        assert unique_filename(path) == tmp_path / 'out_3.csv'

    def test_accepts_str_and_returns_path(self, tmp_path):
        path = tmp_path / 'out.csv'
        path.write_text('x')
        result = unique_filename(str(path))
        assert isinstance(result, Path)
        assert result == tmp_path / 'out_1.csv'

    def test_file_without_extension(self, tmp_path):
        path = tmp_path / 'README'
        path.write_text('x')
        assert unique_filename(path) == tmp_path / 'README_1'

    def test_keeps_only_last_suffix_after_the_counter(self, tmp_path):
        path = tmp_path / 'data.tar.gz'
        path.write_text('x')
        assert unique_filename(path) == tmp_path / 'data.tar_1.gz'

    def test_works_for_directories_too(self, tmp_path):
        (tmp_path / 'folder').mkdir()
        assert unique_filename(tmp_path / 'folder') == tmp_path / 'folder_1'