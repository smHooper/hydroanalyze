"""
Thin wrapper for utils/ingest_noaa.py to allow relative imports

Author: Sam Hooper
Create date: 2026-10-22
Contact: samuel_hooper@nps.gov
"""

import sys
from utils.ingest_noaa import cli

sys.exit(cli())