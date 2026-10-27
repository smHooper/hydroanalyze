"""
USGS AND NOAA API clients to retrieve gage data

Author: Sam Hooper
Create date: 2026-10-21
Contact: samuel_hooper@nps.gov
"""

import datetime as dt
import requests
from typing import Dict


class USGSClient:
    BASE_URL = 'https://waterservices.usgs.gov/nwis/dv'

    def fetch_data(self, gage_id: str) -> bytes:
        today = dt.date.today()
        params = {
            'format': 'waterml,1.1',
            'sites': gage_id,
            'startDT': '1851-01-01',
            'endDT': today.isoformat(),
        }

        response = requests.get(self.BASE_URL, params=params)
        response.raise_for_status()
        
        return response.content


class NOAAClient:
    """
    NOAA doesn't make historical time series data publicly available for their 
    gages via API because they don't have the funding for rigorous QA/QC. 
    Instead, they provide data upon direct request, which is then processed and
    saved as text files. This class provides a method to fetch metadata for a 
    given gage ID, but the actual time series data must be loaded from local 
    files.
    """
    BASE_URL = 'https://api.water.noaa.gov/nwps/v1'

    def get_metadata(self, gage_id: str) -> Dict:
        url = f"{self.BASE_URL}/gauges/{gage_id}"
        response = requests.get(url)
        response.raise_for_status()
        
        return response.json()