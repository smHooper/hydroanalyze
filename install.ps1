
# HydroAnalyze installer
#
# Run from the project directory:
#   .\install.ps1
#
# Creates the Conda environment, configuration directory and NOAA data
# directory if they don't already exist. Existing configuration is preserved.

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

function Write-Step {
    param([string]$Message)

    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Stop-Install {
    param([string]$Message)

    Write-Host "`nERROR: $Message" -ForegroundColor Red
    exit 1
}

function Invoke-Conda {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    # PowerShell 5.1 can treat stderr output from native executables as
    # terminating errors when ErrorActionPreference is set to Stop.
    # Temporarily use Continue so Conda can execute normally.

    $PreviousErrorActionPreference = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"

        $Output = & $CondaExe @Arguments 2>&1
        $ExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }

    # Return output and exit code together so callers can inspect both.
    return [PSCustomObject]@{
        ExitCode = $ExitCode
        Output   = @($Output | ForEach-Object { "$_" })
    }
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$ProjectDir = if ($psISE)
{
    Split-Path -Path $psISE.CurrentFile.FullPath        
}
else
{
    $global:PSScriptRoot
}
$EnvironmentFile = Join-Path $ProjectDir "environment.yml"

$ParentDir = Split-Path $ProjectDir -Parent
$NoaaDataDir = Join-Path $ParentDir "data\noaa"
$ConfigDir = Join-Path $ParentDir "config"
$ConfigFile = Join-Path $ConfigDir "hydroanalyze_config.json"

# ---------------------------------------------------------------------------
# 1. Verify Conda exists
# ---------------------------------------------------------------------------

Write-Step "Checking Conda installation"

# Prefer the executable path provided by an initialized Conda shell.
$CondaExe = $env:CONDA_EXE

# Fall back to locating conda.exe on PATH.
if (-not $CondaExe -or -not (Test-Path $CondaExe)) {
    $CondaCommand = Get-Command conda.exe -ErrorAction SilentlyContinue

    if ($CondaCommand) {
        $CondaExe = $CondaCommand.Source
    }
}

if (-not $CondaExe -or -not (Test-Path $CondaExe)) {
    Stop-Install "Conda was not found. Install Miniconda or Miniforge and ensure Conda is available in PowerShell."
}

Write-Host "Found Conda: $CondaExe"

if (-not (Test-Path $EnvironmentFile)) {
    Stop-Install "Could not find environment.yml in $ProjectDir"
}

# Read the environment name from environment.yml.
$NameMatch = Select-String -Path $EnvironmentFile `
    -Pattern '^\s*name\s*:\s*["'']?([^"''#\s]+)' |
    Select-Object -First 1

if (-not $NameMatch) {
    Stop-Install "Could not find a Conda environment 'name:' in environment.yml"
}

$EnvironmentName = $NameMatch.Matches[0].Groups[1].Value

Write-Host "Environment name: $EnvironmentName"

# ---------------------------------------------------------------------------
# 2. Create Conda environment if necessary
# ---------------------------------------------------------------------------

Write-Step "Checking Conda environment"

$Result = Invoke-Conda -Arguments @("env", "list", "--json")

if ($Result.ExitCode -ne 0) {
    $Result.Output | ForEach-Object { Write-Host $_ }
    Stop-Install "Unable to retrieve the list of Conda environments."
}

try {
    $EnvironmentList = ($Result.Output -join "`n") | ConvertFrom-Json
}
catch {
    Stop-Install "Could not parse the output from 'conda env list --json'."
}

$ExistingEnvironment = $EnvironmentList.envs |
    Where-Object {
        (Split-Path $_ -Leaf) -eq $EnvironmentName
    } |
    Select-Object -First 1

if ($ExistingEnvironment) {
    $EnvironmentPath = $ExistingEnvironment
    Write-Host "Environment already exists: $EnvironmentPath"
}
else {
    Write-Host "Creating environment '$EnvironmentName'..."

    $Result = Invoke-Conda -Arguments @(
        "env",
        "create",
        "-f", $EnvironmentFile,
        "-y"
    )

    $Result.Output | ForEach-Object { Write-Host $_ }

    if ($Result.ExitCode -ne 0) {
        Stop-Install "Conda environment creation failed (exit code $($Result.ExitCode))."
    }

    # Refresh the environment list to determine the actual environment path.
    $Result = Invoke-Conda -Arguments @("env", "list", "--json")

    if ($Result.ExitCode -ne 0) {
        Stop-Install "Could not retrieve the Conda environment path after creation."
    }

    try {
        $EnvironmentList = ($Result.Output -join "`n") | ConvertFrom-Json
    }
    catch {
        Stop-Install "Could not parse the updated Conda environment list."
    }

    $EnvironmentPath = $EnvironmentList.envs |
        Where-Object {
            (Split-Path $_ -Leaf) -eq $EnvironmentName
        } |
        Select-Object -First 1

    if (-not $EnvironmentPath) {
        Stop-Install "Conda reported successful creation, but the environment could not be located."
    }
}

Write-Host "Environment path: $EnvironmentPath"

# ---------------------------------------------------------------------------
# 3. Set HYDROANALYZE_ENV if necessary
# ---------------------------------------------------------------------------

Write-Step "Checking HYDROANALYZE_ENV"

$ExistingEnvironmentVariable = [Environment]::GetEnvironmentVariable(
    "HYDROANALYZE_ENV",
    "User"
)

if ([string]::IsNullOrWhiteSpace($ExistingEnvironmentVariable)) {
    [Environment]::SetEnvironmentVariable(
        "HYDROANALYZE_ENV",
        $EnvironmentPath,
        "User"
    )

    Write-Host "Set user environment variable HYDROANALYZE_ENV."
}
elseif ($ExistingEnvironmentVariable -ne $EnvironmentPath) {
    Stop-Install @"
HYDROANALYZE_ENV is already set to:
  $ExistingEnvironmentVariable

But the Conda environment is located at:
  $EnvironmentPath

Please resolve this mismatch before continuing.
"@
}
else {
    Write-Host "HYDROANALYZE_ENV is already correctly configured."
}

# Make the variable available to this PowerShell session.
$env:HYDROANALYZE_ENV = $EnvironmentPath

# ---------------------------------------------------------------------------
# 4. Create NOAA data directory if necessary
# ---------------------------------------------------------------------------

Write-Step "Checking NOAA data directory"

if (-not (Test-Path $NoaaDataDir)) {
    New-Item -ItemType Directory -Path $NoaaDataDir -Force | Out-Null
    Write-Host "Created: $NoaaDataDir"
}
else {
    Write-Host "Already exists: $NoaaDataDir"
}

# ---------------------------------------------------------------------------
# 5. Create configuration directory and file if necessary
# ---------------------------------------------------------------------------

Write-Step "Checking configuration directory"

if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
    Write-Host "Created: $ConfigDir"
}
else {
    Write-Host "Already exists: $ConfigDir"
}

Write-Step "Checking configuration file"

if (-not (Test-Path $ConfigFile)) {
    $Config = @{
        NOAA_DATA_PATH = $NoaaDataDir
    }

    $Config | ConvertTo-Json |
        Set-Content -Path $ConfigFile -Encoding UTF8

    Write-Host "Created: $ConfigFile"
}
else {
    Write-Host "Configuration file already exists; leaving it unchanged."
}

# ---------------------------------------------------------------------------
# 6. Verify installation
# ---------------------------------------------------------------------------

Write-Step "Verifying installation"

$PythonExe = Join-Path $EnvironmentPath "python.exe"
$CliFile = Join-Path $ProjectDir "hydroanalyze\cli.py"

if (-not (Test-Path $PythonExe)) {
    Stop-Install "Python executable not found: $PythonExe"
}

if (-not (Test-Path $CliFile)) {
    Stop-Install "CLI script not found: $CliFile"
}

if (-not (Test-Path $ConfigFile)) {
    Stop-Install "Configuration file not found: $ConfigFile"
}

# Verify that the configuration file contains valid JSON.
try {
    $Config = Get-Content $ConfigFile -Raw | ConvertFrom-Json
}
catch {
    Stop-Install "Configuration file is not valid JSON: $ConfigFile"
}

if (-not $Config.NOAA_DATA_PATH) {
    Stop-Install "Configuration file is missing NOAA_DATA_PATH."
}

# Verify that the CLI starts successfully.
& $PythonExe $CliFile --help

if ($LASTEXITCODE -ne 0) {
    Stop-Install "HydroAnalyze CLI verification failed."
}

# ---------------------------------------------------------------------------
# Installation complete
# ---------------------------------------------------------------------------

Write-Host "`nHydroAnalyze installation completed successfully!" -ForegroundColor Green

Write-Host "Project directory: $ProjectDir"
Write-Host "Conda environment: $EnvironmentPath"
Write-Host "NOAA data directory: $NoaaDataDir"
Write-Host "Configuration file: $ConfigFile"

Write-Host "`nYou may need to open a new terminal for the environment variable to be available." -ForegroundColor Yellow
