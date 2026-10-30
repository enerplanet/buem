# BUEM PowerShell Management Script
# -----------------------------------------------------------------------------
# Usage:  .\setup.ps1 <command> [options]
#
# This script requires NO hardcoded paths.  It uses:
#   - $PSScriptRoot  - the directory containing this file (project root)
#   - The 'buem' console-script installed by:  conda develop src
#   - conda run -n <env>  as a fallback when 'buem' is not yet on PATH
#   - docker compose  for container-based workflows
#
# To make 'buem' available directly, activate the conda environment first:
#   conda activate buem_env
#   conda develop src
# -----------------------------------------------------------------------------

param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [Parameter(ValueFromRemainingArguments)]
    [string[]]$Rest
)

Set-Location $PSScriptRoot

# -- Conda environment name ----------------------------------------------------
# Override via: $env:BUEM_CONDA_ENV = "my_env"  or  set it in .env
$CondaEnv = if ($env:BUEM_CONDA_ENV) { $env:BUEM_CONDA_ENV } else { "buem_env" }

# -- Helper: run a 'buem' subcommand ------------------------------------------
# If 'buem' is on PATH (conda env active + conda develop src done), use it.
# Otherwise fall back to 'conda run -n <env> buem'.
function Invoke-Buem {
    param([string[]]$BuemArgs)

    if (Get-Command "buem" -ErrorAction SilentlyContinue) {
        buem @BuemArgs
    }
    else {
        Write-Host "  'buem' not found on PATH - falling back to: conda run -n $CondaEnv" `
            -ForegroundColor DarkYellow
        conda run -n $CondaEnv --no-capture-output buem @BuemArgs
    }
}

# -- Commands ------------------------------------------------------------------

function Show-Help {
    Write-Host "BUEM PowerShell Commands" -ForegroundColor Cyan
    Write-Host "========================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Usage:  .\setup.ps1 <command> [options]" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Environment Setup:" -ForegroundColor Green
    Write-Host "  env-update       Create/update the conda env from $EnvFile"
    Write-Host "                   (pulls in the latest occupancy/weather, tracked @main)"
    Write-Host "  install          env-update + install BUEM (conda develop src)"
    Write-Host "  install-dev      install + dev extras (pytest, ruff, mypy)"
    Write-Host "  validate         Verify installation and environment paths"
    Write-Host "  version          Print the installed BuEM version"
    Write-Host ""
    Write-Host "Model Commands:" -ForegroundColor Green
    Write-Host "  run              Run the thermal model for a single building"
    Write-Host "  run --plot       Run with result plots"
    Write-Host "  run --milp       Run using the MILP solver (experimental)"
    Write-Host ""
    Write-Host "API Server:" -ForegroundColor Green
    Write-Host "  api              Start Gunicorn API server (production)"
    Write-Host "  api --dev        Start Flask dev server (development only)"
    Write-Host "  api --port 8080  Start on a custom port"
    Write-Host ""
    Write-Host "Multi-Building:" -ForegroundColor Green
    Write-Host "  multibuilding                               Run parallel processing (default)"
    Write-Host "  multibuilding --test parallel               Parallel processing only"
    Write-Host "  multibuilding --test sequential             Sequential processing only"
    Write-Host "  multibuilding --test comparison             Parallel vs sequential comparison"
    Write-Host "  multibuilding --test benchmark              Comprehensive benchmark suite"
    Write-Host "  multibuilding --test optimize               Auto-find optimal configuration"
    Write-Host "  multibuilding --buildings 20                Process 20 buildings"
    Write-Host "  multibuilding --workers N                   N worker processes (1 to CPU count)"
    Write-Host "  multibuilding --cores N                     Limit to N CPU cores"
    Write-Host "  multibuilding --sequential                  Force sequential (no parallelism)"
    Write-Host "  multibuilding --validate-system             Show system capabilities and valid ranges"
    Write-Host "  multibuilding --quiet                       Reduce logging verbosity"
    Write-Host "  Note: invalid --workers values are reported as errors"
    Write-Host "  Example: .\setup.ps1 multibuilding --test parallel --workers 8" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Docker Workflows:" -ForegroundColor Green
    Write-Host "  docker-build     Build the Docker image"
    Write-Host "  docker-up        Start containers (detached)"
    Write-Host "  docker-down      Stop and remove containers"
    Write-Host "  docker-logs      Follow container logs"
    Write-Host "  docker-status    Show container status"
    Write-Host "  docker-shell     Open a shell inside the running container"
    Write-Host ""
    Write-Host "Tests:" -ForegroundColor Green
    Write-Host "  test             Run the test suite (pytest tests/)"
    Write-Host "  test-coverage    Run tests with coverage report"
    Write-Host ""
    Write-Host "Cleanup:" -ForegroundColor Green
    Write-Host "  clean            Stop containers and remove build artefacts"
    Write-Host "  clean-all        Remove containers, volumes, and Docker image"
    Write-Host ""
    Write-Host "Examples:" -ForegroundColor Yellow
    Write-Host "  .\setup.ps1 install"
    Write-Host "  .\setup.ps1 run --plot"
    Write-Host "  .\setup.ps1 api"
    Write-Host "  .\setup.ps1 docker-up"
    Write-Host ""
    Write-Host "Conda environment: $CondaEnv  (override: `$env:BUEM_CONDA_ENV)" `
        -ForegroundColor DarkGray
}

$EnvFile = "infrastructure/env/buem_env.yml"

function Invoke-EnvUpdate {
    # Create the conda env if it doesn't exist yet, otherwise update it in
    # place (--prune removes anything no longer listed). This is what
    # actually pulls in the latest occupancy/weather (tracked @main in
    # $EnvFile) -- re-run this after every `git pull` to stay current.
    $envExists = (conda env list) -match "^\s*$CondaEnv\s"
    if ($envExists) {
        Write-Host "Updating conda environment '$CondaEnv' from $EnvFile ..." -ForegroundColor Blue
        conda env update -n $CondaEnv -f $EnvFile --prune
    }
    else {
        Write-Host "Creating conda environment '$CondaEnv' from $EnvFile ..." -ForegroundColor Blue
        conda env create -f $EnvFile
    }
}

function Invoke-Install {
    Invoke-EnvUpdate
    Write-Host "Installing BUEM into the conda environment..." -ForegroundColor Blue
    Write-Host "  Running: conda develop src" -ForegroundColor DarkGray
    conda develop src
    Write-Host "BUEM installed. Verify with:  .\setup.ps1 validate" -ForegroundColor Green
}

function Invoke-InstallDev {
    Invoke-EnvUpdate
    Write-Host "Installing BUEM + dev extras into the conda environment..." -ForegroundColor Blue
    conda develop src
    conda install --name $CondaEnv --yes pytest pytest-cov ruff mypy
    Write-Host "Dev extras installed." -ForegroundColor Green
}

function Invoke-Validate {
    Invoke-Buem @("validate")
}

function Invoke-Version {
    Invoke-Buem @("version")
}

function Invoke-Run {
    Invoke-Buem @(@("run") + $Rest)
}

function Invoke-Api {
    Invoke-Buem @(@("api") + $Rest)
}

function Invoke-Multibuilding {
    Invoke-Buem @(@("multibuilding") + $Rest)
}

$ComposeFile = "infrastructure/container/docker-compose.yml"

function Invoke-DockerBuild {
    Write-Host "Building Docker image..." -ForegroundColor Blue
    docker compose -f $ComposeFile build
}

function Invoke-DockerUp {
    Write-Host "Starting containers..." -ForegroundColor Blue
    docker compose -f $ComposeFile up -d
    Write-Host "API available at http://localhost:5000" -ForegroundColor Green
}

function Invoke-DockerDown {
    Write-Host "Stopping containers..." -ForegroundColor Blue
    docker compose -f $ComposeFile down
}

function Invoke-DockerLogs {
    docker compose -f $ComposeFile logs -f
}

function Invoke-DockerStatus {
    docker compose -f $ComposeFile ps
}

function Invoke-DockerShell {
    Write-Host "Opening shell in the api container..." -ForegroundColor Blue
    docker compose -f $ComposeFile exec api bash
}

function Invoke-Test {
    Write-Host "Running test suite..." -ForegroundColor Blue
    if (Get-Command "pytest" -ErrorAction SilentlyContinue) {
        pytest tests/ -v
    }
    else {
        conda run -n $CondaEnv --no-capture-output pytest tests/ -v
    }
}

function Invoke-TestCoverage {
    Write-Host "Running tests with coverage..." -ForegroundColor Blue
    if (Get-Command "pytest" -ErrorAction SilentlyContinue) {
        pytest tests/ -v --cov=buem --cov-report=term-missing
    }
    else {
        conda run -n $CondaEnv --no-capture-output pytest tests/ -v `
            --cov=buem --cov-report=term-missing
    }
}

function Invoke-Clean {
    Write-Host "Cleaning build artefacts and stopping containers..." -ForegroundColor Blue
    docker compose -f $ComposeFile down 2>$null
    Remove-Item -Recurse -Force "src\buem.egg-info" -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force "src\buem\__pycache__" -ErrorAction SilentlyContinue
    Get-ChildItem -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Clean complete." -ForegroundColor Green
}

function Invoke-CleanAll {
    Write-Host "Removing containers, volumes, and Docker image..." -ForegroundColor Blue
    docker compose -f $ComposeFile down -v --rmi all 2>$null
    Invoke-Clean
}

# -- Dispatch ------------------------------------------------------------------
switch ($Command.ToLower()) {
    "help"          { Show-Help }
    "env-update"    { Invoke-EnvUpdate }
    "install"       { Invoke-Install }
    "install-dev"   { Invoke-InstallDev }
    "validate"      { Invoke-Validate }
    "version"       { Invoke-Version }
    "run"           { Invoke-Run }
    "api"           { Invoke-Api }
    "multibuilding" { Invoke-Multibuilding }
    "docker-build"  { Invoke-DockerBuild }
    "docker-up"     { Invoke-DockerUp }
    "docker-down"   { Invoke-DockerDown }
    "docker-logs"   { Invoke-DockerLogs }
    "docker-status" { Invoke-DockerStatus }
    "docker-shell"  { Invoke-DockerShell }
    "test"          { Invoke-Test }
    "test-coverage" { Invoke-TestCoverage }
    "clean"         { Invoke-Clean }
    "clean-all"     { Invoke-CleanAll }
    default {
        Write-Host "Unknown command: $Command" -ForegroundColor Red
        Write-Host ""
        Show-Help
        exit 1
    }
}
