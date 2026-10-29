"""
Command-line interface for calling hydroanalyze API

Author: Sam Hooper
Create date: 2026-10-21
Contact: samuel_hooper@nps.gov
"""

from __future__ import annotations

from click.core import ParameterSource
import datetime as dt
from enum import Enum
from pathlib import Path
import requests
import typer
import xml.etree.ElementTree as ET

from analyze import (
    HydroAnalyzer, 
    RiverData, 
    USGSClient
)
from utils.helpers import get_noaa_data_path

app = typer.Typer(
    help="Analyze USGS or NOAA river gage data.",
    no_args_is_help=True,
    add_completion=False,
)


class Metric(str, Enum):
    """CLI-facing analyses. Value is what the user types."""

    description: str
    options: tuple[str, ...]

    def __new__(cls, value: str, description: str, options: tuple[str, ...] = ()):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.description = description
        obj.options = options
        return obj

    total_discharge = (
        "total-discharge",
        "Total volume of water passing the gage each water year, in cubic feet.",
        ("--include-incomplete",),
    )
    mean_discharge = (
        "mean-discharge",
        "Mean annual discharge for each water year, in cubic feet per second.",
        ("--include-incomplete",),
    )
    max = (
        "max",
        "Peak value and date of the peak for each water year (default: discharge).",
        ("--variable", "--include-incomplete", "--after-water-doy", "--from-date"),
    )
    monthly = (
        "monthly",
        "Monthly mean, min, max, or median for each water year (Oct-Sep).",
        ("--variable", "--stat", "--include-incomplete", "--long"),
    )
    spring_pulse = (
        "spring-pulse",
        "Onset date of the spring snowmelt pulse each water year "
        "(Stewart et al. 2005 cumulative departure method).",
        ("--variable",),
    )
    center_of_mass = (
        "center-of-mass",
        "Date by which half of the water year's flow has passed (flow-weighted "
        "center of the hydrograph). Complete years only.",
    )
    fractional_monthly = (
        "fractional-monthly",
        "Each month's share of the water year's total flow, as a percentage.",
        ("--include-incomplete", "--calendar-order"),
    )

class DataSource(str, Enum):
    usgs = "usgs"
    noaa = "noaa"

# Every flag that belongs to at least one metric, derived from the enum.
METRIC_FLAGS = {flag for m in Metric for flag in m.options}

def _flags_given(ctx: typer.Context) -> list[str]:
    """
    Flag strings (e.g. '--stat') the user typed on the command line.
    """
    given = []
    for param in ctx.command.params:
        source = ctx.get_parameter_source(param.name)
        # Compare by name: the enum class may differ between click and
        #   typer's bundled copy, so `==` against an imported member can fail.
        if source is not None and source.name == "COMMANDLINE":
            given.append(param.opts[0])

    return given


def _check_flags(ctx: typer.Context, metric: Metric) -> None:
    given = _flags_given(ctx)

    # Only metric-specific flags are checked. Shared ones such as --out and
    # --data-source aren't in any Metric.options, so they're ignored here.
    unused = [f for f in given if f in METRIC_FLAGS and f not in metric.options]
    if unused:
        typer.secho(
            f"{', '.join(unused)} not used by '{metric.value}'. "
            "See 'hydroanalyze metrics'.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)
    
    if "--from-date" in given and "--after-water-doy" in given:
        typer.secho(
            "Use either --from-date or --after-water-doy, not both.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)

def _load(
        gage_id: str, 
        out_dir: Path, 
        data_source: str | None = None
    ) -> tuple[RiverData, bytes]:

    # If the user didn't supply the --data_source flag, infer if it's USGS or 
    #   NOAA by the gage ID pattern - USGS uses all numbers but NOAA uses 
    #   letters and numbers
    use_usgs = (
        gage_id.isdigit() 
        if data_source is None 
        else data_source is DataSource.usgs
    )
    
    if use_usgs:
        try:
            xml = USGSClient().fetch_data(gage_id)
        except requests.HTTPError as e:
            typer.secho(
                f"USGS rejected the request for gage {gage_id}: {e}."  
                " If this is a NOAA gage, use the --data-source NOAA option.",
                fg=typer.colors.RED, 
                err=True
            )
            raise typer.Exit(1)
        except (requests.ConnectionError, requests.RequestException):
            typer.secho("Could not reach USGS. Check your internet connection.",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        try:
            river = RiverData(gage_id, out_dir).load_from_xml(xml)
        except (ValueError, ET.ParseError) as e:
            typer.secho(
                f"No timeseries data found for gage ID {gage_id}: {e}",
                fg=typer.colors.RED, err=True
            )
            raise typer.Exit(1)
    else:
        noaa_data_dir = get_noaa_data_path()
        try:
            river = RiverData(gage_id, out_dir).load_noaa(noaa_data_dir)
        except (ValueError, FileNotFoundError):
            typer.secho(f"Could not find NOAA data for gage {gage_id}. "
                        "Please download the data from NOAA and place it in the output directory.",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        
        xml = None  # NOAA data does not come in XML format
        
    river.get_water_year()
    
    return river, xml


@app.command()
def info(
    gage_id: str = typer.Argument(
        ..., 
        metavar="GAGE_ID", 
        help="USGS/NOAA gage ID"
    ),
    out: Path = typer.Option(
        Path("./hydroanalyze_output"), 
        help="Output folder"
    ),
    data_source: DataSource | None = typer.Option(
        None, 
        "--data-source", 
        case_sensitive=False,
        help="Search for gage data from the specified source. Options: NOAA, USGS"
    ),
):
    """
    Show the metadata, period of record, and incomplete water years for a gage.
    """
    
    river, _ = _load(gage_id, out, data_source)
    
    if river.metadata is None:
        typer.secho(
            f"Gage ID: {gage_id}\nNo metadata found for this gage\n", 
            fg=typer.colors.BRIGHT_YELLOW
        )
    else:
        typer.echo(river.metadata.pretty_print())
    
    typer.echo(river.data_ranges().to_string())
    
    for col, years in river.incomplete_years().items():
        if col == 'year':
            continue
        typer.echo(f"\n{col}: {len(years)} incomplete water years")
        if years:
            typer.echo("  " + ", ".join(map(str, years)))


@app.command()
def fetch(
    gage_id: str = typer.Argument(
        ..., 
        help="USGS/NOAA gage ID, e.g. 09085100"
    ),
    out: Path = typer.Option(
        Path("hydroanalyze_output"), 
        help="Output folder"
    ),
    data_source: DataSource | None = typer.Option(
        None, 
        "--data-source",
        case_sensitive=False,
        help="Search for gage data from the specified source. Options: NOAA, USGS"
    ),
    show_metadata: bool = typer.Option(
        False, 
        "--show-metadata", 
        help="Show metadata in the console"
    )
):
    """
    Fetch data for a gage and save the raw XML (if USGS) and a CSV. 
    Alternatively use the --show-metadata flag to just display the gage's 
    metadata
    """
    river, xml = _load(
        gage_id, 
        out, 
        data_source
    )

    if show_metadata:
        typer.echo(river.metadata.pretty_print())
        raise typer.Exit(code=0)
    
    if xml:
        xml_path = river.save_xml(xml)
        typer.echo(f"Saved {xml_path.resolve()}")
    
    csv_path = river.to_csv()
    typer.echo(f"Saved {csv_path.resolve()}")


def _parse_month_day(value: str | None) -> tuple[int, int] | None:
    """
    Helper to validate the from_date parameter
    """
    if value is None:
        return None
    try:
        parsed = dt.datetime.strptime(f"2000-{value}", "%Y-%m-%d")
    except ValueError:
        raise typer.BadParameter(
            f"--from-date {value} is not valid. Use the format MM-DD, e.g."
            " 03-01"
        )
    return parsed.month, parsed.day


@app.command(epilog="Metrics: " + ", ".join(m.value for m in Metric))
def analyze(
    ctx: typer.Context,

    gage_id: str = typer.Argument(
        ..., 
        metavar="GAGE_ID",
        help="USGS/NOAA gage ID"
    ),
    metric: Metric = typer.Argument(
        ..., 
        metavar="METRIC",
        help="Analysis to run. Use 'hydroanalyze metrics' to list them."
    ),
    # Optional args
    out: Path = typer.Option(
        Path("./hydroanalyze_output"), 
        help="Output folder"
    ),
    data_source: DataSource | None = typer.Option(
        None, 
        "--data-source", 
        case_sensitive=False,
        help="Search for gage data from the specified source. Options: NOAA, USGS"
    ),
    include_incomplete: bool = typer.Option(
        False, 
        "--include-incomplete", 
        help="Keep water years/months with missing days"
    ),
    variable: str = typer.Option(
        "discharge_cfs", 
        help="Column to analyze (max/monthly)"
    ),
    stat: str = typer.Option(
        "mean", 
        help="mean, min, max, median, all. Only valid for 'monthly' metric"
    ),
    after_water_doy: int = typer.Option(
        1, 
        "--after-water-doy", 
        min=1, 
        max=366,
        help="Only consider values on/after this water day of year ('max'" 
            " command only)",
    ),
    from_date: str | None = typer.Option(
        None, 
        "--from-date", 
        help="Similar to --after-water-doy but uses numeric Month-Day (e.g.,"
            " 03-01) instead ('max' command only)",
    ),
    long_format: bool = typer.Option(
        False, 
        "--long", 
        help="Long format instead of a water year x month table ('monthly' only)",
    ),
    calendar_order: bool = typer.Option(
        False, 
        "--calendar-order", 
        help="Use calendar month order Jan-Dec instead of water year Oct-Sep"
            " ('fractional-monthly only')",
    ),
):
    """
    Run an analysis and save the result as CSV. All metrics return an annual 
    result for the entire period of record except for 'monthly' and 
    'fractional-monthly'
    """
    # Make sure there aren't any conflicting flags
    _check_flags(ctx, metric)

    river, _ = _load(gage_id, out, data_source)
    h = HydroAnalyzer(river)
    
    if from_date is not None:
        from_date = _parse_month_day(from_date)

    try:
        runners = {
            Metric.total_discharge: lambda: 
                h.total_discharge(include_incomplete),
            
            Metric.mean_discharge: lambda: 
                h.mean_discharge(include_incomplete),
            
            Metric.max: lambda: 
                h.max(
                    variable, 
                    include_incomplete, 
                    after_water_doy,
                    from_month_day=from_date
                ),
            
            Metric.monthly: lambda: 
                h.monthly_stat(
                    variable, 
                    stat, 
                    include_incomplete,
                    pivot=not long_format
                ),
            
            Metric.spring_pulse: lambda: 
                h.spring_pulse(variable),
            
            Metric.center_of_mass: 
                h.center_of_mass,
            
            Metric.fractional_monthly: lambda: 
                h.fractional_monthly_flow(
                    include_incomplete,
                    use_calendar_year=calendar_order
                ),
        }

        result = runners[metric]()
    except ValueError as e:        # your API already raises clear ValueErrors
        typer.secho(f"Error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    out.mkdir(parents=True, exist_ok=True)
    path = river.file_manager.unique_filename(out / f"{gage_id}_{metric.value}.csv")
    result.to_csv(path)
    typer.echo(f"Saved {path}")
    typer.echo(result.head(10).to_string())   # quick preview in the terminal


@app.command()
def metrics():
    """
    List the available analyses and the options each one accepts.
    """
    width = max(len(m.value) for m in Metric)
    for m in Metric:
        typer.secho(
            f"\n{m.value:<{width}}", 
            fg=typer.colors.BRIGHT_YELLOW, 
            bold=True, 
            nl=False
        )
        typer.echo(f"  {m.description}")
        if m.options:
            typer.secho(f"{'':<{width}}  Options: ", fg=typer.colors.CYAN, nl=False)
            typer.secho(", ".join(m.options), fg=typer.colors.BRIGHT_CYAN)


if __name__ == "__main__":
    app()