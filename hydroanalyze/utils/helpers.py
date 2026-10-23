"""
Hydroanalyze utilities

Author: Sam Hooper
Create date: 2026-10-21
Contact: samuel_hooper@nps.gov
"""

from pathlib import Path
from os import PathLike
from typing import Optional


def find_config(
        name:str|PathLike='config/hydroanalyze_config.json'
    ) -> PathLike:
    """
    Search the parents of the current file for a config file path
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / name
        if candidate.exists():
            return candidate
    raise FileNotFoundError(name)


def validate_config(
        config:dict, 
        properties:Optional[list]=[], 
        raise_error:bool=True
    ) -> bool:
    """
    Check that the config has all the required properties
    """
    # Check all properties if not given to check
    default_properties = [
        'NOAA_CDO_API_TOKEN',
        'SSL_CERT_PATH',
        'NOAA_DATA_PATH'
    ]
    properties = properties or default_properties
    missing_properties = []
    for prop in  properties:
        if not prop in config:
            missing_properties.append(prop)

    is_valid = len(missing_properties) == 0
    if not is_valid and raise_error:
        raise ValueError(
            'config is missing the following properties:' +
            ", ".join(missing_properties)
        )
    
    return is_valid