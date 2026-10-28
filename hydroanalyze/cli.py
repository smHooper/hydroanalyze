from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional
import requests
import typer
import xml.etree.ElementTree as ET

from analyze import (
    HydroAnalyzer, 
    RiverData, 
    USGSClient
)
from metadata import ensure_noaa_metadata
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
        ("--variable", "--include-incomplete", "--after-water-doy"),
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


def _load(
        gage_id: str, 
        out_dir: Path, 
        is_usgs: Optional[bool]=False
    ) -> tuple[RiverData, bytes]:

    if is_usgs or gage_id.isdigit():
        try:
            xml = USGSClient().fetch_data(gage_id)
        except requests.HTTPError as e:
            typer.secho(
                f"USGS rejected the request for gage {gage_id}: {e}." +  
                " If this is a NOAA gage, use the --noaa flag.",
                fg=typer.colors.RED, 
                err=True
            )
            raise typer.Exit(1)
        except requests.ConnectionError:
            typer.secho("Could not reach USGS. Check your internet connection.",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        try:
            river = RiverData(gage_id, out_dir).load_from_xml(xml)
        except ValueError:
            typer.secho(
                f"No timeseries data found for gage ID {gage_id}",
                fg=typer.colors.RED, err=True
            )
            raise typer.Exit(1)
    else:
        noaa_data_dir = get_noaa_data_path()
        try:
            river = RiverData(gage_id, out_dir).load_noaa(noaa_data_dir)
        except ValueError, FileNotFoundError:
            typer.secho(f"Could not find NOAA data for gage {gage_id}. "
                        "Please download the data from NOAA and place it in the output directory.",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        
        xml = None  # NOAA data does not come in XML format
        
    river.get_water_year()
    
    return river, xml


@app.command()
def info(
    gage_id: str = typer.Argument(..., metavar="GAGE_ID", help="USGS/NOAA gage ID"),
    out: Path = typer.Option(Path("hydroanalyze_output"), help="Output folder"),
):
    """
    Show the metadata, period of record, and incomplete water years for a gage.
    """
    
    river, _ = _load(gage_id, out)
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
    is_noaa: bool = typer.Option(
        False, 
        "--noaa", 
        help="Search for NOAA gage data instead of USGS"
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
    river, xml = _load(gage_id, out, is_usgs=not is_noaa)

    if show_metadata:
        typer.echo(river.metadata.pretty_print())
        raise typer.Exit(code=0)
    
    if xml:
        xml_path = river.save_xml(xml)
        typer.echo(f"Saved {xml_path.resolve()}")
    
    csv_path = river.to_csv()
    typer.echo(f"Saved {csv_path.resolve()}")


@app.command(epilog="Metrics: " + ", ".join(m.value for m in Metric))
def analyze(
    gage_id: str = typer.Argument(
        ..., 
        metavar="GAGE ID",
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
    river, _ = _load(gage_id, out)
    h = HydroAnalyzer(river)

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
                    after_water_doy
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