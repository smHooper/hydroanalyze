"""
Tests for utils/ingest_noaa.py.

Because this module can overwrite stored data, the tests focus on the
guarantees in its docstring: nothing is written on conflict, records are
immutable unless --overwrite is given, overwrites are backed up and audited,
and --dry-run writes nothing.

The NOAA metadata API call is always stubbed, so no test touches the network.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from metadata import Metadata, NOAAMetadata
from utils import ingest_noaa
from utils.ingest_noaa import (
    COLUMNS,
    DEFAULT_SEARCH_PATTERN,
    KEY,
    IngestConflict,
    find_files,
    ingest,
    load_existing,
    merge_gage,
    read_noaa_file,
    setup_logging,
    write_csv_atomic,
)

HEADER = 'LID PE Obstime Value'


# ----------------------------------------------------------------------
# Helpers for building input files
# ----------------------------------------------------------------------
def rows(gage, values, start='2020-10-01', pe='HG'):
    """(LID, PE, 'YYYY-MM-DD 00:00:00', value) tuples, one per day."""
    days = pd.date_range(start, periods=len(values), freq='D')
    return [(gage, pe, f'{d:%Y-%m-%d} 00:00:00', v) for d, v in zip(days, values)]


def write_nws(
    directory,
    row_list,
    gage='RLYA2',
    pe='HG',
    tag='20201001-20201003',
    header=HEADER,
    comments=True,
):
    """Write an NWS-style whitespace-delimited file whose name matches the default pattern."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lines = []
    if comments:
        lines += ['# QCed data from the NWS', '', '# second comment line']
    lines.append(header)
    lines += [' '.join(str(x) for x in r) for r in row_list]
    path = directory / f'{gage}_{pe}_{tag}.txt'
    path.write_text('\n'.join(lines) + '\n')
    return path


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def text(self):
        return '\n'.join(r.getMessage() for r in self.records)

    @property
    def warnings(self):
        return '\n'.join(
            r.getMessage() for r in self.records if r.levelno >= logging.WARNING
        )

    def clear(self):
        self.records.clear()


@pytest.fixture(autouse=True)
def reset_logger():
    """ingest_noaa.log is global state (setup_logging sets propagate=False)."""
    yield
    for h in list(ingest_noaa.log.handlers):
        h.close()
        ingest_noaa.log.removeHandler(h)
    ingest_noaa.log.propagate = True
    ingest_noaa.log.setLevel(logging.NOTSET)


@pytest.fixture
def log_records(monkeypatch):
    """
    Capture everything sent to ingest_noaa.log. ingest() calls setup_logging(),
    which clears handlers, so wrap it to re-attach the capture handler.
    """
    handler = ListHandler()
    real_setup = ingest_noaa.setup_logging

    def wrapped(log_file):
        real_setup(log_file)
        ingest_noaa.log.addHandler(handler)

    monkeypatch.setattr(ingest_noaa, 'setup_logging', wrapped)
    ingest_noaa.log.addHandler(handler)
    return handler


@pytest.fixture(autouse=True)
def meta_stub(monkeypatch):
    """Never hit the NOAA API. Set ``meta_stub.fail = True`` to simulate an outage."""
    state = SimpleNamespace(calls=[], fail=False)

    def fake(gage_id, data_dir, **kwargs):
        state.calls.append({'gage_id': gage_id, **kwargs})
        if state.fail:
            return Metadata(gage_id=gage_id)
        meta = NOAAMetadata.from_api({'name': f'{gage_id} River'}, gage_id)
        meta.get_fetch_time()
        return meta

    monkeypatch.setattr(ingest_noaa, 'ensure_noaa_metadata', fake)
    return state


@pytest.fixture
def env(tmp_path):
    store = tmp_path / 'store'
    src = tmp_path / 'src'
    src.mkdir()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'NOAA_DATA_PATH': str(store)}))
    return SimpleNamespace(store=store, src=src, config=config, root=tmp_path)


def run(env, src=None, **kwargs):
    return ingest(src or env.src, config_path=env.config, **kwargs)


def stored(env, gage='RLYA2'):
    return load_existing(env.store / f'{gage}.csv')


# ======================================================================
# find_files
# ======================================================================
class TestFindFiles:
    def test_pattern_must_match_the_whole_name(self, tmp_path):
        good = tmp_path / 'RLYA2_HG_20201001-20201003.txt'
        backup = tmp_path / 'RLYA2_HG_20201001-20201003.txt.bak'
        notes = tmp_path / 'notes.txt'
        for p in (good, backup, notes):
            p.write_text('')

        matched, skipped = find_files(tmp_path, DEFAULT_SEARCH_PATTERN)

        assert matched == [good]
        assert set(skipped) == {backup, notes}

    def test_searches_subdirectories(self, tmp_path):
        nested = tmp_path / 'a' / 'b' / 'RLYA2_HG_20201001-20201003.txt'
        nested.parent.mkdir(parents=True)
        nested.write_text('')

        matched, _ = find_files(tmp_path, DEFAULT_SEARCH_PATTERN)
        assert matched == [nested]

    def test_custom_pattern(self, tmp_path):
        (tmp_path / 'a.dat').write_text('')
        (tmp_path / 'b.txt').write_text('')
        matched, skipped = find_files(tmp_path, r'.*\.dat')
        assert [p.name for p in matched] == ['a.dat']
        assert [p.name for p in skipped] == ['b.txt']

    def test_files_within_a_directory_are_sorted(self, tmp_path):
        for name in ('RLYA2_HG_20201005-20201006.txt', 'RLYA2_HG_20201001-20201002.txt'):
            (tmp_path / name).write_text('')
        matched, _ = find_files(tmp_path, DEFAULT_SEARCH_PATTERN)
        assert [p.name for p in matched] == sorted(p.name for p in matched)

    def test_excluded_directory_is_never_entered(self, tmp_path):
        store = tmp_path / 'store'
        store.mkdir()
        archived = store / 'RLYA2_HG_20201001-20201003.txt'
        archived.write_text('')
        keep = tmp_path / 'RLYA2_HG_20201004-20201005.txt'
        keep.write_text('')

        matched, skipped = find_files(
            tmp_path, DEFAULT_SEARCH_PATTERN, exclude_dir=store.resolve()
        )

        assert matched == [keep]
        assert archived not in skipped

        matched_all, _ = find_files(tmp_path, DEFAULT_SEARCH_PATTERN)
        assert archived in matched_all


# ======================================================================
# read_noaa_file
# ======================================================================
class TestReadNoaaFile:
    def test_parses_a_normal_file(self, tmp_path):
        path = write_nws(tmp_path, rows('RLYA2', [5.0, 5.5, 6.0]))
        df = read_noaa_file(path, '#')

        assert list(df.columns) == COLUMNS
        assert len(df) == 3
        assert df['LID'].tolist() == ['RLYA2'] * 3
        assert df['PE'].tolist() == ['HG'] * 3
        assert df['Value'].tolist() == [5.0, 5.5, 6.0]
        assert pd.api.types.is_float_dtype(df['Value'])
        assert df['Obstime'].iloc[0] == pd.Timestamp('2020-10-01 00:00:00')

    def test_obstime_is_naive(self, tmp_path):
        df = read_noaa_file(write_nws(tmp_path, rows('RLYA2', [1.0])), '#')
        assert pd.api.types.is_datetime64_any_dtype(df['Obstime'])
        assert df['Obstime'].dt.tz is None

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        path = write_nws(tmp_path, rows('RLYA2', [1.0, 2.0]), comments=True)
        assert len(read_noaa_file(path, '#')) == 2

    def test_file_without_comments(self, tmp_path):
        path = write_nws(tmp_path, rows('RLYA2', [1.0, 2.0]), comments=False)
        assert len(read_noaa_file(path, None)) == 2

    def test_extra_columns_are_dropped(self, tmp_path):
        path = write_nws(
            tmp_path,
            [('RLYA2', 'HG', '2020-10-01 00:00:00', 5.0, 'G')],
            header=HEADER + ' Qual',
        )
        assert list(read_noaa_file(path, '#').columns) == COLUMNS

    def test_column_order_in_file_does_not_matter(self, tmp_path):
        path = write_nws(
            tmp_path,
            [('HG', 'RLYA2', '2020-10-01 00:00:00', 5.0)],
            header='PE LID Obstime Value',
        )
        df = read_noaa_file(path, '#')
        assert df['LID'].iloc[0] == 'RLYA2'
        assert df['PE'].iloc[0] == 'HG'

    def test_non_numeric_values_become_nan_with_a_warning(self, tmp_path, log_records):
        path = write_nws(tmp_path, rows('RLYA2', ['bad', 5.5]))
        df = read_noaa_file(path, '#')

        assert pd.isna(df['Value'].iloc[0])
        assert df['Value'].iloc[1] == 5.5
        assert '1 non-numeric Value' in log_records.warnings

    def test_header_only_file_raises(self, tmp_path):
        path = write_nws(tmp_path, [])
        with pytest.raises(ValueError, match='no data rows|Obstime'):
            read_noaa_file(path, '#')

    def test_unparseable_timestamp_raises(self, tmp_path):
        path = write_nws(tmp_path, [('RLYA2', 'HG', 'notadate 00:00:00', 5.0)])
        with pytest.raises(ValueError, match='Obstime'):
            read_noaa_file(path, '#')

    def test_null_identifier_raises(self, tmp_path):
        path = write_nws(tmp_path, [('NaN', 'HG', '2020-10-01 00:00:00', 5.0)])
        with pytest.raises(ValueError, match='null values'):
            read_noaa_file(path, '#')

    def test_unreadable_file_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match='could not be read'):
            read_noaa_file(tmp_path / 'does_not_exist.txt', '#')

    def test_missing_required_column_raises_value_error(self, tmp_path):
        path = write_nws(
            tmp_path,
            [('RLYA2', 'HG', '2020-10-01 00:00:00')],
            header='LID PE Obstime',
        )
        with pytest.raises(ValueError, match='missing'):
            read_noaa_file(path, '#')


def test_file_creation_time_returns_datetime(tmp_path):
    path = tmp_path / 'f.txt'
    path.write_text('x')
    assert isinstance(ingest_noaa.file_creation_time(path), datetime)


def test_load_existing_parses_obstime(tmp_path):
    path = tmp_path / 'RLYA2.csv'
    pd.DataFrame({
        'LID': ['RLYA2'], 'Obstime': ['2020-10-01 00:00:00'],
        'PE': ['HG'], 'Value': [5.0],
    }).to_csv(path, index=False)

    df = load_existing(path)
    assert pd.api.types.is_datetime64_any_dtype(df['Obstime'])


# ======================================================================
# merge_gage
# ======================================================================
def frame(items, source='new.txt'):
    """items: (PE, 'YYYY-MM-DD', value) tuples."""
    return pd.DataFrame({
        'LID': 'RLYA2',
        'Obstime': pd.to_datetime([i[1] for i in items]),
        'PE': [i[0] for i in items],
        'Value': [float(i[2]) for i in items],
        'source_file': source,
        'time_added': '2026-01-01 00:00:00',
        'source_creation_time': '2026-01-01 00:00:00',
    })


class TestMergeGage:
    def test_no_existing_data(self):
        new = frame([('HG', '2020-10-02', 2.0), ('HG', '2020-10-01', 1.0)])
        merged, changed, n_identical = merge_gage(None, new)

        assert merged['Value'].tolist() == [1.0, 2.0]       # sorted by time
        assert changed.empty
        assert n_identical == 0

    def test_disjoint_records_are_added(self):
        existing = frame([('HG', '2020-10-01', 1.0)], source='old.txt')
        new = frame([('HG', '2020-10-02', 2.0)])
        merged, changed, n_identical = merge_gage(existing, new)

        assert merged['Value'].tolist() == [1.0, 2.0]
        assert changed.empty
        assert n_identical == 0

    def test_identical_records_are_skipped_and_keep_original_provenance(self):
        existing = frame([('HG', '2020-10-01', 1.0), ('HG', '2020-10-02', 2.0)], 'old.txt')
        new = frame([('HG', '2020-10-01', 1.0), ('HG', '2020-10-02', 2.0)], 'new.txt')

        merged, changed, n_identical = merge_gage(existing, new)

        assert len(merged) == 2
        assert n_identical == 2
        assert changed.empty
        assert set(merged['source_file']) == {'old.txt'}

    def test_differences_below_tolerance_count_as_identical(self):
        existing = frame([('HG', '2020-10-01', 5.0)], 'old.txt')
        new = frame([('HG', '2020-10-01', 5.0 + 1e-9)])
        _, changed, n_identical = merge_gage(existing, new)
        assert changed.empty
        assert n_identical == 1

    def test_differences_above_tolerance_conflict(self):
        existing = frame([('HG', '2020-10-01', 5.0)], 'old.txt')
        new = frame([('HG', '2020-10-01', 5.001)])
        with pytest.raises(IngestConflict, match='differ from stored values'):
            merge_gage(existing, new)

    def test_nan_equals_nan(self):
        existing = frame([('HG', '2020-10-01', np.nan)], 'old.txt')
        new = frame([('HG', '2020-10-01', np.nan)])
        _, changed, n_identical = merge_gage(existing, new)
        assert changed.empty
        assert n_identical == 1

    def test_nan_vs_number_is_a_conflict(self):
        existing = frame([('HG', '2020-10-01', np.nan)], 'old.txt')
        new = frame([('HG', '2020-10-01', 5.0)])
        with pytest.raises(IngestConflict):
            merge_gage(existing, new)

    def test_conflict_message_points_at_overwrite_flag(self):
        existing = frame([('HG', '2020-10-01', 5.0)], 'old.txt')
        new = frame([('HG', '2020-10-01', 9.0)])
        with pytest.raises(IngestConflict, match='--overwrite'):
            merge_gage(existing, new)

    def test_overwrite_replaces_value_and_provenance(self):
        existing = frame([('HG', '2020-10-01', 5.0)], 'old.txt')
        new = frame([('HG', '2020-10-01', 9.0)], 'new.txt')

        merged, changed, n_identical = merge_gage(existing, new, overwrite=True)

        assert merged['Value'].tolist() == [9.0]
        assert merged['source_file'].tolist() == ['new.txt']
        assert len(changed) == 1
        assert changed['Value_old'].iloc[0] == 5.0
        assert changed['Value_new'].iloc[0] == 9.0
        assert changed['source_file_old'].iloc[0] == 'old.txt'
        assert changed['source_file_new'].iloc[0] == 'new.txt'
        assert n_identical == 0

    def test_overwrite_does_not_remove_records_absent_from_new_data(self):
        existing = frame(
            [('HG', '2020-10-01', 1.0), ('HG', '2020-10-02', 2.0), ('HG', '2020-10-03', 3.0)],
            'old.txt',
        )
        new = frame([('HG', '2020-10-02', 20.0)])

        merged, _, _ = merge_gage(existing, new, overwrite=True)

        assert merged['Value'].tolist() == [1.0, 20.0, 3.0]

    def test_mixed_identical_changed_and_new(self):
        existing = frame(
            [('HG', '2020-10-01', 1.0), ('HG', '2020-10-02', 2.0), ('HG', '2020-10-03', 3.0)],
            'old.txt',
        )
        new = frame(
            [('HG', '2020-10-02', 2.0), ('HG', '2020-10-03', 30.0), ('HG', '2020-10-04', 4.0)]
        )

        merged, changed, n_identical = merge_gage(existing, new, overwrite=True)

        assert merged['Value'].tolist() == [1.0, 2.0, 30.0, 4.0]
        assert len(changed) == 1
        assert n_identical == 1

    def test_conflict_between_new_files_is_never_allowed(self):
        a = frame([('HG', '2020-10-01', 5.0)], 'a.txt')
        b = frame([('HG', '2020-10-01', 9.0)], 'b.txt')
        new = pd.concat([a, b], ignore_index=True)

        with pytest.raises(IngestConflict, match='between NEW files'):
            merge_gage(None, new)
        with pytest.raises(IngestConflict, match='between NEW files'):
            merge_gage(None, new, overwrite=True)

    def test_identical_duplicates_between_new_files_are_collapsed(self):
        a = frame([('HG', '2020-10-01', 5.0)], 'a.txt')
        b = frame([('HG', '2020-10-01', 5.0)], 'b.txt')

        merged, _, _ = merge_gage(None, pd.concat([a, b], ignore_index=True))

        assert len(merged) == 1
        assert merged['source_file'].iloc[0] == 'a.txt'

    def test_output_is_sorted_by_pe_then_time(self):
        new = frame([
            ('QR', '2020-10-01', 1.0),
            ('HG', '2020-10-02', 2.0),
            ('HG', '2020-10-01', 3.0),
            ('QR', '2020-09-30', 4.0),
        ])
        merged, _, _ = merge_gage(None, new)

        assert merged['PE'].tolist() == ['HG', 'HG', 'QR', 'QR']
        assert merged['Value'].tolist() == [3.0, 2.0, 4.0, 1.0]

    def test_same_time_different_pe_are_different_records(self):
        existing = frame([('HG', '2020-10-01', 1.0)], 'old.txt')
        new = frame([('QR', '2020-10-01', 99.0)])
        merged, changed, _ = merge_gage(existing, new)
        assert len(merged) == 2
        assert changed.empty


# ======================================================================
# write_csv_atomic / setup_logging
# ======================================================================
class TestWriteCsvAtomic:
    def test_writes_without_index(self, tmp_path):
        path = tmp_path / 'out.csv'
        write_csv_atomic(pd.DataFrame({'a': [1, 2]}), path)
        assert path.read_text().splitlines() == ['a', '1', '2']

    def test_replaces_existing_file_and_leaves_no_temp_file(self, tmp_path):
        path = tmp_path / 'out.csv'
        path.write_text('old')
        write_csv_atomic(pd.DataFrame({'a': [1]}), path)

        assert 'old' not in path.read_text()
        assert [p.name for p in tmp_path.iterdir()] == ['out.csv']


class TestSetupLogging:
    def test_console_handler_writes_to_stdout(self, capsys):
        setup_logging(None)
        ingest_noaa.log.info('hello')
        assert 'INFO: hello' in capsys.readouterr().out

    def test_file_handler(self, tmp_path):
        log_file = tmp_path / 'ingest.log'
        setup_logging(log_file)
        ingest_noaa.log.info('hello file')
        for h in ingest_noaa.log.handlers:
            h.flush()
        assert 'hello file' in log_file.read_text()

    def test_does_not_propagate_and_logs_info(self):
        setup_logging(None)
        assert ingest_noaa.log.propagate is False
        assert ingest_noaa.log.level == logging.INFO

    def test_repeated_calls_do_not_stack_handlers(self, tmp_path):
        setup_logging(tmp_path / 'a.log')
        leaked = list(ingest_noaa.log.handlers)
        setup_logging(tmp_path / 'b.log')
        try:
            assert len(ingest_noaa.log.handlers) == 2    # console + file
        finally:
            for h in leaked:
                h.close()

    def test_previous_file_handler_is_closed(self, tmp_path):
        setup_logging(tmp_path / 'a.log')
        first = [h for h in ingest_noaa.log.handlers
                 if isinstance(h, logging.FileHandler)][0]
        setup_logging(tmp_path / 'b.log')
        try:
            assert first.stream is None
        finally:
            first.close()


# ======================================================================
# ingest(): the end-to-end behavior
# ======================================================================
class TestIngestHappyPath:
    def test_first_ingest_creates_store(self, env, log_records):
        write_nws(env.src, rows('RLYA2', [5.0, 5.5, 6.0]))

        assert run(env) == 0

        df = stored(env)
        assert len(df) == 3
        assert df['Value'].tolist() == [5.0, 5.5, 6.0]
        assert df['Obstime'].iloc[0] == pd.Timestamp('2020-10-01')
        assert {'source_file', 'time_added', 'source_creation_time'} <= set(df.columns)
        assert (env.store / 'ingest.log').exists()
        assert 'Summary' in log_records.text

    def test_writes_metadata_file(self, env):
        write_nws(env.src, rows('RLYA2', [5.0]))
        run(env)

        meta = Metadata.load(env.store, 'RLYA2')
        assert meta is not None
        assert meta.gage_name == 'RLYA2 River'

    def test_archives_source_files(self, env):
        write_nws(env.src, rows('RLYA2', [5.0]))
        run(env)

        copies = list((env.store / 'archive' / 'sources').rglob('RLYA2_HG_*.txt'))
        assert len(copies) == 1

    def test_rerunning_the_same_file_is_a_noop(self, env, log_records):
        write_nws(env.src, rows('RLYA2', [5.0, 5.5, 6.0]))
        assert run(env) == 0
        before = stored(env)

        assert run(env) == 0

        pd.testing.assert_frame_equal(stored(env), before)
        assert '+0 new rows, 3 identical (skipped), 0 overwritten' in log_records.text

    def test_new_file_appends_only_new_records(self, env, log_records):
        write_nws(env.src, rows('RLYA2', [5.0, 5.5, 6.0]))
        run(env)
        (env.src / 'RLYA2_HG_20201001-20201003.txt').unlink()
        log_records.clear()
        # Oct 3 overlaps (identical), Oct 4-5 are new
        write_nws(env.src, rows('RLYA2', [6.0, 6.5, 7.0], start='2020-10-03'),
                  tag='20201003-20201005')

        assert run(env) == 0

        assert stored(env)['Value'].tolist() == [5.0, 5.5, 6.0, 6.5, 7.0]
        assert '+2 new rows, 1 identical' in log_records.text

    def test_multiple_gages_in_one_file(self, env):
        write_nws(env.src, rows('RLYA2', [1.0, 2.0]) + rows('SRDA2', [3.0, 4.0]))
        assert run(env) == 0
        assert stored(env, 'RLYA2')['Value'].tolist() == [1.0, 2.0]
        assert stored(env, 'SRDA2')['Value'].tolist() == [3.0, 4.0]

    def test_multiple_variables_are_sorted_by_pe_then_time(self, env):
        write_nws(env.src, [
            ('RLYA2', 'QR', '2020-10-01 00:00:00', 10.0),
            ('RLYA2', 'HG', '2020-10-02 00:00:00', 2.0),
            ('RLYA2', 'HG', '2020-10-01 00:00:00', 1.0),
            ('RLYA2', 'QR', '2020-10-02 00:00:00', 20.0),
        ])
        run(env)
        assert stored(env)['PE'].tolist() == ['HG', 'HG', 'QR', 'QR']

    def test_no_files_is_not_an_error(self, env, log_records):
        assert run(env) == 0
        assert 'No usable data files' in log_records.text

    def test_unmatched_and_spreadsheet_files_are_skipped(self, env, log_records):
        write_nws(env.src, rows('RLYA2', [5.0]))
        (env.src / 'notes.txt').write_text('hi')
        (env.src / 'RLYA2_data.xlsx').write_text('not really excel')

        assert run(env) == 0

        text = log_records.text
        assert 'Skipped (name does not match pattern)' in text
        assert 'Skipped spreadsheet' in text
        assert 'Files: 1 ingested, 2 skipped, 0 bad' in text

    def test_storage_directory_inside_search_directory_is_not_reingested(
        self, env, log_records
    ):
        inside = env.src / 'store'
        env.config.write_text(json.dumps({'NOAA_DATA_PATH': str(inside)}))
        write_nws(env.src, rows('RLYA2', [5.0, 5.5]))
        assert run(env) == 0
        log_records.clear()

        assert run(env) == 0

        # If the archived copy under store/archive/sources were picked up,
        # this would say '2 ingested'.
        assert 'Files: 1 ingested, 0 skipped, 0 bad' in log_records.text

    def test_uses_find_config_when_no_config_path_given(self, env, monkeypatch):
        monkeypatch.setattr(ingest_noaa, 'find_config', lambda: env.config)
        write_nws(env.src, rows('RLYA2', [5.0]))
        assert ingest(env.src) == 0
        assert (env.store / 'RLYA2.csv').exists()


class TestIngestConflicts:
    @pytest.fixture
    def seeded(self, env):
        write_nws(env.src, rows('RLYA2', [5.0, 5.5, 6.0]))
        assert run(env) == 0
        (env.src / 'RLYA2_HG_20201001-20201003.txt').unlink()
        return env

    def test_conflict_with_stored_data_aborts_and_changes_nothing(self, seeded, log_records):
        env = seeded
        before = (env.store / 'RLYA2.csv').read_bytes()
        write_nws(env.src, rows('RLYA2', [99.0], start='2020-10-03'), tag='20201003-20201003')

        assert run(env) == 2

        assert (env.store / 'RLYA2.csv').read_bytes() == before
        assert not list((env.store / 'archive').glob('RLYA2_*.csv'))
        assert not (env.store / 'RLYA2_overwrites.csv').exists()
        text = log_records.text
        assert 'CONFLICT [RLYA2]' in text
        assert 'differ from stored values' in text
        assert 'Nothing was written' in text

    def test_overwrite_replaces_backs_up_and_audits(self, seeded, log_records):
        env = seeded
        old_bytes = (env.store / 'RLYA2.csv').read_bytes()
        write_nws(env.src, rows('RLYA2', [6.5], start='2020-10-03'), tag='20201003-20201003')

        assert run(env, overwrite_gages=['RLYA2']) == 0

        df = stored(env)
        # Replaced value, and records missing from the new file are kept
        assert df['Value'].tolist() == [5.0, 5.5, 6.5]

        backups = list((env.store / 'archive').glob('RLYA2_*.csv'))
        assert len(backups) == 1
        assert backups[0].read_bytes() == old_bytes

        audit = pd.read_csv(env.store / 'RLYA2_overwrites.csv')
        assert len(audit) == 1
        assert audit['Value_old'].iloc[0] == 6.0
        assert audit['Value_new'].iloc[0] == 6.5
        assert 'time_overwritten' in audit.columns
        assert 'overwrote 1 records' in log_records.text

    def test_audit_file_accumulates_with_a_single_header(self, seeded):
        env = seeded
        write_nws(env.src, rows('RLYA2', [6.5], start='2020-10-03'), tag='20201003-20201003')
        run(env, overwrite_gages=['RLYA2'])
        (env.src / 'RLYA2_HG_20201003-20201003.txt').unlink()
        write_nws(env.src, rows('RLYA2', [7.5], start='2020-10-03'), tag='20201003-20201004')
        run(env, overwrite_gages=['RLYA2'])

        audit_path = env.store / 'RLYA2_overwrites.csv'
        audit = pd.read_csv(audit_path)
        assert audit['Value_new'].tolist() == [6.5, 7.5]
        assert audit_path.read_text().count('Value_old') == 1

    def test_overwrite_only_applies_to_the_named_gage(self, seeded):
        env = seeded
        write_nws(env.src, rows('RLYA2', [99.0], start='2020-10-03'), tag='20201003-20201003')
        assert run(env, overwrite_gages=['SOMEO']) == 2

    def test_conflict_between_new_files_aborts_even_with_overwrite(self, env, log_records):
        write_nws(env.src, rows('RLYA2', [5.0]), tag='20201001-20201001')
        write_nws(env.src, rows('RLYA2', [9.0]), tag='20201001-20201002')

        assert run(env, overwrite_gages=['RLYA2']) == 2

        assert not (env.store / 'RLYA2.csv').exists()
        assert 'between NEW files' in log_records.text

    def test_a_conflict_in_one_gage_blocks_writes_for_every_gage(self, seeded):
        env = seeded
        before = (env.store / 'RLYA2.csv').read_bytes()
        other = env.root / 'src2'
        write_nws(other, rows('SRDA2', [1.0, 2.0]), gage='SRDA2')                      # fine
        write_nws(other, rows('RLYA2', [99.0], start='2020-10-03'), tag='20201003-20201003')  # conflicts

        assert run(env, src=other) == 2

        assert not (env.store / 'SRDA2.csv').exists()
        assert (env.store / 'RLYA2.csv').read_bytes() == before

    def test_unused_overwrite_gage_logs_a_warning(self, env, log_records):
        write_nws(env.src, rows('RLYA2', [5.0]))
        assert run(env, overwrite_gages=['ZZZZZ']) == 0
        assert 'ZZZZZ' in log_records.warnings


class TestIngestDryRun:
    def test_writes_nothing(self, env, log_records):
        write_nws(env.src, rows('RLYA2', [5.0, 5.5]))

        assert run(env, dry_run=True) == 0

        assert not env.store.exists()
        assert 'DRY RUN' in log_records.text
        assert '(dry run)' in log_records.text
        assert '+2 new rows' in log_records.text

    def test_does_not_save_metadata_but_still_looks_it_up(self, env, meta_stub):
        write_nws(env.src, rows('RLYA2', [5.0]))
        run(env, dry_run=True)

        assert [c['gage_id'] for c in meta_stub.calls] == ['RLYA2']
        assert meta_stub.calls[0]['write_file'] is False
        assert meta_stub.calls[0]['refresh'] is True
        assert not env.store.exists()

    def test_reports_would_be_overwrites_without_changing_anything(self, env, log_records):
        write_nws(env.src, rows('RLYA2', [5.0]))
        run(env)
        (env.src / 'RLYA2_HG_20201001-20201003.txt').unlink()
        before = (env.store / 'RLYA2.csv').read_bytes()
        write_nws(env.src, rows('RLYA2', [9.0]), tag='20201001-20201001')
        log_records.clear()

        assert run(env, dry_run=True, overwrite_gages=['RLYA2']) == 0

        assert 'would overwrite 1 records' in log_records.text
        assert (env.store / 'RLYA2.csv').read_bytes() == before
        assert not (env.store / 'RLYA2_overwrites.csv').exists()

    def test_still_detects_conflicts(self, env):
        write_nws(env.src, rows('RLYA2', [5.0]), tag='20201001-20201001')
        write_nws(env.src, rows('RLYA2', [9.0]), tag='20201001-20201002')
        assert run(env, dry_run=True) == 2


class TestIngestBadFiles:
    def test_bad_file_aborts_the_run_by_default(self, env, log_records):
        write_nws(env.src, rows('RLYA2', [5.0]))
        write_nws(env.src, [], gage='BADFL')           # header only: unreadable

        assert run(env) == 1

        assert not (env.store / 'RLYA2.csv').exists()
        assert 'Nothing was written' in log_records.text

    def test_skip_bad_files_ingests_the_good_ones_but_still_returns_1(self, env, log_records):
        write_nws(env.src, rows('RLYA2', [5.0]))
        write_nws(env.src, [], gage='BADFL')

        assert run(env, skip_bad_files=True) == 1

        assert stored(env)['Value'].tolist() == [5.0]
        assert 'Files: 1 ingested, 0 skipped, 1 bad' in log_records.text

    def test_only_bad_files_returns_1(self, env):
        write_nws(env.src, [], gage='BADFL')
        assert run(env, skip_bad_files=True) == 1

    def test_file_missing_a_column_counts_as_a_bad_file(self, env):
        write_nws(env.src, rows('RLYA2', [5.0]))
        write_nws(
            env.src, [('BADFL', 'HG', '2020-10-01 00:00:00')],
            gage='BADFL', header='LID PE Obstime',
        )

        assert run(env, skip_bad_files=True) == 1
        assert stored(env)['Value'].tolist() == [5.0]


class TestIngestMetadata:
    def test_metadata_failure_warns_but_data_is_still_written(
        self, env, meta_stub, log_records
    ):
        meta_stub.fail = True
        write_nws(env.src, rows('RLYA2', [5.0]))

        assert run(env) == 0

        assert (env.store / 'RLYA2.csv').exists()
        assert not (env.store / 'RLYA2_meta.json').exists()
        assert 'Metadata not retrieved for RLYA2' in log_records.warnings


class TestIngestConfig:
    def test_missing_property_raises_runtime_error(self, env):
        env.config.write_text('{}')
        with pytest.raises(RuntimeError, match='NOAA_DATA_PATH'):
            run(env)

    def test_missing_config_file_raises(self, env):
        env.config.unlink()
        with pytest.raises(FileNotFoundError):
            run(env)

    def test_invalid_json_raises_value_error(self, env):
        env.config.write_text('{not json')
        with pytest.raises(ValueError):
            run(env)


# ======================================================================
# The module's own argparse entry point
# ======================================================================
class TestModuleCli:
    def run_cli(self, monkeypatch, argv, returns=0):
        captured = {}

        def fake_ingest(search_dir, **kwargs):
            captured['search_dir'] = search_dir
            captured.update(kwargs)
            return returns

        monkeypatch.setattr(ingest_noaa, 'ingest', fake_ingest)
        monkeypatch.setattr(sys, 'argv', ['ingest_noaa', *argv])
        with pytest.raises(SystemExit) as exc:
            ingest_noaa.cli()
        return exc.value.code, captured

    def test_defaults(self, monkeypatch, tmp_path):
        code, captured = self.run_cli(monkeypatch, [str(tmp_path)])

        assert code == 0
        assert captured['search_dir'] == str(tmp_path)
        assert captured['search_pattern'] == DEFAULT_SEARCH_PATTERN
        assert captured['config_path'] is None
        assert captured['overwrite_gages'] == []
        assert captured['dry_run'] is False
        assert captured['skip_bad_files'] is False

    def test_all_flags_and_exit_code_passthrough(self, monkeypatch, tmp_path):
        code, captured = self.run_cli(
            monkeypatch,
            [str(tmp_path), '--dry-run', '--skip-bad-files',
             '--overwrite', 'A', '--overwrite', 'B',
             '--config', 'c.json', '--search-pattern', 'x.*'],
            returns=2,
        )

        assert code == 2
        assert captured['dry_run'] is True
        assert captured['skip_bad_files'] is True
        assert captured['overwrite_gages'] == ['A', 'B']
        assert captured['config_path'] == 'c.json'
        assert captured['search_pattern'] == 'x.*'

    def test_search_dir_is_required(self, monkeypatch):
        monkeypatch.setattr(sys, 'argv', ['ingest_noaa'])
        with pytest.raises(SystemExit) as exc:
            ingest_noaa.cli()
        assert exc.value.code == 2