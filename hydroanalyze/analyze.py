from __future__ import annotations

import datetime as dt
import json
import requests
import pandas as pd
from pathlib import Path
from os import PathLike
from typing import Dict, List, Tuple, Optional, Self
import warnings
import xml.etree.ElementTree as ET

from clients import (
    NOAAClient,
    USGSClient
)
from metadata import (
    ensure_noaa_metadata,
    Metadata,
    USGSMetadata,
    XML_NAMESPACES
)

INT_DTYPE = 'Int64'


# ==========================================================
# File Management
# ==========================================================
class FileManager:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def ensure_directory(self, name: str) -> Path:
        path = self.base_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def unique_filename(self, path: Path) -> Path:
        counter = 1
        new_path = path

        while new_path.exists():
            new_path = path.with_stem(f'{path.stem}_{counter}')
            counter += 1

        return new_path


# ==========================================================
# River Data Model
# ==========================================================
class RiverData:

    VARIABLE_MAP = {
        "00060": "discharge_cfs",
        "00010": "temp_c",
        "HG"   : "stage_ft"
    }

    STAT_MAP = {
        "Mean": "mean",
        "Maximum": "max",
        "Minimum": "min",
    }
    WATER_YEAR_MONTHS = [10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    NON_VARIABLE_COLUMNS = [
        'year', 
        'water_year', 
        'water_doy', 
        'day_of_year'
    ]

    def __init__(self, gage_id: str, base_dir: Path):
        self.gage_name: str | None = None
        self.gage_id: str = gage_id
        self._metadata: Metadata | None = None
        self.file_manager = FileManager(base_dir)

        self.data = pd.DataFrame()
        self._record_ranges = pd.DataFrame()
        self._incomplete_years = {}


    @property
    def metadata(self) -> Metadata | None:
        """Metadata for the loaded data (site, query, variables)."""
        return self._metadata


    def load_from_xml(self, xml_bytes: bytes) -> Self:
        """
        Parse WaterML 1.1 XML data and load into a pandas DataFrame. The XML is 
        expected to contain time series data for a single site, with one or 
        more variables (e.g. discharge, temperature) and statistics (e.g. mean, 
        max, min).
        
        :param bytes xml_bytes: The raw XML data as bytes, typically obtained 
            from the USGS web service.
        ________
        :return: self (for method chaining)
        :rtype: RiverData 
        """

        ns = XML_NAMESPACES
        root = ET.fromstring(xml_bytes)

        self._metadata = USGSMetadata.from_xml(root)
        self.gage_name = self.metadata.gage_name

        rows = []
        variable_names = {}
        for ts in root.findall(".//ns1:timeSeries", ns):
            
            code = ts.findtext(".//ns1:variableCode", namespaces=ns)
            variable_name = ts.findtext(".//ns1:variableName", namespaces=ns)
            statistic = ts.findtext(".//ns1:option[@name='Statistic']", namespaces=ns)
            nodata_value = ts.findtext(".//ns1:noDataValue", namespaces=ns)

            variable_names[code] = variable_name

            for v in ts.findall(".//ns1:value", ns):

                value = v.text
                date = v.attrib.get("dateTime")

                rows.append({
                    "date": date,
                    "variable": code,  # use code for clean column names
                    'statistic': statistic,
                    "value": value,
                    "nodata_value": nodata_value,
                })

        
        if len(rows) == 0:
            raise ValueError(
                f'No timeseries data found for gage ID {self.gage_id}'
            )
        
        df = pd.DataFrame(rows)

        # --- Clean types ---
        df["date"] = pd.to_datetime(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")

        # Replace nodata with NaN
        df["nodata_value"] = pd.to_numeric(df["nodata_value"], errors="coerce")
        df.loc[df["value"] == df["nodata_value"], "value"] = pd.NA

        # --- Pivot to wide format ---
        df_wide = (
            df.pivot(
                index="date", 
                columns=["variable", "statistic"], 
                values="value"
            )
            .rename(columns=self.VARIABLE_MAP, level=0)
            .rename(columns=self.STAT_MAP, level=1)
        )
        df_wide.columns = ['_'.join(a) for a in df_wide.columns.to_flat_index()]
        
        # Discharge will always be a daily mean
        df_wide.rename(columns={'discharge_cfs_mean': 'discharge_cfs'}, inplace=True)

        # Sort chronologically
        df_wide = df_wide.sort_index()

        self.data = df_wide

        return self
    

    def load_noaa(self, data_dir:str | PathLike) -> Self:
        """
        Load NOAA data from a directory of text files processed with ingest_noaa.py.
        """

        data_dir = Path(data_dir)
        if not data_dir.exists():
            raise ValueError(f"Data directory {data_dir} does not exist.")

        # Find the file. There should only be one per gage_id
        gage_file = data_dir / f'{self.gage_id}.csv'
        if not gage_file.exists():
            raise ValueError(f"No NOAA data file found for gage {self.gage_id} in {data_dir}")
        
        # Get gage name from metadata
        self._metadata = ensure_noaa_metadata(self.gage_id, data_dir)
        self.gage_name = self.metadata.gage_name

        df = (
            pd.read_csv(gage_file, parse_dates=["Obstime"])
                .sort_values("Obstime")
                .pivot(index="Obstime", columns="PE", values="Value")
                .rename(columns=self.VARIABLE_MAP)
        )

        self.data = df
        
        return self


    def _get_variable_columns(self) -> list:
        return [
            col for col in self.data.columns 
            if col not in self.NON_VARIABLE_COLUMNS
        ]
    

    def get_water_year(self) -> pd.DataFrame:
        """
        Add water year and water day of year columns to the data frame. Water 
        year starts on October 1st, so for example, October 1, 2020 would be 
        water year 2021, and its water day of year would be 1. September 30, 
        2021 would also be water year 2021, but its water day of year would be 
        365 (or 366 in a leap year).

        :returns: self (for method chaining)
        :rtype: RiverData
        """
        df = self.data
        df['year'] = df.index.year
        df["water_year"] = df.year + (df.index.month >= 10)

        wy_start = pd.to_datetime(
            (df["water_year"] - 1).astype(str) + "-10-01"
        )

        df["water_doy"] = (df.index - wy_start).dt.days + 1

        df['day_of_year'] = df.index.dayofyear

        # Convert all columns to nullable Int64
        for col in self.NON_VARIABLE_COLUMNS:
            df[col] = df[col].astype(INT_DTYPE)

        return self


    def _set_data_ranges(self) -> None:
        """
        Calculate the start and end dates of the record based on the data index. 
        Sets self._record_ranges

        :returns: None (sets self._record_ranges attribute)
        :rtype: None
        """
        if self.data is None or self.data.empty:
            raise ValueError("No data loaded to determine record range.")

        
        ranges = {}
        for col in self._get_variable_columns():
            if self.data[col].notna().any():
                ranges[col] = {
                    'start': self.data[col].first_valid_index().date(),
                    'end': self.data[col].last_valid_index().date(),
                }
        
        self._record_ranges = pd.DataFrame(ranges)
        

    def data_ranges(self) -> pd.DataFrame:
        """
        Get the start and end dates of the record for each variable/statistic 
        column in the data. This is calculated based on the first and last 
        valid (non-NA) value for each column.

        :returns: DataFrame with columns for each variable/statistic combination, 
            and rows 'start' and 'end' indicating the date range of valid data.
        :rtype: pandas.DataFrame
        """
        if self._record_ranges.empty:
            self._set_data_ranges()

        return self._record_ranges


    def _set_incomplete_years(self) -> None:
        """
        Identify water years with incomplete data. A water year is considered
        incomplete for a given statistic if it does not have valid data for all days (1-365/366).

        :returns: None (sets self._incomplete_years attribute)
        :rtype: None
        """
        df = self.data

        if 'water_year' not in df.columns:
            self.get_water_year()

        if df is None or df.empty:
            raise ValueError("No data loaded to determine record range.")

        # Define water year start and end per group
        def is_complete(group):
            wy = group.name
            start = pd.Timestamp(wy - 1, 10, 1)
            end   = pd.Timestamp(wy, 9, 30)

            expected = pd.date_range(start, end, freq='D')
            actual = pd.DatetimeIndex(group.index)

            return len(actual) == len(expected) and actual.isin(expected).all()
        
        for col in self._get_variable_columns():
            data = df.loc[:, [col, 'water_year']].dropna(subset=[col])

            self._incomplete_years[col] = []
            completeness = data.groupby('water_year').apply(is_complete)
            self._incomplete_years[col] = (
                completeness[~completeness].index.tolist()
            )


    def incomplete_years(self) -> Dict[str, List[int]]:
        """
        Get a dictionary of incomplete water years for each variable/statistic 
        column. A water year is considered incomplete for a given statistic if 
        it does not have valid data for all days (1-365/366).

        :returns: Dictionary where keys are variable/statistic column names and 
            values are lists of water years that are incomplete for that column.
        :rtype: Dict[str, List[int]]
        """
        if not self._incomplete_years:
            self._set_incomplete_years()

        return self._incomplete_years
    

    def incomplete_months(self, variable: str) -> pd.Index:
        """
        Returns a MultiIndex of (water_year, month) pairs that are incomplete
        for the given variable.

        :param str variable: The variable/statistic column to check for completeness
        :returns: MultiIndex of (water_year, month) pairs that are incomplete for the given variable
        :rtype: pandas.MultiIndex
        """

        if variable not in self.data.columns:
            raise ValueError(f"{variable} not found in dataset.")

        df = self.data.copy()

        if "water_year" not in df.columns:
            self.get_water_year()

        df["month"] = df.index.month

        incomplete = []

        for (wy, month), group in df.groupby(["water_year", "month"]):

            # Expected number of days in this month
            start = group.index.min().replace(day=1)
            end = (start + pd.offsets.MonthEnd(1))

            expected_days = (end - start).days + 1

            actual_days = group[variable].count()

            if actual_days != expected_days:
                incomplete.append((wy, month))

        return pd.MultiIndex.from_tuples(
            incomplete, names=["water_year", "month"]
        )


    def save_xml(self, xml_bytes: bytes) -> Path:
        """
        Save the raw XML

        :param bytes xml_bytes: The raw XML data as bytes, typically obtained 
            from the USGS web service
        
        :returns: The path to the saved XML file
        :rtype: pathlib.Path

        """
        self.directory = self.file_manager.ensure_directory(self.gage_id)

        filename = self.directory / f'{self.gage_id}_data.xml'
        filename = self.file_manager.unique_filename(filename)

        filename.write_bytes(xml_bytes)

        return filename
    

    def to_csv(self, save_metadata: bool = True) -> Path:
        """
        Save data as a CSV

        :returns: The path to the saved CSV file
        :rtype: pathlib.Path
        """
        if self.data is None:
            raise ValueError("No data loaded to save.")

        self.directory = self.file_manager.ensure_directory(
            self.gage_name or self.gage_id
        )

        csv_path = self.directory / f'{self.gage_id}_data.csv'
        csv_path = self.file_manager.unique_filename(csv_path)

        self.data.to_csv(csv_path)

        if save_metadata:
            if self.metadata is None:
                warnings.warn('Metadata not saved because it was never loaded')
            
            self.metadata.save(csv_path.parent)
            
        return csv_path



# ==========================================================
# Hydrology Analysis
# ==========================================================

class HydroAnalyzer:

    SECONDS_PER_DAY = 86400

    def __init__(self, river_data: RiverData):
        self.river = river_data


    def check_variable(self, variable: str='discharge_cfs') -> None:
        """
        Helper method to check that discharge data is available and valid before performing analyses that require it. Raises ValueError if checks fail.
        """
        if self.river.data is None or self.river.data.empty:
            raise ValueError("No data loaded.")

        if variable not in self.river.data.columns:
            raise ValueError(f"Column '{variable}' not found in dataset.")
        
        if self.river.data[variable].isna().all():
            raise ValueError(f"All values in '{variable}' column are NaN.")
        
        if not 'water_year' in self.river.data.columns:
            self.river.get_water_year()
        

    def ensure_annual_result_range(
            self, 
            data: pd.DataFrame
        ) -> pd.DataFrame:
        """
        Helper method to make sure that every year exists in the DatFrame 
        result of an annual calculation. Any missing years will be filled with 
        NA. This is necessary because it's possible that an entitre year could 
        be missing from the data, and we want all years represented in text and 
        graph outputs.

        :param pandas.DataFrame data: The DataFrame containing the results of an annual calculation, indexed by year
        
        :returns: result DataFrame for method chaining
        :rtype: pandas.DataFrame
        """
        start_year = data.index.min()
        end_year = data.index.max()

        return data.reindex(range(start_year, end_year + 1), fill_value=pd.NA)


    def total_discharge(self, include_incomplete: bool=False) -> pd.Series:
        """
        Calculate total annual discharge in cubic feet for each water year.
        
        :param bool include_incomplete: If True, water years with incomplete 
            data will still be included. If False, incomplete years will be set
            to NaN.
        
        :returns: Series of total annual discharge values indexed by water year
        :rtype: pandas.Series
        """

        self.check_variable()

        # Resampler.mean() converts all numeric columns to float64, so record 
        #   the original dtypes to restore later
        dtypes = self.river.data.dtypes
        integer_cols = dtypes.loc[
            dtypes.astype(str).str.lower().str.startswith('int')
        ].index

        # Reduce data to one row per day by taking mean / day
        daily_mean = self.river.data.resample('D').mean()

        # Restore dtypes
        for col in integer_cols:
            if col in daily_mean.columns:
                daily_mean[col] = daily_mean[col].astype(INT_DTYPE)

        # Discharge is an average of cubic feet per second, so to get total 
        #  volume in cubic feet, sum over the year by the number of seconds in
        #  a day 
        total = (
            daily_mean.groupby('water_year').discharge_cfs.sum() * 
            self.SECONDS_PER_DAY
        )

        # If the user wants to exclude years with incomplete data, set the 
        #   incomplete years to NA
        if not include_incomplete:
            total.loc[self.river.incomplete_years()['discharge_cfs']] = pd.NA

        return self.ensure_annual_result_range(total)


    def mean_discharge(self, include_incomplete: bool=False) -> pd.Series:
        """
        Calculate mean annual discharge in cubic feet per second for each water 
        year. 
        
        :param bool include_incomplete: If True, water years with incomplete 
            data will still be included. If False, incomplete years will be set
            to NaN.
        
        :returns: Series of mean annual discharge values indexed by water year
        :rtype: pandas.Series
        """
        total = self.total_discharge(include_incomplete=include_incomplete)
        
        days = self.river.data.groupby('water_year').discharge_cfs.count()
        mean_q = (total / (days * self.SECONDS_PER_DAY)).round(2)

        return self.ensure_annual_result_range(mean_q)
    

    def max(
            self, 
            variable: str, 
            include_incomplete: bool=False, 
            after_water_doy: int=1
        ) -> pd.DataFrame:
        """
        Returns the maximum value for the specified variable for each water year

        :param str variable: The variable for which to find the maximum
        :param bool include_incomplete: If True, water years with incomplete 
            data will still be included. If False, incomplete years will be set 
            to NaN.
        :param int after_water_doy: Only consider values on or after 
            this water day of year (e.g. to exclude early season peaks) 
            Defaults to 1
        :returns: DataFrame with columns 'max_discharge_cfs' and 
            'max_discharge_date' indexed by water year
        :rtype: pandas.DataFrame
        """

        self.check_variable(variable)

        # Only consider values on or after the specified water day of year
        df = self.river.data.loc[self.river.data.water_doy >= after_water_doy]

        max_values = df.groupby('water_year').max()[variable]
        max_date = df.groupby('water_year').idxmax()[variable]
        result = pd.DataFrame({
            f'date_of_max_{variable}': max_date, 
            f'max_{variable}': max_values
        })
        
        # If the user doesn't want to include incomplete years, set the 
        #   incomplete years to NA
        if not include_incomplete:
            incomplete_years = [
                y for y in
                self.river.incomplete_years().get(variable, [])
                if y in result.index
            ]
            result.loc[incomplete_years] = pd.NA

        return self.ensure_annual_result_range(
            result
        )


    def monthly_stat(
        self,
        variable: str,
        stat: str,
        include_incomplete: bool = False,
        pivot: bool = True
    ) -> pd.DataFrame:
        """
        Calculate monthly statistics for a given variable.

        :param str variable: Column name (e.g. 'discharge_cfs', 'temp_mean')
        :param str stat: 'mean', 'max', 'min', 'median', or 'all'
        :param bool include_incomplete: If False, incomplete months are set 
            to NA
        :param bool pivot: If True, returns water_year x month matrix.
                    If False, returns long format.
        :returns: DataFrame of monthly statistics
        :rtype: pandas.DataFrame
        """

        self.check_variable(variable)

        valid_vars = {
            "discharge_cfs",
            "temp_c_mean",
            "temp_c_min",
            "temp_c_max",
            "stage_ft"
        }

        valid_stats = {"mean", "min", "max", "median", "all"}

        if variable not in valid_vars:
            raise ValueError(
                f"Invalid variable '{variable}'. "
                f"Valid options: {valid_vars}"
            )

        if stat not in valid_stats:
            raise ValueError(
                f"Invalid stat '{stat}'. "
                f"Valid options: {valid_stats}"
            )

        df = self.river.data.copy()

        df["month"] = df.index.month

        grouped = df.groupby(["water_year", "month"])[variable]

        if stat == "mean":
            result = grouped.mean()
        elif stat == "max":
            result = grouped.max()
        elif stat == "min":
            result = grouped.min()
        elif stat == "median":
            result = grouped.median()
        elif stat == "all":
            result = grouped.agg(["mean", "max", "min", "median"])

        # --- Handle incomplete months ---
        if not include_incomplete:
            incomplete = self.river.incomplete_months(variable)
            result.loc[incomplete] = pd.NA

        result = result.rename(f"{variable}_{stat}")

        if stat == "all" or not pivot:
            return result.reset_index()

        # Pivot to water_year x month matrix
        result = result.unstack("month")

        # Order months Oct–Sep (water year order)
        result = result.reindex(columns=self.river.WATER_YEAR_MONTHS)

        # Optional: rename to month abbreviations
        month_names = {
            1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
            7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"
        }
        result.rename(columns=month_names, inplace=True)

        return result


    def spring_pulse(
            self, 
            variable: Optional[str]='discharge_cfs'
        ) -> pd.DataFrame:
        """
        Calculate spring pulse onset date for each water year using
        Stewart et al. (2005) cumulative departure method.

        :returns: DataFrame with columns 'spring_pulse_doy' and 
            'spring_pulse_date' indexed by water year
        :rtype: pandas.DataFrame
        """

        self.check_variable(variable)

        required_cols = {"water_year", "water_doy"}
        if not required_cols.issubset(self.river.data.columns):
            raise ValueError(
                "water_year and water_doy must be computed before calling spring_pulse()."
            )

        results = []

        for wy, group in self.river.data.groupby("water_year"):

            # Restrict to day of calendar year 9–248
            subset = group[
                (group["day_of_year"] >= 9) &
                (group["day_of_year"] <= 248)
            ].copy()

            # Skip incomplete years
            if subset.empty or subset[variable].isna().any():
                results.append({
                    "water_year": wy,
                    "spring_pulse_doy": pd.NA,
                    "spring_pulse_date": pd.NaT
                })
                continue

            # Mean discharge over window
            mean_q = subset[variable].mean()

            # Daily departures
            subset["departure"] = subset[variable] - mean_q

            # Cumulative departure
            subset["cum_departure"] = subset["departure"].cumsum()

            # Find minimum cumulative departure
            min_idx = subset["cum_departure"].idxmin()

            results.append({
                "water_year": wy,
                "spring_pulse_doy": subset.loc[min_idx, "water_doy"],
                "spring_pulse_date": min_idx,
            })

        result_df = self.ensure_annual_result_range(
            pd.DataFrame(results).set_index("water_year")
        )

        return result_df
    

    def center_of_mass(self):
        """
        Returns the date of the center of mass of annual flow
        for the given water year.

        Water year runs Oct 1 (previous year) through Sept 30.

        If data are incomplete or discharge_cfs column is missing,
        returns None.

        :returns: Series of center of mass dates indexed by water year
        :rtype: pandas.Series
        """

        self.check_variable()

        df = self.river.data

        centers_of_mass = {}
        for water_year, df in self.river.data.groupby("water_year"):

            # --- Check full year length (365 or 366 days) ---
            start = df.index.min()
            end = df.index.max()
            expected_days = (end - start).days + 1
            if len(df) != expected_days:
                centers_of_mass[water_year] =  pd.NA
                continue

            if df.discharge_cfs.isna().any():
                centers_of_mass[water_year] =  pd.NA
                continue

            # --- Center of mass calculation ---
            numerator = (df.water_doy * df.discharge_cfs).sum()
            denominator = df.discharge_cfs.sum()

            if denominator == 0:
                centers_of_mass[water_year] =  pd.NA
                continue

            center_day = int(round(numerator / denominator))

            # Convert back to calendar date
            center_date = start + pd.Timedelta(days=center_day - 1)

            centers_of_mass[water_year] = center_date

        return self.ensure_annual_result_range(
            pd.DataFrame({'center_of_mass': centers_of_mass})
        )
    
    
    
    def fractional_monthly_flow(
            self, 
            include_incomplete=False, 
            use_calendar_year=False
        ) -> pd.DataFrame:
        """
        Calculate the fractional monthly flow for each month in each water year. Fractional monthly flow is the total flow for a given month divided by the total flow for the entire water year, expressed as a percentage.

        :param bool include_incomplete: If True, water years with incomplete 
            data will still be included. If False, incomplete years will be set 
            to NaN.
        
        :returns: DataFrame with columns for each month (e.g. 'Jan', 'Feb', etc.) and rows indexed by water year, containing the fractional monthly flow percentages.
        :rtype: pandas.DataFrame
        """
        #TODO: check if using the calendar year is desirable or if water year should always be used

        self.check_variable()

        df = self.river.data.copy()
        df["month"] = df.index.month # numeric month
        

        # Calculate total annual flow
        annual_flow = self.total_discharge(include_incomplete=include_incomplete)

        # Calculate monthly flow
        monthly_flow = df.groupby(["water_year", "month"]).discharge_cfs.sum() * self.SECONDS_PER_DAY

        # Calculate fractional monthly flow
        fractional_flow = (
            (
                (monthly_flow / annual_flow)
                    .unstack(level="month") * 100
            )
            # Order columns by calendar year or water year
            .loc[:, range(1, 13) if use_calendar_year 
                 else self.river.WATER_YEAR_MONTHS
            ] 
        )

        # Get month: month name series to rename columns. The month is 
        #   initially calculated numerically to keep the months in calendar 
        #   order because if we use month names from the start, they will be 
        #   sorted alphabetically. 
        df["month_name"] = df.index.strftime('%b')
        month_names = (
            df.drop_duplicates(subset=['month_name', 'month'])
                .loc[:, ['month', 'month_name']]
                .set_index('month')['month_name']
        )
        fractional_flow.rename(columns=month_names, inplace=True)

        return self.ensure_annual_result_range(fractional_flow)
