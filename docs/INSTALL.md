# Installation and Setup

HydroAnalyze is distributed as a Python application with a Windows batch-file launcher. The launcher uses a dedicated Conda environment to run the application without requiring users to activate the environment manually.

### Requirements

- Windows
- Miniconda (or another compatible Conda installation)
- Access to the USGS web services for retrieving USGS gage data
- Locally stored NOAA data for analyzing NOAA gages

### 1. Install the HydroAnalyze Application

Run 
```
git clone https://github.com/smHooper/hydroanalyze.git
``` 
or [download the repository](https://github.com/smHooper/hydroanalyze/archive/refs/heads/main.zip) and unpack it.

Run `isntall.ps1` using PowerShell which will:

1. Verify Conda exists
2. Create Conda environment if it doesn't already exist
3. Set HYDROANALYZE_ENV environment variable (for the current user) if it doesn't already exist
4. Create NOAA data directory if necessary (See [Configure NOAA Data Store](#4-configure-noaa-data-store-if-applicable) for more info)
5. Create the project config file if necessary

Open a new terminal window 

**Important:** Restart any open terminals after changing system environment variables so that the new settings are available.

### 2. Add HydroAnalyze to the PATH (Optional)

To run `hydroanalyze` from any directory, add the installation directory to the Windows system `PATH` environment variable.

For example:

```text
C:\Program Files\HydroAnalyze
```

After updating `PATH`, open a new terminal.

You can then invoke the CLI from any directory:

```powershell
hydroanalyze --help
```

If the installation directory is not added to `PATH`, you can still run the application by specifying the full path to `hydroanalyze.bat`.

### 3. Verify the Installation

Open a new terminal and run:

```powershell
hydroanalyze --help
```

You should see the available commands:

- `info`
- `fetch`
- `analyze`
- `metrics`
- `gages`
- `add-noaa`

You can also verify that the application can access a particular gage:

```powershell
hydroanalyze info GAGE_ID
```

### 4. Configure NOAA Data store (If Applicable)

NOAA gage data must be downloaded and added to the local NOAA data store before it can be accessed by HydroAnalyze. Historical time series data are not available online, but you can obtain QA/QC-ed data directly by contacting the National Water Prediction Service.

The location of the NOAA data store is configured through the application's JSON configuration file.

Once NOAA data files have been downloaded, use `add-noaa` to import them:

```powershell
hydroanalyze add-noaa "C:\path\to\noaa\data" --dry-run
```

Review the proposed changes, then perform the import:

```powershell
hydroanalyze add-noaa "C:\path\to\noaa\data"
```

To see which NOAA gages are available:

```powershell
hydroanalyze gages
```

See the `add-noaa` [USAGE](USAGE.md#add-noaa--add-noaa-data-to-the-local-data-store) for additional information and command options.