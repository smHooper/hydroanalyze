from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional
import requests
import typer
import xml.etree.ElementTree as ET

from analyze import HydroAnalyzer, RiverData, USGSClient
from metadata import (
    ensure_noaa_metadata,
    USGSMetadata
)
from utils.helpers import get_noaa_data_path

app = typer.Typer(
    help="Analyze USGS or NOAA river gage data.",
    no_args_is_help=True,
    add_completion=False,
)


class Metric(str, Enum):
    total_discharge = "total-discharge"
    mean_discharge = "mean-discharge"
    max = "max"
    monthly = "monthly"
    spring_pulse = "spring-pulse"
    center_of_mass = "center-of-mass"
    fractional_monthly = "fractional-monthly"


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

        river = RiverData(gage_id, out_dir).load_from_xml(xml)
    else:
        noaa_data_dir = get_noaa_data_path()
        try:
            river = RiverData(gage_id, out_dir).load_noaa(noaa_data_dir)
        except FileNotFoundError:
            typer.secho(f"Could not find NOAA data for gage {gage_id}. "
                        "Please download the data from NOAA and place it in the output directory.",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        
        xml = None  # NOAA data does not come in XML format
        
    river.get_water_year()
    
    return river, xml


@app.command()
def fetch(
    gage_id: str = typer.Argument(..., help="USGS/NOAA gage ID, e.g. 09085100"),
    out: Path = typer.Option(Path("hydroanalyze_output"), help="Output folder"),
    is_noaa: bool = typer.Option(False, "--noaa", help="Search for NOAA gage data instead of USGS"),
    show_metadata: bool = typer.Option(False, "--show-metadata", help="Show metadata in the console")
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
    
    csv_path = river.to_csv()

    typer.echo(f"Saved {xml_path}")
    typer.echo(f"Saved {csv_path}")


@app.command()
def analyze(
    gage_id: str = typer.Argument(..., help="USGS/NOAA gage ID"),
    metric: Metric = typer.Argument(..., help="Which analysis to run"),
    out: Path = typer.Option(Path("hydroanalyze_output"), help="Output folder"),
    include_incomplete: bool = typer.Option(
        False, "--include-incomplete", help="Keep water years with missing days"
    ),
    variable: str = typer.Option("discharge_cfs", help="Column to analyze (max/monthly)"),
    stat: str = typer.Option("mean", help="mean, min, max, median, all (monthly only)"),
):
    """
    Run an analysis and save the result as CSV. All metrics return an annual 
    result for the entire period of record except for 'monthly' and 
    'fractional_monthly'
    """
    river, _ = _load(gage_id, out)
    h = HydroAnalyzer(river)

    try:
        runners = {
            Metric.total_discharge: lambda: h.total_discharge(include_incomplete),
            Metric.mean_discharge: lambda: h.mean_discharge(include_incomplete),
            Metric.max: lambda: h.max(variable, include_incomplete),
            Metric.monthly: lambda: h.monthly_stat(variable, stat, include_incomplete),
            Metric.spring_pulse: lambda: h.spring_pulse(variable),
            Metric.center_of_mass: h.center_of_mass,
            Metric.fractional_monthly: lambda: h.fractional_monthly_flow(include_incomplete),
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
    """List the available analyses."""
    for m in Metric:
        typer.echo(f"  {m.value}")


if __name__ == "__main__":
    app()