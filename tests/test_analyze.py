'''Tests for analyze.py (RiverData and HydroAnalyzer).'''
import datetime as dt
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import pytest

import analyze
from analyze import HydroAnalyzer, RiverData
from metadata import Metadata, NOAAMetadata, USGSMetadata

SECONDS = 86400
MONTHS_WY = ['Oct', 'Nov', 'Dec', 'Jan', 'Feb', 'Mar',
             'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep']
MONTHS_CAL = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def n_days(start, end):
    return len(pd.date_range(start, end, freq='D'))


# ======================================================================
# RiverData: loading
# ======================================================================
class TestLoadFromXml:
    def test_single_variable(self, waterml):
        xml = waterml.build([waterml.series([100, 200, 300])])
        river = RiverData('15356000').load_from_xml(xml)

        assert list(river.data.columns) == ['discharge_cfs']
        assert river.data['discharge_cfs'].tolist() == [100.0, 200.0, 300.0]
        assert river.data.index[0] == pd.Timestamp('2020-10-01')
        assert river.data.index.is_monotonic_increasing
        assert river.gage_name == 'TEST RIVER NEAR PLACE'
        assert isinstance(river.metadata, USGSMetadata)

    def test_returns_self_for_chaining(self, waterml):
        xml = waterml.build([waterml.series([1, 2])])
        river = RiverData('15356000')
        assert river.load_from_xml(xml) is river

    def test_nodata_value_becomes_nan(self, waterml):
        xml = waterml.build([waterml.series([100, -999999.0, 300])])
        river = RiverData('15356000').load_from_xml(xml)
        assert pd.isna(river.data['discharge_cfs'].iloc[1])
        assert river.data['discharge_cfs'].iloc[[0, 2]].tolist() == [100.0, 300.0]

    def test_multiple_variables_and_statistics(self, waterml):
        xml = waterml.build([
            waterml.series([100, 200, 300], code='00060', stat='Mean'),
            waterml.series([5.0, 6.0], code='00010', stat='Maximum'),
            waterml.series([1.0, 2.0], code='00010', stat='Minimum'),
        ])
        river = RiverData('15356000').load_from_xml(xml)

        assert set(river.data.columns) == {'discharge_cfs', 'temp_c_max', 'temp_c_min'}
        assert len(river.data) == 3
        # Temperature series is shorter, so the last day is NaN
        assert pd.isna(river.data['temp_c_max'].iloc[2])

    def test_series_without_values_raises(self, waterml):
        xml = waterml.build([waterml.series([])])
        with pytest.raises(ValueError, match='No timeseries data found'):
            RiverData('15356000').load_from_xml(xml)

    def test_malformed_xml_raises_parse_error(self):
        with pytest.raises(ET.ParseError):
            RiverData('15356000').load_from_xml(b'this is not xml')

    def test_response_with_no_time_series_raises_value_error(self, waterml):
        xml = waterml.build([], include_site=False)
        with pytest.raises(ValueError, match='No timeseries data found'):
            RiverData('00000000').load_from_xml(xml)


class TestLoadNoaa:
    def test_loads_and_renames_columns(self, tmp_path, write_noaa_csv):
        write_noaa_csv(tmp_path, 'RLYA2', values=[5.0, 5.5, 6.0])
        NOAAMetadata(gage_id='RLYA2', gage_name='Test River').save(tmp_path)

        river = RiverData('RLYA2').load_noaa(tmp_path)

        assert list(river.data.columns) == ['stage_ft']
        assert river.data['stage_ft'].tolist() == [5.0, 5.5, 6.0]
        assert river.gage_name == 'Test River'

    def test_returns_self(self, tmp_path, write_noaa_csv):
        write_noaa_csv(tmp_path, 'RLYA2')
        NOAAMetadata(gage_id='RLYA2', gage_name='X').save(tmp_path)
        river = RiverData('RLYA2')
        assert river.load_noaa(tmp_path) is river

    def test_sorts_by_observation_time(self, tmp_path):
        pd.DataFrame({
            'Obstime': ['2020-10-03', '2020-10-01', '2020-10-02'],
            'PE': 'HG',
            'Value': [3.0, 1.0, 2.0],
        }).to_csv(tmp_path / 'RLYA2.csv', index=False)
        NOAAMetadata(gage_id='RLYA2', gage_name='X').save(tmp_path)

        river = RiverData('RLYA2').load_noaa(tmp_path)
        assert river.data['stage_ft'].tolist() == [1.0, 2.0, 3.0]

    def test_missing_directory_raises(self, tmp_path):
        with pytest.raises(ValueError, match='does not exist'):
            RiverData('RLYA2').load_noaa(tmp_path / 'nope')

    def test_missing_gage_file_raises(self, tmp_path):
        with pytest.raises(ValueError, match='No NOAA data file'):
            RiverData('RLYA2').load_noaa(tmp_path)

    def test_warns_when_metadata_unavailable(self, tmp_path, write_noaa_csv, monkeypatch):
        write_noaa_csv(tmp_path, 'RLYA2')
        monkeypatch.setattr(analyze, 'ensure_noaa_metadata', lambda *a, **k: None)

        river = RiverData('RLYA2')
        with pytest.warns(UserWarning, match='No metadata found'):
            river.load_noaa(tmp_path)

        assert river.gage_name is None
        assert river.metadata.gage_id == 'RLYA2'


# ======================================================================
# RiverData: water-year helpers
# ======================================================================
class TestGetWaterYear:
    def test_water_year_boundary(self, river_factory):
        # Sep 29, Sep 30, Oct 1, Oct 2 of 2020 (WY2020 is a leap water year)
        river = river_factory(values=[1.0, 2.0, 3.0, 4.0], start='2020-09-29')
        river.get_water_year()

        assert river.data['water_year'].tolist() == [2020, 2020, 2021, 2021]
        assert river.data['water_doy'].tolist() == [365, 366, 1, 2]

    def test_calendar_year_rollover(self, river_factory):
        river = river_factory(values=[1.0, 2.0], start='2020-12-31')
        river.get_water_year()

        assert river.data['year'].tolist() == [2020, 2021]
        assert river.data['water_year'].tolist() == [2021, 2021]
        assert river.data['water_doy'].tolist() == [92, 93]

    def test_leap_day(self, river_factory):
        river = river_factory(values=[1.0], start='2020-02-29')
        river.get_water_year()

        assert river.data['water_year'].iloc[0] == 2020
        assert river.data['day_of_year'].iloc[0] == 60
        assert river.data['water_doy'].iloc[0] == 152

    def test_columns_are_nullable_int(self, river_factory):
        river = river_factory(values=[1.0, 2.0])
        river.get_water_year()
        for col in RiverData.NON_VARIABLE_COLUMNS:
            assert str(river.data[col].dtype) == 'Int64'

    def test_returns_self(self, river_factory):
        river = river_factory(values=[1.0])
        assert river.get_water_year() is river


class TestDataRanges:
    def test_first_and_last_valid_dates(self, river_factory):
        river = river_factory(values=[np.nan, 1.0, 2.0, np.nan])
        ranges = river.data_ranges()

        assert ranges.loc['start', 'discharge_cfs'] == dt.date(2020, 10, 2)
        assert ranges.loc['end', 'discharge_cfs'] == dt.date(2020, 10, 3)

    def test_excludes_helper_columns_and_all_nan_columns(self, river_factory):
        river = river_factory(values=[1.0, 2.0, 3.0])
        river.data['stage_ft'] = np.nan
        river.get_water_year()

        assert list(river.data_ranges().columns) == ['discharge_cfs']

    def test_empty_data_raises(self):
        with pytest.raises(ValueError, match='No data loaded'):
            RiverData('X').data_ranges()

    def test_result_is_cached(self, river_factory):
        river = river_factory(values=[1.0, 2.0])
        assert river.data_ranges() is river.data_ranges()


class TestIncompleteYears:
    def test_complete_years(self, river_factory):
        river = river_factory(start='2020-10-01', end='2022-09-30')
        assert river.incomplete_years()['discharge_cfs'] == []

    def test_partial_first_year(self, river_factory):
        river = river_factory(start='2020-12-01', end='2022-09-30')
        assert river.incomplete_years()['discharge_cfs'] == [2021]

    def test_missing_day_makes_year_incomplete(self, river_factory):
        river = river_factory(start='2020-10-01', end='2022-09-30')
        river.data = river.data.drop(pd.Timestamp('2021-03-15'))
        assert river.incomplete_years()['discharge_cfs'] == [2021]

    def test_nan_day_makes_year_incomplete(self, river_factory):
        river = river_factory(start='2020-10-01', end='2022-09-30')
        river.data.loc['2021-03-15', 'discharge_cfs'] = np.nan
        assert river.incomplete_years()['discharge_cfs'] == [2021]

    def test_leap_year_is_complete_with_366_days(self, river_factory):
        river = river_factory(start='2019-10-01', end='2020-09-30')
        assert river.incomplete_years()['discharge_cfs'] == []

    def test_empty_data_raises(self):
        with pytest.raises(ValueError, match='No data loaded'):
            RiverData('X').incomplete_years()


class TestIncompleteMonths:
    def test_all_complete(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30')
        river.get_water_year()
        assert len(river.incomplete_months('discharge_cfs')) == 0

    def test_nan_day_flags_that_month(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30')
        river.get_water_year()
        river.data.loc['2021-01-15', 'discharge_cfs'] = np.nan

        result = river.incomplete_months('discharge_cfs')

        assert list(result) == [(2021, 1)]
        assert list(result.names) == ['water_year', 'month']

    def test_missing_row_flags_that_month(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30')
        river.get_water_year()
        river.data = river.data.drop(pd.Timestamp('2021-02-10'))

        assert list(river.incomplete_months('discharge_cfs')) == [(2021, 2)]

    def test_unknown_variable_raises(self, river_factory):
        river = river_factory(values=[1.0])
        with pytest.raises(ValueError, match='not found'):
            river.incomplete_months('nope')

    def test_works_before_water_year_is_computed(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30')
        assert len(river.incomplete_months('discharge_cfs')) == 0


class TestMissingDays:
    def test_absent_rows_are_counted(self, river_factory):
        river = river_factory(start='2020-10-01', end='2022-09-30')
        river.data = river.data.drop(pd.date_range('2021-03-10', '2021-03-12'))

        result = river.missing_days('discharge_cfs')

        assert result.index.name == 'water_year'
        assert result.loc[2021, 'expected_days'] == 365
        assert result.loc[2021, 'valid_days'] == 362
        assert result.loc[2021, 'missing_days'] == 3
        assert result.loc[2022, 'missing_days'] == 0

    def test_nan_values_are_counted(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30')
        river.data.loc['2021-03-10':'2021-03-11', 'discharge_cfs'] = np.nan
        assert river.missing_days('discharge_cfs').loc[2021, 'missing_days'] == 2

    def test_within_record_ignores_partial_edge_years(self, river_factory):
        river = river_factory(start='2021-01-01', end='2021-09-30')
        result = river.missing_days('discharge_cfs', within_record=True)
        assert result.loc[2021, 'expected_days'] == 273
        assert result.loc[2021, 'missing_days'] == 0

    def test_whole_water_year_when_not_within_record(self, river_factory):
        river = river_factory(start='2021-01-01', end='2021-09-30')
        result = river.missing_days('discharge_cfs', within_record=False)
        assert result.loc[2021, 'expected_days'] == 365
        assert result.loc[2021, 'valid_days'] == 273
        assert result.loc[2021, 'missing_days'] == 92

    def test_multiple_readings_per_day_count_once(self):
        river = RiverData('X')
        idx = pd.DatetimeIndex([
            '2020-10-01 00:00', '2020-10-01 12:00',
            '2020-10-02 00:00', '2020-10-03 00:00',
        ])
        river.data = pd.DataFrame({'discharge_cfs': [1.0, 2.0, 3.0, 4.0]}, index=idx)

        result = river.missing_days('discharge_cfs')
        assert result.loc[2021, 'valid_days'] == 3
        assert result.loc[2021, 'missing_days'] == 0

    def test_unknown_variable_raises(self, river_factory):
        river = river_factory(values=[1.0])
        with pytest.raises(ValueError, match='not found'):
            river.missing_days('nope')

    def test_all_nan_raises(self, river_factory):
        river = river_factory(values=[np.nan, np.nan])
        with pytest.raises(ValueError, match='NaN'):
            river.missing_days('discharge_cfs')


# ======================================================================
# RiverData: saving
# ======================================================================
class TestSaveXml:
    def test_writes_bytes_under_gage_directory(self, tmp_path):
        river = RiverData('TEST')
        path = river.save_xml(b'<a/>', tmp_path)

        assert path.parent == tmp_path / 'TEST'
        assert path.name.startswith('TEST_data')
        assert path.suffix == '.xml'
        assert path.read_bytes() == b'<a/>'

    def test_overwrite_true_replaces_file(self, tmp_path):
        river = RiverData('TEST')
        first = river.save_xml(b'one', tmp_path, overwrite=True)
        second = river.save_xml(b'two', tmp_path, overwrite=True)

        assert first == second
        assert second.read_bytes() == b'two'

    def test_overwrite_false_keeps_existing_file(self, tmp_path):
        river = RiverData('TEST')
        first = river.save_xml(b'one', tmp_path)
        second = river.save_xml(b'two', tmp_path)

        assert first != second
        assert first.read_bytes() == b'one'
        assert second.read_bytes() == b'two'


class TestToCsv:
    def test_writes_csv_and_metadata(self, tmp_path, river_factory):
        river = river_factory(values=[1.0, 2.0, 3.0])
        river._metadata = Metadata(gage_id='TEST', gage_name='N')

        path = river.to_csv(tmp_path)

        assert path.parent == tmp_path / 'TEST'
        assert path.suffix == '.csv'
        assert (tmp_path / 'TEST' / 'TEST_meta.json').exists()
        back = pd.read_csv(path, index_col=0, parse_dates=True)
        assert back['discharge_cfs'].tolist() == [1.0, 2.0, 3.0]

    def test_can_skip_metadata(self, tmp_path, river_factory):
        river = river_factory(values=[1.0])
        river._metadata = Metadata(gage_id='TEST')
        river.to_csv(tmp_path, save_metadata=False)
        assert not (tmp_path / 'TEST' / 'TEST_meta.json').exists()

    def test_warns_when_metadata_never_loaded(self, tmp_path, river_factory):
        river = river_factory(values=[1.0])
        with pytest.warns(UserWarning, match='Metadata not saved'):
            river.to_csv(tmp_path)

    def test_overwrite_true_replaces_file(self, tmp_path, river_factory):
        river = river_factory(values=[1.0])
        river._metadata = Metadata(gage_id='TEST')
        first = river.to_csv(tmp_path, overwrite=True)
        second = river.to_csv(tmp_path, overwrite=True)
        assert first == second


# ======================================================================
# HydroAnalyzer
# ======================================================================
class TestCheckVariable:
    def test_empty_data(self):
        with pytest.raises(ValueError, match='No data loaded'):
            HydroAnalyzer(RiverData('X')).check_variable()

    def test_unknown_column(self, river_factory):
        h = HydroAnalyzer(river_factory(values=[1.0]))
        with pytest.raises(ValueError, match='not found'):
            h.check_variable('nope')

    def test_all_nan(self, river_factory):
        h = HydroAnalyzer(river_factory(values=[np.nan, np.nan]))
        with pytest.raises(ValueError, match='NaN'):
            h.check_variable()

    def test_adds_water_year_columns(self, river_factory):
        river = river_factory(values=[1.0, 2.0])
        HydroAnalyzer(river).check_variable()
        assert 'water_year' in river.data.columns


class TestEnsureAnnualResultRange:
    def test_fills_missing_years_in_series(self, river_factory):
        h = HydroAnalyzer(river_factory(values=[1.0]))
        result = h.ensure_annual_result_range(pd.Series([1.0, 3.0], index=[2020, 2022]))

        assert list(result.index) == [2020, 2021, 2022]
        assert pd.isna(result.loc[2021])

    def test_fills_missing_years_in_dataframe(self, river_factory):
        h = HydroAnalyzer(river_factory(values=[1.0]))
        df = pd.DataFrame({'a': [1.0, 3.0]}, index=[2020, 2023])
        result = h.ensure_annual_result_range(df)

        assert list(result.index) == [2020, 2021, 2022, 2023]
        assert result.loc[[2021, 2022], 'a'].isna().all()


class TestTotalAndMeanDischarge:
    def test_total_for_complete_years(self, river_factory):
        river = river_factory(start='2020-10-01', end='2022-09-30', value=100.0)
        total = HydroAnalyzer(river).total_discharge()

        assert list(total.index) == [2021, 2022]
        assert total.loc[2021] == pytest.approx(100.0 * SECONDS * 365)
        assert total.loc[2022] == pytest.approx(100.0 * SECONDS * 365)

    def test_total_in_leap_water_year(self, river_factory):
        river = river_factory(start='2019-10-01', end='2020-09-30', value=100.0)
        total = HydroAnalyzer(river).total_discharge()
        assert total.loc[2020] == pytest.approx(100.0 * SECONDS * 366)

    def test_incomplete_year_is_na_by_default(self, river_factory):
        river = river_factory(start='2020-12-01', end='2022-09-30', value=100.0)
        total = HydroAnalyzer(river).total_discharge()

        assert pd.isna(total.loc[2021])
        assert total.loc[2022] == pytest.approx(100.0 * SECONDS * 365)

    def test_incomplete_year_included_on_request(self, river_factory):
        river = river_factory(start='2020-12-01', end='2022-09-30', value=100.0)
        total = HydroAnalyzer(river).total_discharge(include_incomplete=True)
        days = n_days('2020-12-01', '2021-09-30')
        assert total.loc[2021] == pytest.approx(100.0 * SECONDS * days)

    def test_mean_of_constant_flow(self, river_factory):
        river = river_factory(start='2020-10-01', end='2022-09-30', value=100.0)
        mean = HydroAnalyzer(river).mean_discharge()
        assert mean.loc[2021] == pytest.approx(100.0)
        assert mean.loc[2022] == pytest.approx(100.0)

    def test_mean_of_varying_flow(self, river_factory):
        river = river_factory(values=[float(v) for v in range(1, 366)])
        mean = HydroAnalyzer(river).mean_discharge()
        assert mean.loc[2021] == pytest.approx(183.0)

    def test_mean_incomplete_year_handling(self, river_factory):
        river = river_factory(start='2020-12-01', end='2022-09-30', value=100.0)
        h = HydroAnalyzer(river)

        assert pd.isna(h.mean_discharge().loc[2021])
        assert h.mean_discharge(include_incomplete=True).loc[2021] == pytest.approx(100.0)


@pytest.fixture
def peaks_river(river_factory):
    '''Base flow of 10 with known peaks in WY2021 (an early one and a late one) and WY2022.'''
    river = river_factory(start='2020-10-01', end='2022-09-30', value=10.0)
    river.data.loc['2020-11-01', 'discharge_cfs'] = 2000.0   # early peak, water doy 32
    river.data.loc['2021-06-15', 'discharge_cfs'] = 1000.0
    river.data.loc['2022-05-20', 'discharge_cfs'] = 500.0
    return river


class TestMax:
    def test_default_finds_peak_and_date(self, peaks_river):
        result = HydroAnalyzer(peaks_river).max('discharge_cfs')

        assert result.loc[2021, 'max_discharge_cfs'] == 2000.0
        assert result.loc[2021, 'date_of_max_discharge_cfs'] == pd.Timestamp('2020-11-01')
        assert result.loc[2022, 'max_discharge_cfs'] == 500.0
        assert result.loc[2022, 'date_of_max_discharge_cfs'] == pd.Timestamp('2022-05-20')

    def test_after_water_doy_excludes_early_peak(self, peaks_river):
        result = HydroAnalyzer(peaks_river).max('discharge_cfs', after_water_doy=100)
        assert result.loc[2021, 'max_discharge_cfs'] == 1000.0
        assert result.loc[2021, 'date_of_max_discharge_cfs'] == pd.Timestamp('2021-06-15')

    def test_from_month_day_excludes_early_peak(self, peaks_river):
        result = HydroAnalyzer(peaks_river).max('discharge_cfs', from_month_day=(3, 1))
        assert result.loc[2021, 'max_discharge_cfs'] == 1000.0

    def test_from_month_day_is_inclusive(self, peaks_river):
        h = HydroAnalyzer(peaks_river)
        on_day = h.max('discharge_cfs', from_month_day=(6, 15))
        day_after = h.max('discharge_cfs', from_month_day=(6, 16))

        assert on_day.loc[2021, 'max_discharge_cfs'] == 1000.0
        assert day_after.loc[2021, 'max_discharge_cfs'] == 10.0

    def test_from_month_day_takes_precedence_over_water_doy(self, peaks_river):
        # after_water_doy=366 alone would exclude every row in these years
        result = HydroAnalyzer(peaks_river).max(
            'discharge_cfs', after_water_doy=366, from_month_day=(3, 1)
        )
        assert result.loc[2021, 'max_discharge_cfs'] == 1000.0

    def test_leap_day_is_a_valid_month_day(self, peaks_river):
        result = HydroAnalyzer(peaks_river).max('discharge_cfs', from_month_day=(2, 29))
        assert result.loc[2021, 'max_discharge_cfs'] == 1000.0

    @pytest.mark.parametrize('bad', [(2, 30), (13, 1), (4, 31)])
    def test_invalid_month_day_raises(self, peaks_river, bad):
        with pytest.raises(ValueError, match='Invalid month/day'):
            HydroAnalyzer(peaks_river).max('discharge_cfs', from_month_day=bad)

    def test_incomplete_year_handling(self, river_factory):
        river = river_factory(start='2020-12-01', end='2022-09-30', value=10.0)
        river.data.loc['2021-06-15', 'discharge_cfs'] = 1000.0
        h = HydroAnalyzer(river)

        assert pd.isna(h.max('discharge_cfs').loc[2021, 'max_discharge_cfs'])
        included = h.max('discharge_cfs', include_incomplete=True)
        assert included.loc[2021, 'max_discharge_cfs'] == 1000.0

    def test_other_variable_names_columns(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30')
        river.data['stage_ft'] = 3.0
        river.data.loc['2021-05-01', 'stage_ft'] = 9.0

        result = HydroAnalyzer(river).max('stage_ft')

        assert result.loc[2021, 'max_stage_ft'] == 9.0
        assert 'date_of_max_stage_ft' in result.columns

    def test_unknown_variable_raises(self, peaks_river):
        with pytest.raises(ValueError, match='not found'):
            HydroAnalyzer(peaks_river).max('nope')


@pytest.fixture
def monthly_river(river_factory):
    '''One complete water year where every day's flow is 10 x its month number.'''
    river = river_factory(start='2020-10-01', end='2021-09-30')
    river.data['discharge_cfs'] = (river.data.index.month * 10.0).to_numpy()
    return river


class TestMonthlyStat:
    def test_pivot_layout_and_values(self, monthly_river):
        result = HydroAnalyzer(monthly_river).monthly_stat('discharge_cfs', 'mean')

        assert list(result.columns) == MONTHS_WY
        assert list(result.index) == [2021]
        assert result.loc[2021, 'Oct'] == 100.0
        assert result.loc[2021, 'Jan'] == 10.0
        assert result.loc[2021, 'Sep'] == 90.0

    @pytest.mark.parametrize(
        'stat, expected', [('mean', 16.0), ('min', 1.0), ('max', 31.0), ('median', 16.0)]
    )
    def test_each_statistic(self, river_factory, stat, expected):
        river = river_factory(start='2020-10-01', end='2021-09-30')
        river.data['discharge_cfs'] = river.data.index.day.to_numpy(dtype=float)

        result = HydroAnalyzer(river).monthly_stat('discharge_cfs', stat)
        assert result.loc[2021, 'Oct'] == expected   # October has 31 days

    def test_incomplete_month_is_na_by_default(self, monthly_river):
        monthly_river.data.loc['2021-01-15', 'discharge_cfs'] = np.nan
        result = HydroAnalyzer(monthly_river).monthly_stat('discharge_cfs', 'mean')

        assert pd.isna(result.loc[2021, 'Jan'])
        assert result.loc[2021, 'Feb'] == 20.0

    def test_incomplete_month_included_on_request(self, monthly_river):
        monthly_river.data.loc['2021-01-15', 'discharge_cfs'] = np.nan
        result = HydroAnalyzer(monthly_river).monthly_stat(
            'discharge_cfs', 'mean', include_incomplete=True
        )
        assert result.loc[2021, 'Jan'] == 10.0

    def test_long_format(self, monthly_river):
        result = HydroAnalyzer(monthly_river).monthly_stat(
            'discharge_cfs', 'mean', pivot=False
        )

        assert list(result.columns) == ['water_year', 'month', 'discharge_cfs_mean']
        assert len(result) == 12
        oct_row = result.loc[result['month'] == 10, 'discharge_cfs_mean'].iloc[0]
        assert oct_row == 100.0

    def test_stat_all_returns_every_statistic(self, monthly_river):
        result = HydroAnalyzer(monthly_river).monthly_stat('discharge_cfs', 'all')
        assert {'mean', 'max', 'min', 'median'} <= set(result.columns)

    def test_invalid_stat_raises(self, monthly_river):
        with pytest.raises(ValueError, match='Invalid stat'):
            HydroAnalyzer(monthly_river).monthly_stat('discharge_cfs', 'mode')

    def test_invalid_variable_raises(self, monthly_river):
        monthly_river.data['foo'] = 1.0
        with pytest.raises(ValueError, match='Invalid variable'):
            HydroAnalyzer(monthly_river).monthly_stat('foo', 'mean')

    def test_missing_variable_raises(self, monthly_river):
        with pytest.raises(ValueError, match='not found'):
            HydroAnalyzer(monthly_river).monthly_stat('stage_ft', 'mean')


class TestSpringPulse:
    def make_river(self, river_factory):
        # Low flow from calendar DOY 9 to 119 (Apr 29), then high flow.
        # The cumulative departure from the window mean bottoms out on the
        # last low-flow day, so the pulse onset should be 2021-04-29.
        river = river_factory(start='2020-10-01', end='2021-09-30')
        doy = river.data.index.day_of_year.to_numpy()
        river.data['discharge_cfs'] = np.where((doy >= 9) & (doy <= 119), 10.0, 100.0)
        return river

    def test_known_onset_date(self, river_factory):
        result = HydroAnalyzer(self.make_river(river_factory)).spring_pulse()

        expected_date = pd.Timestamp('2021-04-29')
        expected_doy = (expected_date - pd.Timestamp('2020-10-01')).days + 1
        assert result.loc[2021, 'spring_pulse_date'] == expected_date
        assert result.loc[2021, 'spring_pulse_doy'] == expected_doy

    def test_nan_in_window_gives_na(self, river_factory):
        river = self.make_river(river_factory)
        river.data.loc['2021-03-01', 'discharge_cfs'] = np.nan

        result = HydroAnalyzer(river).spring_pulse()

        assert pd.isna(result.loc[2021, 'spring_pulse_date'])
        assert pd.isna(result.loc[2021, 'spring_pulse_doy'])

    def test_year_with_no_data_in_window_gives_na(self, river_factory):
        river = river_factory(start='2020-10-01', end='2020-12-31')
        result = HydroAnalyzer(river).spring_pulse()
        assert pd.isna(result.loc[2021, 'spring_pulse_date'])

    def test_unknown_variable_raises(self, river_factory):
        river = self.make_river(river_factory)
        with pytest.raises(ValueError, match='not found'):
            HydroAnalyzer(river).spring_pulse('nope')


class TestCenterOfMass:
    def test_constant_flow_centers_mid_year(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30', value=50.0)
        result = HydroAnalyzer(river).center_of_mass()

        # mean of water days 1..365 is 183, i.e. 182 days after Oct 1
        expected = pd.Timestamp('2020-10-01') + pd.Timedelta(days=182)
        assert result.loc[2021, 'center_of_mass'] == expected

    def test_all_flow_on_one_day(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30', value=0.0)
        river.data.loc['2021-03-01', 'discharge_cfs'] = 1000.0

        result = HydroAnalyzer(river).center_of_mass()
        assert result.loc[2021, 'center_of_mass'] == pd.Timestamp('2021-03-01')

    def test_gap_gives_na(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30')
        river.data = river.data.drop(pd.Timestamp('2021-03-15'))
        assert pd.isna(HydroAnalyzer(river).center_of_mass().loc[2021, 'center_of_mass'])

    def test_nan_flow_gives_na(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30')
        river.data.loc['2021-03-15', 'discharge_cfs'] = np.nan
        assert pd.isna(HydroAnalyzer(river).center_of_mass().loc[2021, 'center_of_mass'])

    def test_zero_total_flow_gives_na(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30', value=0.0)
        assert pd.isna(HydroAnalyzer(river).center_of_mass().loc[2021, 'center_of_mass'])

    def test_partial_contiguous_year_gives_na(self, river_factory):
        river = river_factory(start='2020-12-01', end='2022-09-30')
        result = HydroAnalyzer(river).center_of_mass()
        assert pd.isna(result.loc[2021, 'center_of_mass'])


class TestFractionalMonthlyFlow:
    def test_constant_flow_shares_follow_month_length(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30', value=100.0)
        result = HydroAnalyzer(river).fractional_monthly_flow()

        assert list(result.columns) == MONTHS_WY
        assert result.loc[2021].sum() == pytest.approx(100.0)
        assert result.loc[2021, 'Oct'] == pytest.approx(31 / 365 * 100)
        assert result.loc[2021, 'Feb'] == pytest.approx(28 / 365 * 100)

    def test_calendar_order(self, river_factory):
        river = river_factory(start='2020-10-01', end='2021-09-30')
        result = HydroAnalyzer(river).fractional_monthly_flow(use_calendar_year=True)
        assert list(result.columns) == MONTHS_CAL

    def test_incomplete_year_is_all_na_by_default(self, river_factory):
        river = river_factory(start='2020-12-01', end='2022-09-30')
        result = HydroAnalyzer(river).fractional_monthly_flow()

        assert result.loc[2021].isna().all()
        assert result.loc[2022].sum() == pytest.approx(100.0)

    def test_incomplete_year_included_on_request(self, river_factory):
        river = river_factory(start='2020-12-01', end='2022-09-30')
        result = HydroAnalyzer(river).fractional_monthly_flow(include_incomplete=True)
        assert result.loc[2021].sum() == pytest.approx(100.0)