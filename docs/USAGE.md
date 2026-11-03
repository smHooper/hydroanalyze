# HydroAnalyze CLI

HydroAnalyze is a command-line interface (CLI) for retrieving, managing, and analyzing river gage data from the U.S. Geological Survey (USGS) and the National Oceanic and Atmospheric Administration (NOAA).

The CLI provides tools to:

- Retrieve and inspect gage data.
- Examine gage metadata, periods of record, and missing data.
- Run hydrologic analyses and export results to CSV.
- Manage locally stored NOAA gage data.
- Discover which NOAA gages are available in the local data store.

## Getting Started

To see the available commands:

```bash
hydroanalyze --help
```

To see help for an individual command:

```bash
hydroanalyze COMMAND --help
```

For example:

```bash
hydroanalyze analyze --help
```

### Gage IDs and Data Sources

HydroAnalyze supports two data sources:

| Data source | Gage ID format | Data access |
|---|---|---|
| USGS | Numeric IDs | Retrieved directly from the USGS |
| NOAA | Alphanumeric IDs | Retrieved from the local NOAA data store |

The data source is inferred automatically from the gage ID:

- IDs containing only numbers are assumed to be USGS gages.
- IDs containing letters are assumed to be NOAA gages.

You can override this behavior using the `--data-source` option:

```bash
hydroanalyze info 15515500 --data-source usgs
hydroanalyze info RLYA2 --data-source noaa
```

The source name is case-insensitive.

**Note:** NOAA data must first be downloaded and added to the local data store before it can be accessed through HydroAnalyze. See [add-noaa](#add-noaa--add-noaa-data-to-the-local-data-store).

---

## Commands

### `info` — Inspect a Gage

Display metadata, available data variables, date ranges, and missing-data information for a gage.

**Usage:**

```bash
hydroanalyze info GAGE_ID [OPTIONS]
```

**Arguments:**

| Argument | Description |
|---|---|
| `GAGE_ID` | USGS or NOAA gage identifier |

**Options:**

| Option | Description |
|---|---|
| `--data-source` | Specify `usgs` or `noaa`. Otherwise, infer from the gage ID. |
| `--missing-days VARIABLE` | Show the number of missing days in each water year for a specific variable instead of the default date-range and missing-data summaries. |

**Examples:**

Display general information about a gage:

```bash
hydroanalyze info 15515500
```

Inspect a NOAA gage:

```bash
hydroanalyze info RLYA2
```

Show missing days for a specific variable:

```bash
hydroanalyze info RLYA2 --missing-days stage_ft
```

The default output includes:

- Gage metadata, when available.
- Available data variables.
- Date ranges for each variable.
- Missing-day counts for each variable, organized by water year.

When `--missing-days` is specified, the output instead lists only water years with missing days for the requested variable.

---

### `fetch` — Retrieve and Save Gage Data

Retrieve gage data and save it locally.

**Usage:**

```bash
hydroanalyze fetch GAGE_ID [OPTIONS]
```

**Arguments:**

| Argument | Description |
|---|---|
| `GAGE_ID` | USGS or NOAA gage identifier |

**Options:**

| Option | Default | Description |
|---|---|---|
| `--out` | `~/desktop/hydroanalyze_output` | Output directory. |
| `--data-source` | Inferred | Specify `usgs` or `noaa`. |
| `--overwrite` | `False` | Overwrite existing output files. |
| `--show-metadata` | `False` | Display gage metadata without saving data files. |

**Examples:**

Fetch a USGS gage:

```bash
hydroanalyze fetch 15515500
```

Fetch a NOAA gage:

```bash
hydroanalyze fetch RLYA2
```

Specify an output directory:

```bash
hydroanalyze fetch 15515500 --out ./river_data
```

Display metadata without downloading files:

```bash
hydroanalyze fetch RLYA2 --show-metadata
```

Overwrite existing files:

```bash
hydroanalyze fetch 15515500 --overwrite
```

**Output files:**

For USGS gages, the command saves:

- Raw XML data.
- A CSV containing the gage data.

For NOAA gages, the command saves:

- A CSV containing the gage data.

If an output file already exists, HydroAnalyze generates a unique filename rather than overwriting the existing file. Use `--overwrite` to replace existing files.

---

### `analyze` — Run a Hydrologic Analysis

Run a hydrologic analysis on a gage's period of record and save the results as a CSV.

**Usage:**

```bash
hydroanalyze analyze GAGE_ID METRIC [OPTIONS]
```

**Arguments:**

| Argument | Description |
|---|---|
| `GAGE_ID` | USGS or NOAA gage identifier |
| `METRIC` | Analysis to perform. See [Available Metrics](#available-metrics). |

**Common Options:**

| Option | Default | Description |
|---|---|---|
| `--out` | `~/desktop/hydroanalyze_output` | Output directory. |
| `--overwrite` | `False` | Overwrite an existing output file. |
| `--data-source` | Inferred | Specify `usgs` or `noaa`. |
| `--include-incomplete` | `False` | Include water years or months containing missing days. |

**Metric-Specific Options:**

| Option | Default | Applicable metrics | Description |
|---|---|---|---|
| `--variable` | `discharge_cfs` | `max`, `monthly`, `spring-pulse` | Data variable to analyze. |
| `--stat` | `mean` | `monthly` | Monthly statistic: `mean`, `min`, `max`, `median`, or `all`. |
| `--after-water-doy` | `1` | `max` | Only consider values on or after the specified water day of year (1–366). |
| `--from-date` | None | `max` | Only consider values on or after the specified calendar month and day (`MM-DD`). |
| `--long` | `False` | `monthly` | Return results in long format instead of a water year × month table. |
| `--calendar-order` | `False` | `fractional-monthly` | Return months in calendar order (January–December) instead of water year order (October–September). |

The `--from-date` and `--after-water-doy` options are mutually exclusive.

Options that are not applicable to the selected metric cannot be used. HydroAnalyze reports an error if an incompatible metric-specific option is supplied.

**Examples:**

Calculate annual maximum discharge:

```bash
hydroanalyze analyze 15515500 max
```

Calculate annual maximum stage, including incomplete water years:

```bash
hydroanalyze analyze RLYA2 max --variable stage_ft --include-incomplete
```

Calculate monthly mean stage:

```bash
hydroanalyze analyze RLYA2 monthly --variable stage_ft
```

Calculate all available monthly statistics:

```bash
hydroanalyze analyze RLYA2 monthly --stat all
```

Calculate monthly statistics in long format:

```bash
hydroanalyze analyze RLYA2 monthly --stat all --long
```

Calculate spring pulse onset using stage data:

```bash
hydroanalyze analyze RLYA2 spring-pulse --variable stage_ft
```

Calculate the spring pulse using May 1 as the starting date:

```bash
hydroanalyze analyze RLYA2 spring-pulse --variable stage_ft --from-date 05-01
```

Save results to a specific directory:

```bash
hydroanalyze analyze RLYA2 max --out ./analysis_results
```

**Output:**

Results are saved as CSV files using the following naming convention:

```text
GAGE_ID_METRIC.csv
```

For example:

```text
RLYA2_max.csv
RLYA2_monthly.csv
15515500_total-discharge.csv
```

If a file with the same name already exists, HydroAnalyze generates a unique filename unless `--overwrite` is specified.

The first ten rows of the results are also displayed in the terminal.

---

### `metrics` — List Available Analyses

Display all available analyses and the options supported by each.

**Usage:**

```bash
hydroanalyze metrics
```

This command displays each metric's description and its metric-specific options.

See [Available Metrics](#available-metrics) for descriptions of each analysis.

---

### `gages` — List Available NOAA Gages

List NOAA gages available in the local data store.

**Usage:**

```bash
hydroanalyze gages [OPTIONS]
```

**Options:**

| Option | Description |
|---|---|
| `--search TEXT` | Only display gages whose ID or name contains the specified text. |

**Examples:**

List all available NOAA gages:

```bash
hydroanalyze gages
```

Search for a particular gage:

```bash
hydroanalyze gages --search RLYA2
```

Search by gage name:

```bash
hydroanalyze gages --search Nenana
```

The command displays the gage ID and name for each matching gage.

If no gages are found, the command suggests using `add-noaa` to add data.

---

### `add-noaa` — Add NOAA Data to the Local Data Store

Import raw NOAA gage data files into the local NOAA data store.

The command searches the specified directory, including subdirectories, for files matching a configurable filename pattern.

**Usage:**

```bash
hydroanalyze add-noaa SEARCH_DIR [OPTIONS]
```

**Arguments:**

| Argument | Description |
|---|---|
| `SEARCH_DIR` | Directory to search recursively for NOAA data files. |

**Options:**

| Option | Description |
|---|---|
| `--dry-run` | Report what would happen without writing anything. |
| `--overwrite GAGE` | Allow new values to replace stored values for the specified gage. Can be repeated for multiple gages. |
| `--skip-bad-files` | Import readable files even if some matching files cannot be read. |
| `--search-pattern REGEX` | Regular expression that filenames must fully match. Uses the built-in default pattern unless overridden. |
| `--config PATH` | Specify a configuration file, overriding automatic configuration-file discovery. |

**Examples:**

Preview an import without making changes:

```bash
hydroanalyze add-noaa /path/to/noaa/data --dry-run
```

Import NOAA data:

```bash
hydroanalyze add-noaa /path/to/noaa/data
```

Allow existing values to be replaced for a specific gage:

```bash
hydroanalyze add-noaa /path/to/noaa/data --overwrite RLYA2
```

Allow replacements for multiple gages:

```bash
hydroanalyze add-noaa /path/to/noaa/data \
    --overwrite RLYA2 \
    --overwrite SRDA2
```

Import readable files while skipping unreadable files:

```bash
hydroanalyze add-noaa /path/to/noaa/data --skip-bad-files
```

Specify a custom filename pattern:

```bash
hydroanalyze add-noaa /path/to/noaa/data --search-pattern ".*\.csv"
```

**Data protection:**

The import process is designed to protect existing data:

- Existing stored records are not changed by default.
- Conflicting values must be explicitly authorized using `--overwrite GAGE`.
- Before existing data is replaced, the previous CSV is backed up.
- Every changed record is logged.
- Use `--dry-run` to preview the changes before importing.

**Exit codes:**

| Exit code | Meaning |
|---|---|
| `0` | Import completed successfully. |
| `1` | One or more files were unreadable. |
| `2` | Conflicting values were detected. |

---

## Available Metrics

HydroAnalyze currently supports seven analyses.

All metrics return annual results for the period of record, except `monthly` and `fractional-monthly`, which return monthly results.

### `total-discharge`

Calculates the total volume of water passing the gage during each water year.

- Units: cubic feet.
- Option: `--include-incomplete`.

By default, incomplete water years are excluded.

Example:

```bash
hydroanalyze analyze 15515500 total-discharge
```

### `mean-discharge`

Calculates the mean annual discharge for each water year.

- Units: cubic feet per second.
- Option: `--include-incomplete`.

By default, incomplete water years are excluded.

Example:

```bash
hydroanalyze analyze 15515500 mean-discharge
```

### `max`

Identifies the maximum value and the date on which it occurred for each water year.

- Default variable: `discharge_cfs`.
- Options:
  - `--variable`
  - `--include-incomplete`
  - `--after-water-doy`
  - `--from-date`

Use `--variable` to calculate maxima for a different data variable, such as `stage_ft`.

The `--after-water-doy` and `--from-date` options restrict the analysis to observations on or after a specified point in the water year.

Examples:

```bash
hydroanalyze analyze RLYA2 max
```

```bash
hydroanalyze analyze RLYA2 max --variable stage_ft
```

```bash
hydroanalyze analyze RLYA2 max --after-water-doy 150
```

```bash
hydroanalyze analyze RLYA2 max --from-date 05-01
```

### `monthly`

Calculates monthly statistics for each water year.

- Default variable: `discharge_cfs`.
- Default statistic: `mean`.
- Available statistics: `mean`, `min`, `max`, `median`, `all`.
- Options:
  - `--variable`
  - `--stat`
  - `--include-incomplete`
  - `--long`

By default, results are returned as a table with water years as rows and months as columns, ordered October through September.

Use `--long` to return the results in long format instead.

Examples:

```bash
hydroanalyze analyze RLYA2 monthly
```

```bash
hydroanalyze analyze RLYA2 monthly --stat all
```

```bash
hydroanalyze analyze RLYA2 monthly --variable stage_ft --stat max
```

### `spring-pulse`

Calculates the onset date of the spring snowmelt pulse for each water year using the cumulative departure method described by Stewart et al. (2005).

- Default variable: `discharge_cfs`.
- Option: `--variable`.

Example:

```bash
hydroanalyze analyze RLYA2 spring-pulse
```

Use a different variable:

```bash
hydroanalyze analyze RLYA2 spring-pulse --variable stage_ft
```

### `center-of-mass`

Calculates the flow-weighted center of the hydrograph for each water year.

The result is the date by which half of the water year's total flow has passed.

- Complete water years only.
- No metric-specific options.

Example:

```bash
hydroanalyze analyze 15515500 center-of-mass
```

### `fractional-monthly`

Calculates each month's contribution to the total flow for the water year.

Results are expressed as percentages of the water year's total flow.

- Options:
  - `--include-incomplete`
  - `--calendar-order`

By default, months are ordered by water year (October–September). Use `--calendar-order` to return January–December instead.

Examples:

```bash
hydroanalyze analyze 15515500 fractional-monthly
```

```bash
hydroanalyze analyze 15515500 fractional-monthly --calendar-order
```

---

## Water Years

HydroAnalyze organizes most annual analyses by water year rather than calendar year.

A water year runs from October 1 through September 30 and is identified by the calendar year in which it ends.

For example:

| Water year | Date range |
|---|---|
| 2023 | October 1, 2022 – September 30, 2023 |
| 2024 | October 1, 2023 – September 30, 2024 |
| 2025 | October 1, 2024 – September 30, 2025 |

Monthly analyses use this same October–September ordering unless `--calendar-order` is specified for `fractional-monthly`.

## Output Files and Overwriting

Commands that save output files accept the `--out` option to specify an output directory.

The default output directory is:

```text
~/desktop/hydroanalyze_output
```

The directory is created automatically if it does not already exist.

By default, existing files are protected. If an output filename already exists, HydroAnalyze generates a unique filename rather than replacing the existing file.

For example, repeated analyses might produce:

```text
RLYA2_max.csv
RLYA2_max_1.csv
RLYA2_max_2.csv
```

Use `--overwrite` when you explicitly want to replace an existing output file.

**Note:** The `--overwrite` option for `add-noaa` has a different purpose: it authorizes replacement of existing NOAA data records for specified gages.
