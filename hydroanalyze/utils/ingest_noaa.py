"""
Read QCed NWS data and store it in a canonical format.

Data is stored in the directory given by the NOAA_DATA_PATH property in the
config file. Each gage ID gets its own CSV file. Records are keyed on
(LID, PE, Obstime) and are IMMUTABLE by default:

  * key not in stored data                  -> added
  * key in stored data, same value          -> skipped silently
  * key in stored data, different value     -> run aborts, nothing is written
  * same key, different values in two files
    from this run                           -> run aborts (even with --overwrite)

Use --overwrite GAGE to deliberately replace stored values for a specific gage
(e.g. NWS sent re-QCed data). Before replacing, the old CSV is copied to
<storage>/archive/ and every changed record is appended to
<storage>/<GAGE>_overwrites.csv.

Note: overwrite replaces records that exist in both places. It does NOT remove
stored records that are absent from the new file.

Run as a module (the relative import requires it), e.g.:
    python -m utils.ingest_noaa /path/to/nws/files --dry-run
or use the root-level wrapper hydroanalyze/ingest_noaa_cli.py

Author: Sam Hooper
Create date: 2026-10-21
Contact: samuel_hooper@nps.gov
"""
from __future__ import annotations

import logging
import json
import os
import re
import shutil
import sys
from argparse import ArgumentParser
from datetime import datetime
from os import PathLike
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from utils.helpers import find_config, validate_config
from clients import NOAAClient
from metadata import ensure_noaa_metadata

DATA_STORAGE_CONFIG_PROP = 'NOAA_DATA_PATH'
COLUMNS = ['LID', 'Obstime', 'PE', 'Value']
KEY = ['LID', 'PE', 'Obstime']
DEFAULT_SEARCH_PATTERN = r'[A-Z0-9]{5}_[A-Z]+_\d{8}-\d{8}\.txt'
VALUE_ATOL = 1e-6  # values closer than this are considered unchanged
SPREADSHEET_SUFFIXES = {'.xlsx', '.xls', '.xlsm'}

log = logging.getLogger('noaa_ingest')


class IngestConflict(Exception):
    """
    Raised when new data conflicts with other new data or stored data.
    """


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def find_files(
        search_dir: str | PathLike,
        pattern: str,
        exclude_dir: Optional[Path] = None,
    ) -> tuple[list[Path], list[Path]]:
    """
    Return (matched, skipped) file paths under search_dir.
    """
    matched, skipped = [], []
    for root, dirs, files in os.walk(search_dir):
        # Never descend into the storage dir (would re-ingest the archive)
        if exclude_dir is not None:
            dirs[:] = [
                d for d in dirs if (Path(root) / d).resolve() != exclude_dir
            ]
        for name in sorted(files):
            path = Path(root) / name
            if re.fullmatch(pattern, name):
                matched.append(path)
            else:
                skipped.append(path)
    
    return matched, skipped


def read_noaa_file(file_path: Path, comment_char: Optional[str]) -> pd.DataFrame:
    """
    Read one NWS file into a validated DataFrame with COLUMNS.

    Raises ValueError (with a message naming the problem) if the file can't
    be used.
    """
    # The Obstime column unfortunately has a space in the timestamp even though the file is fixed-wdith file and the only thing separating columns is whitespace. Read the header line and replace 'Obstime' with 'date' and 'time'
    try:
        # Find the header line (first non-blank, non-comment line)
        header = []
        with open(file_path) as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    header = line.split()
                    break
    except Exception as e:
        raise ValueError(f'could not be read: {e}') from e

    # Expand Obstime into two columns to match the data rows
    names = []
    for col in header:
        if col == "Obstime":
            names += ["Date", "Time"]
        else:
            names.append(col)

    try:
        df = pd.read_csv(
            file_path,
            sep=r'\s+',
            header=0,       # replace original header row
            names=names,    # with new column names
            comment=comment_char,
            skip_blank_lines=True,
        )
    except Exception as e:
        raise ValueError(f'could not be parsed as tab-delimited text: {e}') from e
    
    # Combine Date and Time columns back into one Obstime and drop them
    try:
        df["Obstime"] = pd.to_datetime(df["Date"] + " " + df["Time"], utc=True)
    except (KeyError, ValueError, TypeError) as e:
        raise ValueError(f"could not build Obstime from Date/Time: {e}") from e
    df = df.drop(columns=["Date", "Time"])

    df.columns = df.columns.str.strip()
    if df.empty:
        raise ValueError('file contains no data rows')

    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f'missing expected columns: {missing}')

    df = df[COLUMNS].copy()

    if df[['LID', 'PE', 'Obstime']].isna().any().any():
        raise ValueError('null values in LID, PE, or Obstime')
    for col in ('LID', 'PE'):
        df[col] = df[col].astype(str).str.strip()

    try:
        df['Obstime'] = pd.to_datetime(df['Obstime'])
    except Exception as e:
        raise ValueError(f'unparseable Obstime values: {e}') from e
    if df['Obstime'].dt.tz is not None:
        # Store everything as naive UTC so timestamps compare consistently
        df['Obstime'] = df['Obstime'].dt.tz_convert('UTC').dt.tz_localize(None)

    raw_value = df['Value']
    df['Value'] = pd.to_numeric(raw_value, errors='coerce')
    n_coerced = int((df['Value'].isna() & raw_value.notna()).sum())
    if n_coerced:
        log.warning(
            '%s: %d non-numeric Value entries were set to NaN',
            file_path, n_coerced
        )
    
    return df


def file_creation_time(file_path: Path) -> datetime:
    """
    Best-effort creation time (provenance only; never used for decisions).
    """
    st = file_path.stat()
    ts = st.st_birthtime if hasattr(st, 'st_birthtime') else st.st_ctime
    return datetime.fromtimestamp(ts)


def load_existing(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df['Obstime'] = pd.to_datetime(df['Obstime'])
    return df


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------
def _show(df: pd.DataFrame, n: int = 10) -> str:
    return df.head(n).to_string(index=False)


def merge_gage(
        existing: Optional[pd.DataFrame],
        new: pd.DataFrame,
        overwrite: bool = False,
    ) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """
    Merge new records for one gage into its stored records.

    Returns (merged, changed, n_identical):
      merged       full DataFrame to store
      changed      rows whose stored value is being replaced (old/new values)
      n_identical  new rows that matched a stored record exactly (skipped)

    Raises IngestConflict on any conflict that isn't an allowed overwrite.
    """
    if existing is None:
        existing = new.iloc[0:0]

    # 1. Conflicts inside the new batch are never resolved automatically.
    new_unique = new.drop_duplicates(subset=KEY + ['Value'])
    internal = new_unique[new_unique.duplicated(subset=KEY, keep=False)]
    if not internal.empty:
        raise IngestConflict(
            f'{len(internal)} rows conflict between NEW files '
            '(same LID/PE/Obstime, different Value). Remove the bad file '
            'and rerun:\n'
            + _show(internal.sort_values(KEY)[KEY + ['Value', 'source_file']])
        )

    # 2. Compare new records against stored records on the key.
    both = existing.merge(new_unique, on=KEY, suffixes=('_old', '_new'))
    same = np.isclose(
        both['Value_old'].to_numpy(dtype=float),
        both['Value_new'].to_numpy(dtype=float),
        rtol=0, atol=VALUE_ATOL, equal_nan=True,
    )
    changed = both[~same]
    n_identical = int(same.sum())

    if not changed.empty and not overwrite:
        raise IngestConflict(
            f'{len(changed)} new records differ from stored values. '
            'If this is intentional (e.g. re-QCed data), rerun with '
            '--overwrite for this gage.\n'
            + _show(changed[KEY + [
                'Value_old', 'Value_new', 'source_file_old', 'source_file_new'
            ]])
        )

    # 3. Drop only the changed keys from stored data. Identical rows keep
    #    their original provenance (source_file, time_added, ...).
    if not changed.empty:
        existing = (
            existing.merge(changed[KEY], on=KEY, how='left', indicator=True)
            .query('_merge == "left_only"')
            .drop(columns='_merge')
        )

    # Any remaining duplicate keys are value-identical, so keep='first'
    # (stored rows come first) is safe.
    merged = (
        pd.concat([existing, new_unique], ignore_index=True)
        .drop_duplicates(subset=KEY, keep='first')
        .sort_values(['PE', 'Obstime'])
        .reset_index(drop=True)
    )
    
    return merged, changed, n_identical


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
def write_csv_atomic(df: pd.DataFrame, path: Path) -> None:
    """
    Write via a temp file so a crash can't leave a truncated CSV.
    """

    tmp = path.with_name(path.name + '.tmp')
    df.to_csv(tmp, index=False)
    tmp.replace(path)


def setup_logging(log_file: Optional[Path]) -> None:
    log.handlers.clear()
    log.setLevel(logging.INFO)
    log.propagate = False
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    log.addHandler(console)
    if log_file is not None:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(
            logging.Formatter('%(asctime)s %(levelname)s %(message)s')
        )
        log.addHandler(fh)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def ingest(
        search_dir: str | PathLike,
        search_pattern: str = DEFAULT_SEARCH_PATTERN,
        config_path: Optional[str | PathLike] = None,
        comment_char: Optional[str] = '#',
        overwrite_gages: Iterable[str] = (),
        dry_run: bool = False,
        skip_bad_files: bool = False,
    ) -> int:
    """
    Ingest NWS files. Returns an exit code: 0 ok, 1 bad files, 2 conflict.
    """
    overwrite_gages = set(overwrite_gages)

    # --- config ---------------------------------------------------------
    if not config_path:
        config_path = find_config()
    with open(config_path) as f:
        config = json.load(f)
    try:
        validate_config(config, properties=[DATA_STORAGE_CONFIG_PROP])
    except Exception as e:
        raise RuntimeError(
            f'config file at {config_path} missing {DATA_STORAGE_CONFIG_PROP}'
        ) from e

    storage_dir = Path(config[DATA_STORAGE_CONFIG_PROP])
    archive_dir = storage_dir / 'archive'
    run_time = datetime.now()
    run_stamp = run_time.strftime('%Y%m%dT%H%M%S')

    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(None if dry_run else storage_dir / 'ingest.log')
    if dry_run:
        log.info('DRY RUN: nothing will be written')

    # --- read files -----------------------------------------------------
    matched, skipped = find_files(
        search_dir, search_pattern, exclude_dir=storage_dir.resolve()
    )
    
    frames, read_ok, bad_files = [], [], []
    for file_path in matched:
        log.info('Processing %s', file_path)
        try:
            df = read_noaa_file(file_path, comment_char)
        except ValueError as e:
            log.error('%s: %s', file_path, e)
            bad_files.append(file_path)
            continue
        df['source_file'] = str(file_path)
        df['time_added'] = run_time.strftime('%Y-%m-%d %H:%M:%S')
        df['source_creation_time'] = file_creation_time(file_path)
        frames.append(df)
        read_ok.append(file_path)

    for p in skipped:
        if p.suffix.lower() in SPREADSHEET_SUFFIXES:
            log.warning('Skipped spreadsheet (not ingested; looks like data?): %s', p)
        else:
            log.info('Skipped (name does not match pattern): %s', p)

    if bad_files and not skip_bad_files:
        log.error(
            '%d file(s) could not be read. Nothing was written. Fix or move '
            'them, or rerun with --skip-bad-files.', len(bad_files)
        )
        return 1
    if not frames:
        log.warning('No usable data files found. Nothing to do.')
        return 1 if bad_files else 0

    new = pd.concat(frames, ignore_index=True)

    unused = overwrite_gages - set(new['LID'])
    if unused:
        log.warning(
            '--overwrite given for %s but no new data found for them',
            sorted(unused)
        )

    # --- plan every gage before writing anything -------------------------
    plans, conflicts = {}, []
    for gage_id, gage_new in new.groupby('LID'):
        path = storage_dir / f'{gage_id}.csv'
        existing = load_existing(path) if path.exists() else None
        try:
            merged, changed, n_identical = merge_gage(
                existing, gage_new, overwrite=gage_id in overwrite_gages
            )
        except IngestConflict as e:
            conflicts.append(f'[{gage_id}] {e}')
            continue
        n_before = 0 if existing is None else len(existing)
        plans[gage_id] = {
            'path': path,
            'merged': merged,
            'changed': changed,
            'n_added': len(merged) - n_before,
            'n_identical': n_identical,
            'had_existing': existing is not None,
        }

    if conflicts:
        for c in conflicts:
            log.error('CONFLICT %s', c)
        log.error('Nothing was written.')
        return 2

    # --- write ---------------------------------------------------------
    for gage_id, p in plans.items():
        changed = p['changed']
        
        # Get metadata, but don't save just yet. If this is a dry run, save 
        #   shouldn't happen at all
        metadata = ensure_noaa_metadata(
            gage_id, 
            storage_dir, 
            log=log,
            refresh=True,
            write_file=False,
        )
        metadata_retrieved = metadata is not None and metadata.raw
        if not metadata_retrieved:
            log.warning('Metadata not retrieved for %s', gage_id)

        if not dry_run:
            if not changed.empty:
                backup = archive_dir / f'{gage_id}_{run_stamp}.csv'
                shutil.copy2(p['path'], backup)
                audit = storage_dir / f'{gage_id}_overwrites.csv'
                (changed[KEY + ['Value_old', 'Value_new',
                                'source_file_old', 'source_file_new']]
                    .assign(time_overwritten=run_time.strftime('%Y-%m-%d %H:%M:%S'))
                    .to_csv(audit, mode='a', header=not audit.exists(), index=False))
                log.warning(
                    '%s: overwrote %d records (old file backed up to %s)',
                    gage_id, len(changed), backup
                )
            csv_path = p['path']
            write_csv_atomic(p['merged'], csv_path)

            # Only save the metadata if it's actually been fetched (.raw is a 
            #   dictionary with the raw NOAA metadata)
            if metadata_retrieved:
                metadata.save(csv_path.parent)

        elif not changed.empty:
            log.warning('%s: would overwrite %d records', gage_id, len(changed))

    if not dry_run:
        for file_path in read_ok:
            try:
                rel = file_path.relative_to(Path(search_dir))
            except ValueError:
                rel = Path(file_path.name)
            dest = archive_dir / 'sources' / run_stamp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dest)

    # --- summary -------------------------------------------------------
    log.info('---- Summary%s ----', ' (dry run)' if dry_run else '')
    log.info(
        'Files: %d ingested, %d skipped, %d bad',
        len(read_ok), len(skipped), len(bad_files)
    )
    for gage_id, p in plans.items():
        log.info(
            '%s: +%d new rows, %d identical (skipped), %d overwritten',
            gage_id, p['n_added'], p['n_identical'], len(p['changed'])
        )
    return 1 if bad_files else 0


def cli() -> None:
    parser = ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('search_dir', help='Directory to search for data in')
    parser.add_argument(
        '--search-pattern',
        default=DEFAULT_SEARCH_PATTERN,
        help='regex that file names must fully match (default: %(default)s)',
    )
    parser.add_argument(
        '--config',
        default=None,
        help=(
            'path to configuration file. If none given, '
            'utils.helpers.find_config() is used'
        ),
    )
    parser.add_argument(
        '--overwrite',
        action='append',
        default=[],
        metavar='GAGE',
        help=(
            'allow new values to replace stored values for this gage ID '
            '(repeat the flag for multiple gages)'
        ),
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='report what would happen without writing anything',
    )
    parser.add_argument(
        '--skip-bad-files',
        action='store_true',
        help='ingest the good files even if some matching files are unreadable',
    )
    args = parser.parse_args()

    sys.exit(ingest(
        args.search_dir,
        search_pattern=args.search_pattern,
        config_path=args.config,
        overwrite_gages=args.overwrite,
        dry_run=args.dry_run,
        skip_bad_files=args.skip_bad_files,
    ))


if __name__ == '__main__':
    # Only run the CLI if this file is executed directly.
    cli()
