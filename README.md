# HydroAnalyze

A command-line application for retrieving, managing, and
analyzing USGS and NOAA river gage data.

## Features

- Retrieve USGS gage data directly from the USGS.
- Manage locally stored NOAA gage data.
- Examine gage metadata and data completeness.
- Calculate annual and monthly hydrologic metrics.
- Export analysis results to CSV.

## Getting Started

See [Installation](docs/INSTALL.md) for setup instructions.

See [Usage](docs/USAGE.md) for command reference and examples.

### Available Commands

| Command | Purpose |
|---|---|
| `hydroanalyze info GAGE_ID` | Inspect gage metadata and data availability. |
| `hydroanalyze fetch GAGE_ID` | Retrieve and save gage data. |
| `hydroanalyze analyze GAGE_ID METRIC` | Run a hydrologic analysis. |
| `hydroanalyze metrics` | List available analyses. |
| `hydroanalyze gages` | List locally available NOAA gages. |
| `hydroanalyze add-noaa SEARCH_DIR` | Import NOAA data into the local data store. |

For additional details about any command, use:

```bash
hydroanalyze COMMAND --help
```

## Documentation

- [Installation and Configuration](docs/INSTALL.md)
- [Command-Line Usage](docs/USAGE.md)