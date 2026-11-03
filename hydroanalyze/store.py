from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from os import PathLike
from pathlib import Path

import pandas as pd

from metadata import Metadata
from utils.helpers import get_noaa_data_path

# Matches <LID>.csv only, so <LID>_overwrites.csv, ingest.log and the
# archive folder are never mistaken for gages.
GAGE_FILE = re.compile(r"[A-Z0-9]{5}\.csv")


def gage_csv_path(data_dir: str | PathLike, gage_id: str) -> Path:
    """
    The one place the data filename is defined.
    """
    return Path(data_dir) / f"{gage_id}.csv"


@dataclass
class GageSummary:
    gage_id: str
    gage_name: str | None = None
    state: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    start: pd.Timestamp | None = None   # only filled with details=True
    end: pd.Timestamp | None = None
    n_records: int | None = None
    has_metadata: bool = False


# list_noaa_gages display options
DISPLAY_OPTIONS = ['id', 'info', 'details']


def list_noaa_gages(
        data_dir: str | PathLike | None, 
        display: str = 'id',
    ) -> list[GageSummary]:
    """
    List all NOAA gages that have data in the specified directory.

    :param data_dir: The directory containing NOAA gage CSV files. If none
        specified, use the default NOAA data path from the project config.
    :param display: The display option for the gage summary. Valid options are 'id', 'info', and 'details'.
    :return: A list of GageSummary objects representing the gages found in the specified directory.
    """

    if data_dir is None:
        data_dir = get_noaa_data_path()
    data_dir = Path(data_dir)
    if not data_dir.exists():
        raise ValueError(f'NOAA data directory {data_dir} does not exist.')

    if not display in DISPLAY_OPTIONS:
        raise ValueError(
            f'Invalid display option {display}. Valid options are ' + 
            ', '.join(DISPLAY_OPTIONS)
        )
    
    gages = []
    for csv in sorted(data_dir.iterdir()):
        if not (csv.is_file() and GAGE_FILE.fullmatch(csv.name)):
            continue
        gage_id = csv.stem
        # If this is just a query of gage IDs, skip loading metadata and 
        #   getting other details
        if display == 'id':
            gages.append(GageSummary(gage_id=gage_id))
            continue

        meta = Metadata.load(data_dir, gage_id)
        g = GageSummary(
            gage_id=gage_id,
            has_metadata=meta is not None,
            gage_name=getattr(meta, 'gage_name', None),
            state=getattr(meta, 'state', None),
            latitude=getattr(meta, 'latitude', None),
            longitude=getattr(meta, 'longitude', None),
        )
        if display == 'details':
            obs = pd.read_csv(
                csv, usecols=['Obstime'], parse_dates=['Obstime']
            ).Obstime
            g.start, g.end, g.n_records = obs.min(), obs.max(), len(obs)
        gages.append(g)

    return gages